"""Daily check for new models on OpenRouter; track the ones people will talk about."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db, llm

# OpenRouter id prefix -> our lab key (only these labs are watched).
LABS = {"anthropic": "anthropic", "openai": "openai", "google": "google", "x-ai": "xai",
        "deepseek": "deepseek", "moonshotai": "moonshot", "qwen": "qwen", "z-ai": "zai",
        "meta": "meta", "meta-llama": "meta", "xiaomi": "xiaomi"}

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["decisions"],
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["openrouter_ids", "action", "slug", "name", "aliases", "reason"],
        "properties": {
            "openrouter_ids": {"type": "array", "items": {"type": "string"}},
            "action": {"type": "string", "enum": ["track", "merge", "skip"]},
            "slug": {"type": "string"},
            "name": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        }}}},
}

PROMPT = """We run a site collecting what people on X say about how LLMs behave. We track the current, popular chat/agent models of major labs. New models just appeared on OpenRouter; decide what to do with each.

## Currently tracked (slug: name; keywords)
{tracked}

## New on OpenRouter
{new}

For each new model (group ids that are the same model, e.g. a model and its "-pro" variant), return one decision:
- "track": only a new version number of a tracked family's top model (e.g. Opus 5.5 -> Opus 5.6, GPT-6 -> GPT-6.5 Sol), or a new flagship line from a lab. Be conservative: we want about 15 models in total. Give a `slug` (lowercase, hyphens), a display `name` without the lab prefix (e.g. "GPT-6 Terra", "Claude Opus 5.6"), and `aliases`: 3-6 lowercase ways people write it in tweets ("opus 5.6", "claude opus 5.6", "opus-5.6"). Every alias must contain the family name (e.g. "deepseek", "opus", "gpt") plus the version; never a bare version or tier ("v4.1 flash", "sol", "flash", "pro").
- "merge": a variant of a model already tracked (Pro/Max/Thinking/Prime/dated snapshot, or a speed/size tier such as Flash, Mini, Lite, Turbo, Omni of a tracked family). Set `slug` to the tracked slug and `aliases` to any new ways to refer to it (may be empty).
- "skip": speed/size tiers of families we don't track, embeddings, moderation/guard models, image/audio/video-only models, tiny or niche fine-tunes, "-latest" aliases, and older versions superseded by a tracked model.
Keep `reason` to a few words."""


def _now():
    return datetime.now(timezone.utc)


def check(con, force=False) -> list[str]:
    """Returns slugs of models that were added or got new keywords (recent tweets need a rescan)."""
    last = db.get_state(con, "models_checked_at")
    if last and not force and _now() - datetime.fromisoformat(last) < timedelta(hours=24):
        return []
    data = httpx.get("https://openrouter.ai/api/v1/models", timeout=60).json()["data"]
    watched = [m for m in data if m["id"].split("/")[0] in LABS and ":" not in m["id"] and not m["id"].startswith("~")]
    seen = set(json.loads(db.get_state(con, "openrouter_seen") or "[]"))
    first_run = not seen
    new = [m for m in watched if m["id"] not in seen]
    db.set_state(con, "openrouter_seen", json.dumps(sorted(seen | {m["id"] for m in watched})))
    db.set_state(con, "models_checked_at", _now().isoformat(timespec="seconds"))
    if first_run or not new:
        print(f"models: {'recorded' if first_run else 'no new'} OpenRouter models ({len(watched)} watched)")
        return []

    tracked = "\n".join(f"- {m['slug']}: {m['name']}; {', '.join(m['aliases'])}" for m in config.MODELS)
    listing = "\n".join(f"- {m['id']} | {m['name']} | released {datetime.fromtimestamp(m['created'], timezone.utc):%Y-%m-%d} | "
                        f"{(m.get('description') or '')[:300]}" for m in new)
    out = llm.chat_json(config.CLASSIFY_MODEL, PROMPT.format(tracked=tracked, new=listing), SCHEMA,
                        "new_models", max_tokens=16000)

    try:
        extra = json.loads(config.EXTRA_MODELS_PATH.read_text())
    except (OSError, ValueError):
        extra = []
    added = []
    for d in out["decisions"]:
        aliases = sorted({a.strip().lower() for a in d["aliases"] if len(a.strip()) >= 5})
        print(f"models: {d['action']:<5} {', '.join(d['openrouter_ids'])} -> {d['slug'] or '-'} ({d['reason']})")
        if d["action"] == "merge" and d["slug"] in config.MODEL_BY_SLUG and aliases:
            extra.append({"slug": d["slug"], "aliases": aliases})
            added.append(d["slug"])
        elif d["action"] == "track" and d["slug"] and aliases and d["slug"] not in config.MODEL_BY_SLUG:
            lab = LABS[d["openrouter_ids"][0].split("/")[0]]
            extra.append({"slug": d["slug"], "name": d["name"], "lab": lab, "aliases": aliases,
                          "openrouter_ids": d["openrouter_ids"], "added_at": _now().date().isoformat()})
            added.append(d["slug"])
    config.EXTRA_MODELS_PATH.write_text(json.dumps(extra, indent=2))
    config.load_models()
    return added
