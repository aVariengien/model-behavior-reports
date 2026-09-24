"""Models tracked, their keywords, and pipeline knobs."""

import json
import math
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("MBR_ROOT", Path(__file__).resolve().parent.parent))
DB_PATH = Path(os.environ.get("MBR_DB", ROOT / "data" / "mbr.sqlite"))
OUT_DIR = Path(os.environ.get("MBR_OUT", ROOT / "dist"))

SITE_URL = "https://modelbehavior.report"
GITHUB_URL = "https://github.com/aVariengien/model-behavior-reports"
UMAMI_SCRIPT = "https://analytics.kenno.space/script.js"
UMAMI_WEBSITE_ID = os.environ.get("MBR_UMAMI_ID", "")

CA_URL = "https://fabxmporizzqflnftavs.supabase.co/rest/v1/"
# Public anon key, published in https://www.community-archive.org/llms.txt
CA_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZhYnhtcG9yaXp6cWZsbmZ0YXZzIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3MjIyNDQ5MTIsImV4cCI6MjAzNzgyMDkxMn0.UIEJiUNkLsW28tBHmG-RQDW-I5JNlJLt62CSk9D_qG8"
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CLASSIFY_MODEL = "google/gemini-3.8-flash"
SUMMARY_MODEL = "anthropic/claude-opus-5.5"
CLASSIFY_MODEL_NAME = "Gemini 3.8 Flash"
SUMMARY_MODEL_NAME = "Claude Opus 5.5"

WINDOW_DAYS = 180          # only keep tweets younger than this
MAX_REPLIES = 10           # replies kept per report (most liked)
CLASSIFY_PER_RUN = 400     # cap per hourly run, newest first; backfill drains gradually
MIN_SCORE = 4              # interestingness needed to be shown on the site
MIN_MODEL_REPORTS = 5      # models with fewer shown reports are hidden
SUMMARY_EVERY_HOURS = 24   # regenerate a model summary at most this often
SUMMARY_MIN_NEW = 3        # ...and only if at least this many new reports arrived
SUMMARY_MAX_REPORTS = 150  # reports fed to the summarizer per model

# Ranking: quotes by archive accounts dominate; retweets count on a log scale
# that saturates at RANK_RT_CAP; the classifier's interestingness breaks ties.
RANK_QUOTE = 50
RANK_RT = 20
RANK_RT_CAP = 100
RANK_SCORE = 15


def rank(quotes, retweets, score) -> float:
    return (RANK_QUOTE * (quotes or 0)
            + RANK_RT * math.log2(1 + min(retweets or 0, RANK_RT_CAP))
            + RANK_SCORE * (score or 0))


# Accent colour per lab; the only colour on the page besides text.
LABS = {
    "anthropic": "#c2562f",
    "openai": "#0b8062",
    "google": "#2a64d6",
    "xai": "#55555c",
    "deepseek": "#4655d4",
    "moonshot": "#7a45d6",
    "qwen": "#b5249f",
    "zai": "#0b7f76",
    "meta": "#0a74b0",
    "xiaomi": "#d4560b",
}

# Pro / non-Pro variants are merged into one entry. Aliases are matched
# case-insensitively; spaces, hyphens and underscores are interchangeable.
BASE_MODELS = [
    dict(slug="claude-opus-5-5", name="Claude Opus 5.5", lab="anthropic",
         aliases=["opus 5.5", "opus5.5", "claude-opus-5-5"]),
    dict(slug="claude-fable-5-1", name="Claude Fable 5.1", lab="anthropic",
         aliases=["fable 5.1", "fable5.1", "claude fable", "claude-fable-5-1"]),
    dict(slug="claude-sonnet-5", name="Claude Sonnet 5", lab="anthropic",
         aliases=["sonnet 5", "sonnet5", "claude-sonnet-5"]),
    dict(slug="gpt-6-sol", name="GPT-6 Sol", lab="openai",
         aliases=["gpt-6 sol", "gpt6 sol", "gpt sol", "gpt-6-sol-pro", "sol pro"]),
    dict(slug="gpt-6-luna", name="GPT-6 Luna", lab="openai",
         aliases=["gpt-6 luna", "gpt6 luna", "gpt luna", "luna pro"]),
    dict(slug="gpt-6-astra", name="GPT-6 Astra", lab="openai",
         aliases=["gpt-6 astra", "gpt6 astra", "gpt astra", "astra pro"]),
    dict(slug="gpt-5-6-terra", name="GPT-5.6 Terra", lab="openai",
         aliases=["gpt terra", "gpt-5.6 terra", "gpt5.6 terra", "5.6 terra", "terra pro"]),
    dict(slug="gemini-3-8-flash", name="Gemini 3.8 Flash", lab="google",
         aliases=["gemini 3.8", "gemini3.8", "gemini-3.8-flash"]),
    dict(slug="grok-4-7", name="Grok 4.7", lab="xai",
         aliases=["grok 4.7", "grok4.7", "grok-4-7"]),
    dict(slug="deepseek-v4", name="DeepSeek V4", lab="deepseek",
         aliases=["deepseek v4", "deepseekv4", "deepseek v4.1", "deepseek-v4-pro", "deepseek v4 pro"]),
    dict(slug="kimi-k3", name="Kimi K3", lab="moonshot",
         aliases=["kimi k3", "kimik3", "kimi-k3"]),
    dict(slug="qwen-3-8", name="Qwen3.8 Max", lab="qwen",
         aliases=["qwen3.8", "qwen 3.8", "qwen-3.8", "qwen3.8 max"]),
    dict(slug="glm-5-3", name="GLM 5.3", lab="zai",
         aliases=["glm 5.3", "glm5.3", "glm-5-3"]),
    dict(slug="muse-spark", name="Muse Spark 1.3", lab="meta",
         aliases=["muse spark", "musespark", "muse-spark"]),
    dict(slug="mimo-v2-6", name="MiMo V2.6", lab="xiaomi",
         aliases=["mimo v2.6", "mimo-v2.6", "mimo 2.6", "mimo v2.6 pro"]),
]
# Models added automatically from OpenRouter (see newmodels.py) live next to the DB:
# a list of full model dicts, or {"slug": <existing>, "aliases": [...]} to extend one.
EXTRA_MODELS_PATH = DB_PATH.parent / "models.json"


def _alias_pattern(alias: str) -> str:
    parts = re.split(r"[\s\-_]+", alias.lower())
    body = r"[\s\-_]?".join(re.escape(p) for p in parts)
    # No letter/digit before; after, forbid more version digits ("sonnet 5.1", "opus 5.55").
    return rf"(?<![a-z0-9]){body}(?![a-z0-9]|[.\-]\d)"


def load_models():
    """(Re)build MODELS, MODEL_BY_SLUG and MODEL_PATTERNS from the base list plus models.json."""
    global MODELS, MODEL_BY_SLUG, MODEL_PATTERNS
    models = [dict(m, aliases=list(m["aliases"])) for m in BASE_MODELS]
    by_slug = {m["slug"]: m for m in models}
    try:
        extra = json.loads(EXTRA_MODELS_PATH.read_text())
    except (OSError, ValueError):
        extra = []
    for e in extra:
        if e["slug"] in by_slug:
            by_slug[e["slug"]]["aliases"] += [a for a in e.get("aliases", []) if a not in by_slug[e["slug"]]["aliases"]]
        elif e.get("name"):
            m = dict(e)
            models.append(m)
            by_slug[m["slug"]] = m
    for m in models:
        m["color"] = LABS.get(m["lab"], "#55555c")
    MODELS, MODEL_BY_SLUG = models, by_slug
    MODEL_PATTERNS = {m["slug"]: re.compile("|".join(_alias_pattern(a) for a in m["aliases"]), re.I)
                      for m in models}


load_models()


def match_models(text: str) -> list[str]:
    return [slug for slug, pat in MODEL_PATTERNS.items() if pat.search(text or "")]
