#!/usr/bin/env python3
"""Green Shoots aggregator.

Pulls RSS/Atom feeds from good-news and climate-solutions publications,
keeps the upbeat stories, and writes a static ``news.json`` that the
front-end (index.html) reads.

Usage:
    python src/news_aggregator.py            # fetch + update news.json
    python src/news_aggregator.py --check    # test every feed, write nothing

Design notes:
  * Failure-tolerant: a dead feed is logged and skipped, never fatal.
  * Additive: new stories are merged into the existing news.json, so
    stories stay on the site after they scroll off a publisher's feed
    (until MAX_AGE_DAYS).
  * Only rewrites news.json when something actually changed, so the
    scheduled GitHub Action doesn't create empty commits.
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "news.json"

# Be a polite citizen: say who you are. Put your own repo URL here.
USER_AGENT = "GoodNewsEverymoneBot/1.0 (+https://github.com/dankniight/good-news-everyone)"
REQUEST_TIMEOUT = 15
MAX_AGE_DAYS = 45          # drop stories older than this
MAX_ARTICLES = 400         # hard cap on stored stories
OG_LOOKUPS_PER_RUN = 30    # max page fetches per run to find missing images
SUMMARY_TARGET_CHARS = 110  # keep adding sentences until we pass this
SUMMARY_MAX_CHARS = 300

# ---------------------------------------------------------------------------
# Sources
#
# curated=True  -> the whole publication is positive/solutions-focused, keep all
# curated=False -> mixed publication; only keep stories that look like good news
#                  (see POSITIVE_TERMS / NEGATIVE_TERMS below)
#
# Run `python src/news_aggregator.py --check` after editing to verify feeds.
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "name": "Good News Network",
        "site": "https://www.goodnewsnetwork.org",
        "feed": "https://www.goodnewsnetwork.org/feed/",
        "color": "#2E8B57",
        "curated": True,
    },
    {
        "name": "Positive News",
        "site": "https://www.positive.news",
        "feed": "https://www.positive.news/feed/",
        "color": "#E0A21B",
        "curated": True,
    },
    {
        "name": "Reasons to be Cheerful",
        "site": "https://reasonstobecheerful.world",
        "feed": "https://reasonstobecheerful.world/feed/",
        "color": "#3B82C4",
        "curated": True,
    },
    {
        "name": "The Optimist Daily",
        "site": "https://www.optimistdaily.com",
        "feed": "https://www.optimistdaily.com/feed/",
        "color": "#B84F94",
        "curated": True,
    },
    {
        "name": "Grist",
        "site": "https://grist.org",
        "feed": "https://grist.org/feed/",
        "color": "#6F8A2B",
        "curated": False,
    },
    {
        "name": "Canary Media",
        "site": "https://www.canarymedia.com",
        "feed": "https://www.canarymedia.com/rss.rss",
        "color": "#1799A6",
        "curated": False,
    },
    {
        # Mostly tech/AI, so keep only its energy & environment coverage that
        # reads as good news. tag_allow: a matching feed category counts as a
        # positive signal on its own (Futurism tags e.g. "Renewable Energy").
        "name": "Futurism",
        "site": "https://futurism.com",
        "feed": "https://futurism.com/feed",
        "color": "#6E56CF",
        "curated": False,
        "tag_allow": [
            "renewable energy", "solar power", "clean energy", "electric vehicles",
            "mass transit", "environment", "agriculture",
        ],
    },
]

# Term syntax: plain words match whole words (plural "s"/"es" allowed);
# a trailing * means "any word starting with this". Matching is case-insensitive.
POSITIVE_TERMS = [
    "breakthrough*", "record", "record-breaking", "milestone*", "success*",
    "win", "wins", "won", "victory", "victories", "restor*", "recover*",
    "revive*", "revival", "rebound*", "comeback", "thriv*", "flourish*",
    "rewild*", "reforest*", "regenerat*", "rescue*", "celebrat*", "hope*",
    "promising", "progress", "boost*", "cheaper", "cleaner", "clean energy",
    "solution*", "landmark", "surpass*", "fastest", "good news",
    "bright spot*", "cuts emissions", "cut emissions", "saved", "saves",
    "protected", "protects", "cheap*", "affordable", "plummet*", "overtak*",
    "outpac*",
]
NEGATIVE_TERMS = [
    "dead", "dies", "died", "death*", "kills", "killed", "killing",
    "shooting", "massacre", "war", "attack*", "collapse*", "disaster*",
    "devastat*", "rollback", "roll back", "rolls back", "setback*", "worst",
    "dire", "grim", "alarming", "weaken*", "delay*", "scrap*",
]


def build_pattern(terms: list[str]) -> re.Pattern:
    """Compile a word-list into one regex. See term syntax above."""
    parts = []
    for term in terms:
        prefix = term.endswith("*")
        body = re.escape(term.rstrip("*").strip())
        body = body.replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]+")
        parts.append(body + (r"\w*" if prefix else r"(?:s|es)?"))
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)


POSITIVE_RE = build_pattern(POSITIVE_TERMS)
NEGATIVE_RE = build_pattern(NEGATIVE_TERMS)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\"'”’)]*\s+(?=[A-Z0-9“\"'‘(])")
BOILERPLATE_RE = re.compile(r"\s*The post .{0,300}? appeared first on .*$", re.IGNORECASE)
IMG_TAG_RE = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
OG_IMAGE_RES = [
    re.compile(r"<meta[^>]+property=[\"']og:image[\"'][^>]*?content=[\"']([^\"']+)[\"']", re.I),
    re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*?property=[\"']og:image[\"']", re.I),
]
IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:\?|$)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Text / URL helpers
# ---------------------------------------------------------------------------
def clean_text(raw: str) -> str:
    text = html.unescape(TAG_RE.sub(" ", raw or ""))
    text = WS_RE.sub(" ", text).strip()
    return BOILERPLATE_RE.sub("", text).strip()


def summarize(raw: str) -> str:
    """First sentence or two of the entry, plain text, capped in length."""
    text = clean_text(raw)
    if not text:
        return ""
    summary = ""
    for sentence in SENTENCE_SPLIT_RE.split(text):
        summary = f"{summary} {sentence}".strip()
        if len(summary) >= SUMMARY_TARGET_CHARS:
            break
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return summary


def clean_url(url: str) -> str:
    """Strip tracking params so the same story dedupes across runs."""
    parts = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")]
    return urlunparse(parts._replace(query=urlencode(query), fragment=""))


def is_http_url(url: str | None) -> bool:
    return bool(url) and urlparse(url).scheme in ("http", "https")


def looks_like_image(mime: str | None, medium: str | None, url: str) -> bool:
    if mime:
        return mime.lower().startswith("image")
    if medium:
        return medium.lower() == "image"
    return bool(IMAGE_EXT_RE.search(url))


def find_image(entry, base_url: str) -> str | None:
    """Best image for an entry: media tags, then enclosures, then inline <img>."""
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = media.get("url")
            if url and looks_like_image(media.get("type"), media.get("medium"), url):
                return urljoin(base_url, url)
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and link.get("href"):
            if looks_like_image(link.get("type"), None, link["href"]):
                return urljoin(base_url, link["href"])
    blobs = [c.get("value", "") for c in entry.get("content") or []]
    blobs.append(entry.get("summary", ""))
    for blob in blobs:
        match = IMG_TAG_RE.search(blob or "")
        if match:
            return urljoin(base_url, html.unescape(match.group(1)))
    return None


def entry_datetime(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return datetime.now(timezone.utc)


def is_good_news(source: dict, title: str, summary: str, tags: list[str]) -> bool:
    if source["curated"]:
        return True
    if NEGATIVE_RE.search(title):
        return False
    haystack = " ".join([title, summary, *tags])
    if POSITIVE_RE.search(haystack):
        return True
    allowed = {t.lower() for t in source.get("tag_allow", [])}
    return any(t.strip().lower() in allowed for t in tags)


# ---------------------------------------------------------------------------
# Feed processing
# ---------------------------------------------------------------------------
def process_feed(source: dict, content: bytes) -> tuple[list[dict], int]:
    """Parse raw feed bytes. Returns (kept_articles, total_entries_seen)."""
    feed = feedparser.parse(content)
    if not feed.entries and (feed.bozo or not feed.get("version")):
        raise ValueError(f"not a valid feed ({feed.get('bozo_exception') or 'no feed data found'})")

    kept = []
    for entry in feed.entries:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not is_http_url(link):
            continue

        body = ""
        if entry.get("content"):
            body = entry["content"][0].get("value", "")
        raw_summary = entry.get("summary", "") or body
        summary = summarize(raw_summary)
        tags = [t.get("term", "") for t in entry.get("tags") or []]

        if not is_good_news(source, title, summary, tags):
            continue

        image = find_image(entry, link)
        kept.append(
            {
                "title": title,
                "summary": summary,
                "link": clean_url(link),
                "image_url": image if is_http_url(image) else None,
                "source": source["name"],
                "published": entry_datetime(entry).isoformat(),
            }
        )
    return kept, len(feed.entries)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    retry = Retry(total=2, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def fetch_bytes(session: requests.Session, url: str) -> bytes:
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.content


def find_og_image(session: requests.Session, page_url: str) -> str | None:
    """Fallback: read the article page's og:image tag."""
    try:
        html_text = fetch_bytes(session, page_url)[:250_000].decode("utf-8", "ignore")
    except requests.RequestException:
        return None
    for pattern in OG_IMAGE_RES:
        match = pattern.search(html_text)
        if match:
            url = urljoin(page_url, html.unescape(match.group(1)))
            if is_http_url(url):
                return url
    return None


# ---------------------------------------------------------------------------
# Merge + output
# ---------------------------------------------------------------------------
def load_existing() -> list[dict]:
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8")).get("articles", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def merge(existing: list[dict], fresh: list[dict], now: datetime) -> list[dict]:
    known_sources = {s["name"] for s in SOURCES}
    by_link = {a["link"]: a for a in existing if a.get("source") in known_sources}
    for article in fresh:
        old = by_link.get(article["link"])
        if old and not article["image_url"]:
            article["image_url"] = old.get("image_url")  # don't lose a found image
        by_link[article["link"]] = article

    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    kept = [a for a in by_link.values() if datetime.fromisoformat(a["published"]) >= cutoff]
    kept.sort(key=lambda a: a["published"], reverse=True)
    return kept[:MAX_ARTICLES]


def build_payload(articles: list[dict], now: datetime) -> dict:
    counts: dict[str, int] = {}
    for article in articles:
        counts[article["source"]] = counts.get(article["source"], 0) + 1
    return {
        "lastUpdated": now.isoformat(),
        "sources": [
            {"name": s["name"], "site": s["site"], "color": s["color"], "count": counts.get(s["name"], 0)}
            for s in SOURCES
        ],
        "articles": articles,
    }


def run_update() -> int:
    session = make_session()
    now = datetime.now(timezone.utc)
    fresh: list[dict] = []
    failures = 0

    for source in SOURCES:
        try:
            kept, seen = process_feed(source, fetch_bytes(session, source["feed"]))
            fresh.extend(kept)
            print(f"  ok    {source['name']}: kept {len(kept)} of {seen}")
        except Exception as exc:  # one bad feed must not sink the run
            failures += 1
            print(f"  FAIL  {source['name']}: {exc}", file=sys.stderr)

    if failures == len(SOURCES):
        print("Every feed failed; leaving news.json untouched.", file=sys.stderr)
        return 1

    existing = load_existing()
    articles = merge(existing, fresh, now)

    # Look up images for stories that have none (capped, newest first).
    lookups = 0
    for article in articles:
        if lookups >= OG_LOOKUPS_PER_RUN:
            break
        if not article["image_url"]:
            lookups += 1
            article["image_url"] = find_og_image(session, article["link"])

    if [strip_volatile(a) for a in articles] == [strip_volatile(a) for a in existing]:
        print("No changes; news.json left as is.")
        return 0

    payload = build_payload(articles, now)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(articles)} stories to {OUTPUT.name}")
    return 0


def strip_volatile(article: dict) -> dict:
    return {k: article.get(k) for k in ("title", "summary", "link", "image_url", "source", "published")}


def run_check() -> int:
    """Try every feed and report; writes nothing."""
    session = make_session()
    bad = 0
    for source in SOURCES:
        print(f"\n{source['name']}  ({source['feed']})")
        try:
            kept, seen = process_feed(source, fetch_bytes(session, source["feed"]))
        except Exception as exc:
            bad += 1
            print(f"  FAIL: {exc}")
            continue
        with_img = sum(1 for a in kept if a["image_url"])
        mode = "curated, keeps all" if source["curated"] else "filtered for good news"
        print(f"  ok: {seen} entries, {len(kept)} kept ({mode}), {with_img} with images")
        for article in kept[:3]:
            print(f"    - {article['title']}")
    print(f"\n{len(SOURCES) - bad}/{len(SOURCES)} feeds working")
    return 1 if bad == len(SOURCES) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="test all feeds and exit without writing")
    args = parser.parse_args()
    sys.exit(run_check() if args.check else run_update())
