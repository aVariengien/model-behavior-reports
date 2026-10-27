# Model Behavior Reports

Crowdsourced LLM behaviors from X, built from the [Community Archive](https://www.community-archive.org).
Live at **[modelbehavior.report](https://modelbehavior.report)**.

Benchmarks measure what language models can do. This site collects what people notice about how they
*behave*: their tendencies, quirks and personality, as reported on X.

## How it works

Every hour, `mbr update`:

1. **Scans** tweets created since the last run (re-reading the last 6 hours, for tweets that reach the archive late)
   through the [Community Archive API](https://analytics.community-archive.org/v1/openapi.json), and matches model
   keywords locally (`mbr/config.py`). When an account uploads an archive, its last 180 days are scanned too.
2. **Hydrates** each match with its parent and quoted tweet; replies and quote tweets of known reports are collected
   as the scan passes them (the API can't list them). Playable video URLs and missing author info come from X's
   public embed endpoint (`mbr/xmedia.py`).
3. **Grades** each candidate with Gemini 3.8 Flash (text + screenshots): is it a *behavior* report, which models
   does it really discuss, and how interesting is it (1-10) (`mbr/classify.py`).
4. **Counts quotes**: distinct archive accounts that quote-tweeted each report.
5. **Summarizes** each model with Claude Opus 5.5, in one batch a day (first run after 04:00 UTC), only for models with new reports (`mbr/summarize.py`).
   This runs through the Claude Code CLI (`claude -p`) on a logged-in Claude subscription, so it has no per-token cost.
6. **Checks OpenRouter** once a day for new models and adds them with generated keywords (`mbr/newmodels.py`).
7. **Builds** a static site plus plain-text exports for LLMs (`/llms.txt`, `/llms-full.txt`, `/data/<model>.md`).

Reports are ranked by `50 × quotes + 20 × log2(1 + min(retweets, 100)) + 15 × interestingness`.

## Running it

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). LLM calls go through [OpenRouter](https://openrouter.ai),
except summaries, which need [Claude Code](https://claude.com/claude-code) logged in (`claude` on `PATH`, or `CLAUDE_BIN`).

```bash
uv sync
export OPENROUTER_API_KEY=...
export CA_API_KEY=cak_...         # from community-archive.org/settings/api-keys
uv run mbr backfill --days 180   # scan the archive (~20k API calls: a day's quota, ~6 h)
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
which syncs the code to the server. Secrets (`OPENROUTER_API_KEY`, `CA_API_KEY`, optional `MBR_UMAMI_ID` for analytics) go in
`secrets.env` next to the code, which is never committed.

## Data and credits

Tweets come from the [Community Archive](https://www.community-archive.org), an open, opt-in archive of X,
through its API. Made by [Alexandre Variengien](https://alexandrevariengien.com). MIT licensed.
