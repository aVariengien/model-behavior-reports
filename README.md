# Model Behavior Reports

Crowdsourced LLM behaviors from X, built from the [Community Archive](https://www.community-archive.org).
Live at **[modelbehavior.report](https://modelbehavior.report)**.

Benchmarks measure what language models can do. This site collects what people notice about how they
*behave*: their tendencies, quirks and personality, as reported on X.

## How it works

Every hour, `mbr update`:

1. **Scans** tweets inserted or updated in the Community Archive since the last run (by `updated_at`, so late
   archive uploads are caught) and matches model keywords locally (`mbr/config.py`).
2. **Hydrates** each match with its parent tweet, quoted tweet, top 10 replies and media; resolves playable video
   URLs and missing author info from X's public embed endpoint (`mbr/xmedia.py`).
3. **Grades** each candidate with Gemini 3.8 Flash (text + screenshots): is it a *behavior* report, which models
   does it really discuss, and how interesting is it (1-10) (`mbr/classify.py`).
4. **Counts quotes**: distinct archive accounts that quote-tweeted each report.
5. **Summarizes** each model with Claude Opus 5.5, at most daily and only when new reports arrived (`mbr/summarize.py`).
6. **Checks OpenRouter** once a day for new models and adds them with generated keywords (`mbr/newmodels.py`).
7. **Builds** a static site plus plain-text exports for LLMs (`/llms.txt`, `/llms-full.txt`, `/data/<model>.md`).

Reports are ranked by `50 × quotes + 20 × log2(1 + min(retweets, 100)) + 15 × interestingness`.

## Running it

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). All LLM calls go through
[OpenRouter](https://openrouter.ai).

```bash
uv sync
export OPENROUTER_API_KEY=...
uv run mbr backfill --days 180   # scan the archive (free, ~15 min)
uv run mbr classify              # grade candidates (~$0.0012 per tweet)
uv run mbr quotes && uv run mbr videos
uv run mbr summarize
uv run mbr build                 # static site in dist/
uv run mbr update                # the hourly job: all of the above, incrementally
uv run mbr stats
```

Settings (tracked models, score threshold, ranking weights, summary cadence) are at the top of `mbr/config.py`.
Data lives in `data/mbr.sqlite`; models added automatically are stored in `data/models.json`.
Add `?debug=1` to any page to see each tweet's grade.

## Deployment

`deploy/` holds the setup used for modelbehavior.report: a systemd service and hourly timer
(`MBR_OUT` points the build at the web root), a Caddy site block serving the static files, and `deploy.sh`,
which syncs the code to the server. Secrets (`OPENROUTER_API_KEY`, optional `MBR_UMAMI_ID` for analytics) go in
`secrets.env` next to the code, which is never committed.

## Data and credits

Tweets come from the [Community Archive](https://www.community-archive.org), an open, opt-in archive of X,
through its public API. Made by [Alexandre Variengien](https://alexandrevariengien.com). MIT licensed.
