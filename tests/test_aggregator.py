import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import news_aggregator as agg  # noqa: E402

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"
     xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Test</title><link>https://example.org</link>
<item>
  <title>Solar &amp; wind hit record share of UK power</title>
  <link>https://example.org/a?utm_source=rss&amp;id=7</link>
  <pubDate>Mon, 21 Sep 2026 08:00:00 +0000</pubDate>
  <description><![CDATA[<p>Renewables set a new record this summer. Analysts say it is the start of a trend. More text follows here.</p><p>The post Solar appeared first on Example.</p>]]></description>
  <media:content url="https://cdn.example.org/a.jpg" medium="image"/>
</item>
<item>
  <title>Wildfire kills dozens as region burns</title>
  <link>https://example.org/b</link>
  <pubDate>Mon, 21 Sep 2026 07:00:00 +0000</pubDate>
  <description>Record heat and devastation.</description>
</item>
<item>
  <title>Council debates new bus timetable</title>
  <link>https://example.org/c</link>
  <pubDate>Mon, 21 Sep 2026 06:00:00 +0000</pubDate>
  <description>Nothing much happened.</description>
</item>
<item>
  <title>Beavers return to the river after 400 years</title>
  <link>https://example.org/d</link>
  <pubDate>Sun, 20 Sep 2026 06:00:00 +0000</pubDate>
  <content:encoded><![CDATA[<img src="/images/beaver.png" width="600"><p>Wildlife recovery is going well.</p>]]></content:encoded>
</item>
<item><title>No link here</title></item>
</channel></rss>"""


class TermMatching(unittest.TestCase):
    def test_whole_words_only(self):
        pattern = agg.build_pattern(["win", "restor*"])
        self.assertTrue(pattern.search("A big win for rivers"))
        self.assertFalse(pattern.search("The window was open"))
        self.assertTrue(pattern.search("Wetlands restoration begins"))

    def test_plural_and_hyphen(self):
        pattern = agg.build_pattern(["heat pump", "record-breaking"])
        self.assertTrue(pattern.search("Heat pumps outsell gas boilers"))
        self.assertTrue(pattern.search("A record breaking year"))


class HelperFunctions(unittest.TestCase):
    def test_summarize_strips_html_and_boilerplate(self):
        text = "<p>This first sentence is deliberately long enough to pass the target length on its own, so nothing else is added. Second one.</p>The post X appeared first on Y."
        self.assertEqual(
            agg.summarize(text),
            "This first sentence is deliberately long enough to pass the target length on its own, so nothing else is added.",
        )

    def test_summarize_short_first_sentence_adds_second(self):
        self.assertEqual(agg.summarize("Short one. And a second sentence."), "Short one. And a second sentence.")

    def test_clean_url_drops_tracking_only(self):
        self.assertEqual(agg.clean_url("https://x.org/a?utm_source=rss&id=7#top"), "https://x.org/a?id=7")

    def test_only_http_urls(self):
        self.assertTrue(agg.is_http_url("https://x.org"))
        self.assertFalse(agg.is_http_url("javascript:alert(1)"))
        self.assertFalse(agg.is_http_url(None))


class FeedProcessing(unittest.TestCase):
    curated = {"name": "T", "curated": True}
    mixed = {"name": "T", "curated": False}

    def test_curated_keeps_everything_with_a_link(self):
        kept, seen = agg.process_feed(self.curated, RSS)
        self.assertEqual(seen, 5)
        self.assertEqual(len(kept), 4)  # entry with no link is dropped

    def test_mixed_filters_for_good_news(self):
        kept, _ = agg.process_feed(self.mixed, RSS)
        titles = [a["title"] for a in kept]
        self.assertIn("Solar & wind hit record share of UK power", titles)
        self.assertIn("Beavers return to the river after 400 years", titles)
        self.assertNotIn("Wildfire kills dozens as region burns", titles)  # negative title
        self.assertNotIn("Council debates new bus timetable", titles)       # nothing positive

    def test_fields_and_images(self):
        kept, _ = agg.process_feed(self.curated, RSS)
        first = next(a for a in kept if a["title"].startswith("Solar"))
        self.assertEqual(first["link"], "https://example.org/a?id=7")
        self.assertEqual(first["image_url"], "https://cdn.example.org/a.jpg")
        self.assertTrue(first["published"].startswith("2026-09-21T08:00"))
        self.assertNotIn("appeared first on", first["summary"])
        beaver = next(a for a in kept if a["title"].startswith("Beavers"))
        self.assertEqual(beaver["image_url"], "https://example.org/images/beaver.png")  # inline <img>, made absolute

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            agg.process_feed(self.curated, b"<html>not a feed</html>")


FUTURISM_RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Futurism</title><link>https://futurism.com</link>
<item>
  <title>Solar Power Is Getting So Cheap That It\xe2\x80\x99s Almost Unbelievable</title>
  <link>https://futurism.com/science-energy/solar-power-renewable-energy-incredibly-cheap</link>
  <pubDate>Sun, 20 Sep 2026 22:01:00 +0000</pubDate>
  <category>Energy</category><category>Renewable Energy</category><category>Solar Power</category>
  <description>The cost of solar panels has dropped to just a few cents per watt, thanks to a massive supply of photovoltaics flooding the market.</description>
</item>
<item>
  <title>Teen Boys Are Using Meta Glasses to Harass and Bully Girls</title>
  <link>https://futurism.com/artificial-intelligence/teen-boys-meta-glasses</link>
  <pubDate>Sun, 20 Sep 2026 20:00:00 +0000</pubDate>
  <category>Ethics</category>
  <description>Awful behaviour.</description>
</item>
<item>
  <title>New wind farm stalls in permitting limbo</title>
  <link>https://futurism.com/science-energy/wind-limbo</link>
  <pubDate>Sun, 20 Sep 2026 19:00:00 +0000</pubDate>
  <category>Renewable Energy</category>
  <description>Nothing is moving.</description>
</item>
<item>
  <title>Regulators scrap solar subsidy in setback for industry</title>
  <link>https://futurism.com/science-energy/subsidy-scrapped</link>
  <pubDate>Sun, 20 Sep 2026 18:00:00 +0000</pubDate>
  <category>Solar Power</category>
  <description>Bad day.</description>
</item>
</channel></rss>"""


class FuturismSource(unittest.TestCase):
    source = next(s for s in agg.SOURCES if s["name"] == "Futurism")

    def test_keeps_the_solar_story_drops_the_ai_story(self):
        kept, seen = agg.process_feed(self.source, FUTURISM_RSS)
        titles = [a["title"] for a in kept]
        self.assertEqual(seen, 4)
        self.assertIn("Solar Power Is Getting So Cheap That It\u2019s Almost Unbelievable", titles)
        self.assertFalse(any("Meta Glasses" in t for t in titles))

    def test_green_tag_alone_can_qualify_but_negatives_still_veto(self):
        kept, _ = agg.process_feed(self.source, FUTURISM_RSS)
        titles = [a["title"] for a in kept]
        self.assertIn("New wind farm stalls in permitting limbo", titles)  # tag route
        self.assertNotIn("Regulators scrap solar subsidy in setback for industry", titles)  # negative title


class Merge(unittest.TestCase):
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)

    def article(self, link, days_old, image="img", source=None):
        return {
            "title": link, "summary": "", "link": link, "image_url": image,
            "source": source or agg.SOURCES[0]["name"],
            "published": (self.now - timedelta(days=days_old)).isoformat(),
        }

    def test_dedupe_keeps_found_image_and_sorts(self):
        existing = [self.article("a", 3, image="found.jpg"), self.article("b", 1)]
        fresh = [self.article("a", 3, image=None)]
        merged = agg.merge(existing, fresh, self.now)
        self.assertEqual([a["link"] for a in merged], ["b", "a"])
        self.assertEqual(merged[1]["image_url"], "found.jpg")

    def test_drops_old_and_unknown_sources(self):
        existing = [self.article("old", agg.MAX_AGE_DAYS + 1), self.article("gone", 1, source="Removed Feed")]
        self.assertEqual(agg.merge(existing, [], self.now), [])


if __name__ == "__main__":
    unittest.main()
