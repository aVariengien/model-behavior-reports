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
CLASSIFY_REASONING = "low"  # A/B test on 50 graded tweets: 49/50 agree, ~half the cost
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
# `aliases` name the exact version and always count. `generic` are family names without a
# version ("claude fable"): they count for this model only in tweets posted after `released`
# (the newest tracked version at the time wins), and never when followed by another version.
# `released` = earlier of the OpenRouter listing and the 5th explicit mention in the archive.
BASE_MODELS = [
    dict(slug="claude-opus-5-5", name="Claude Opus 5.5", lab="anthropic", released="2026-09-20",
         aliases=["opus 5.5", "opus5.5", "claude-opus-5-5"], generic=["claude opus"]),
    dict(slug="claude-fable-5-1", name="Claude Fable 5.1", lab="anthropic", released="2026-07-09",
         aliases=["fable 5.1", "fable5.1", "claude-fable-5-1"], generic=["claude fable"]),
    dict(slug="claude-opus-5", name="Claude Opus 5", lab="anthropic", released="2026-07-24",
         aliases=["opus 5", "opus5", "claude-opus-5"], generic=["claude opus"]),
    dict(slug="claude-fable-5", name="Claude Fable 5", lab="anthropic", released="2026-06-09",
         aliases=["fable 5", "fable5", "claude-fable-5"], generic=["claude fable"]),
    dict(slug="claude-sonnet-5", name="Claude Sonnet 5", lab="anthropic", released="2026-04-21",
         aliases=["sonnet 5", "sonnet5", "claude-sonnet-5"], generic=["claude sonnet"]),
    dict(slug="gpt-6-sol", name="GPT-6 Sol", lab="openai", released="2026-09-15",
         aliases=["gpt-6 sol", "gpt6 sol", "gpt 6 sol", "gpt-6-sol-pro"], generic=["gpt sol", "sol pro"]),
    dict(slug="gpt-5-6-sol", name="GPT-5.6 Sol", lab="openai", released="2026-07-09",
         aliases=["gpt-5.6 sol", "gpt5.6 sol", "gpt 5.6 sol", "5.6 sol", "gpt-5.6-sol-pro"], generic=["gpt sol", "sol pro"]),
    dict(slug="gpt-6-luna", name="GPT-6 Luna", lab="openai", released="2026-09-19",
         aliases=["gpt-6 luna", "gpt6 luna", "gpt 6 luna"], generic=["gpt luna", "luna pro"]),
    dict(slug="gpt-6-astra", name="GPT-6 Astra", lab="openai", released="2026-09-02",
         aliases=["gpt-6 astra", "gpt6 astra", "gpt 6 astra"], generic=["gpt astra", "astra pro"]),
    dict(slug="gpt-5-6-terra", name="GPT-5.6 Terra", lab="openai", released="2026-06-30",
         aliases=["gpt-5.6 terra", "gpt5.6 terra", "5.6 terra"], generic=["gpt terra", "terra pro"]),
    dict(slug="gemini-3-8-flash", name="Gemini 3.8 Flash", lab="google", released="2026-08-31",
         aliases=["gemini 3.8", "gemini3.8", "gemini-3.8-flash"], generic=["gemini flash"]),
    dict(slug="gemini-3-5-flash", name="Gemini 3.5 Flash", lab="google", released="2026-05-19",
         aliases=["gemini 3.5 flash", "gemini3.5 flash", "gemini-3.5-flash"], generic=["gemini flash"]),
    dict(slug="gemini-3-1-pro", name="Gemini 3.1 Pro", lab="google", released="2026-02-19",
         aliases=["gemini 3.1 pro", "gemini3.1 pro", "gemini-3.1-pro"], generic=["gemini pro"]),
    dict(slug="grok-4-7", name="Grok 4.7", lab="xai", released="2026-08-12",
         aliases=["grok 4.7", "grok4.7", "grok-4-7"], generic=[]),
    dict(slug="deepseek-v4", name="DeepSeek V4", lab="deepseek", released="2026-03-30",
         aliases=["deepseek v4", "deepseekv4", "deepseek v4.1", "deepseek-v4-pro", "deepseek v4 pro"], generic=[]),
    dict(slug="kimi-k3", name="Kimi K3", lab="moonshot", released="2026-06-13",
         aliases=["kimi k3", "kimik3", "kimi-k3"], generic=[]),
    dict(slug="qwen-3-8", name="Qwen3.8 Max", lab="qwen", released="2026-07-19",
         aliases=["qwen3.8", "qwen 3.8", "qwen-3.8", "qwen3.8 max"], generic=[]),
    dict(slug="glm-5-3", name="GLM 5.3", lab="zai", released="2026-07-25",
         aliases=["glm 5.3", "glm5.3", "glm-5-3"], generic=[]),
    dict(slug="muse-spark", name="Muse Spark 1.3", lab="meta", released="2026-09-02",
         aliases=["muse spark 1.3", "spark 1.3", "muse-spark-1.3"], generic=["muse spark"]),
    dict(slug="mimo-v2-6", name="MiMo V2.6", lab="xiaomi", released="2026-09-21",
         aliases=["mimo v2.6", "mimo-v2.6", "mimo 2.6", "mimo v2.6 pro"], generic=[]),
]
# Models added automatically from OpenRouter (see newmodels.py) and keyword edits made on
# /status/ live next to the DB, in a list of entries:
#   {"slug", "name", "lab", "released", "aliases", "generic"}  a new model
#   {"slug", "aliases": [...]}                                 extra aliases for a model
#   {"slug", "replace": true, "aliases", "generic", "released"}  hand edit (wins)
EXTRA_MODELS_PATH = DB_PATH.parent / "models.json"


def _alias_body(alias: str) -> str:
    parts = re.split(r"[\s\-_]+", alias.lower())
    return r"(?<![a-z0-9])" + r"[\s\-_]?".join(re.escape(p) for p in parts)


def _alias_pattern(alias: str) -> str:
    # After the alias, forbid more version digits ("sonnet 5.1", "opus 5.55").
    return _alias_body(alias) + r"(?![a-z0-9]|[.\-]\d)"


def _generic_pattern(alias: str) -> str:
    # A family name must not be followed by any version: "claude fable 5" is not generic.
    return _alias_body(alias) + r"(?![a-z0-9]|[\s\-_.]?v?\d)"


def load_models():
    """(Re)build MODELS, MODEL_BY_SLUG and the keyword patterns from the base list plus models.json."""
    global MODELS, MODEL_BY_SLUG, MODEL_PATTERNS, GENERIC
    models = [dict(m, aliases=list(m["aliases"]), generic=list(m.get("generic", []))) for m in BASE_MODELS]
    by_slug = {m["slug"]: m for m in models}
    try:
        extra = json.loads(EXTRA_MODELS_PATH.read_text())
    except (OSError, ValueError):
        extra = []
    for e in extra:
        m = by_slug.get(e["slug"])
        if m and e.get("replace"):
            m.update({k: e[k] for k in ("aliases", "generic", "released") if k in e})
        elif m:
            m["aliases"] += [a for a in e.get("aliases", []) if a not in m["aliases"]]
        elif e.get("name"):
            m = dict(e, generic=list(e.get("generic", [])))
            models.append(m)
            by_slug[m["slug"]] = m
    for m in models:
        m["color"] = LABS.get(m["lab"], "#55555c")
        m.setdefault("released", "2000-01-01")
    MODELS, MODEL_BY_SLUG = models, by_slug
    MODEL_PATTERNS = {m["slug"]: re.compile("|".join(_alias_pattern(a) for a in m["aliases"]), re.I)
                      for m in models if m["aliases"]}
    # Models sharing any family name form one family ("opus" and "claude opus" both point at
    # Opus 5 and 5.5): a hit on any of its names resolves to the family's newest version out.
    parent = {m["slug"]: m["slug"] for m in models}

    def root(x):
        while parent[x] != x:
            x = parent[x]
        return x

    owner: dict[str, str] = {}
    for m in models:
        for g in m["generic"]:
            g = g.lower()
            if g in owner:
                parent[root(m["slug"])] = root(owner[g])
            owner.setdefault(g, m["slug"])
    family = {}
    for m in models:
        family.setdefault(root(m["slug"]), []).append((m["released"], m["slug"]))
    GENERIC = [(re.compile(_generic_pattern(g), re.I), sorted(family[root(slug)], reverse=True))
               for g, slug in owner.items()]


load_models()


def match_models(text: str, created_at: str | None = None) -> list[str]:
    """Tracked models a tweet mentions. Family names ("claude fable") resolve to the newest
    tracked version released on or before the tweet's date, or to nothing."""
    text = text or ""
    found = {slug for slug, pat in MODEL_PATTERNS.items() if pat.search(text)}
    day = (created_at or "9999")[:10]
    explicit = set(found)
    for pat, versions in GENERIC:
        # A tweet that names a version of this family explicitly ("GPT-5.6 Sol Pro") isn't
        # also a family-name mention of another version ("sol pro").
        if explicit & {slug for _, slug in versions}:
            continue
        if pat.search(text):
            slug = next((s for released, s in versions if released <= day), None)
            if slug:
                found.add(slug)
    return sorted(found)
