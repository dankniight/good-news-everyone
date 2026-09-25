# Good News, Everyone

A static good-news site. A Python script gathers positive climate, nature and
community stories from RSS feeds into `news.json`; `index.html` renders it.
Same shape as the tech aggregator, different beat.

```
RSS feeds ──> src/news_aggregator.py ──> news.json ──> index.html (GitHub Pages)
                    ^
       GitHub Action, every 4 hours
```

## Run it

```bash
pip install -r requirements.txt
python src/news_aggregator.py --check   # test every feed, writes nothing
python src/news_aggregator.py           # fetch and update news.json
python -m http.server 8000              # then open http://localhost:8000
```

(Open it through a server, not by double-clicking: browsers block `fetch`
of local files.)

## Deploy

1. Push to GitHub, then Settings > Pages > deploy from your branch (root).
2. Settings > Actions > General > Workflow permissions > **Read and write**.
3. Edit `USER_AGENT` in `src/news_aggregator.py` to point at your repo.

The workflow in `.github/workflows/update.yml` runs the tests, refreshes the
stories, and commits `news.json` only when something changed.

## Sources and the "is it good news?" filter

Edit `SOURCES` in `src/news_aggregator.py`.

- `curated: True`: the whole publication is positive (Good News Network,
  Positive News, Reasons to be Cheerful, The Optimist Daily). Keep everything.
- `curated: False`: mixed publications (Grist, Canary Media, Futurism). A story
  is kept if its title/summary/tags match `POSITIVE_TERMS`, and dropped if its
  title matches `NEGATIVE_TERMS`. Optional `tag_allow` lists feed categories
  that count as a positive signal by themselves.

This is keyword matching, so it will occasionally let a dull story through or
miss a good one. Tune the two term lists as you watch what lands.

## Front-end

Topic chips (`TOPICS` in `index.html`) match by whole word against each story's
title and summary, so adding a topic means adding a word list. The chosen topic
is kept in the URL hash (`#nature`) so it can be shared.

Stories are shown as a headline and short summary with a link to the publisher.
Please keep it that way and don't republish full text.
