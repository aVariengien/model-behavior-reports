"""Grade each candidate: is it a behaviour report, and how interesting is it."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import config, llm, runlog

KINDS = ["behavior_report", "tendency", "commentary", "capability", "benchmark",
         "news", "hype", "joke", "other"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["models", "kind", "is_behavior_report", "interestingness", "behavior"],
    "properties": {
        "models": {"type": "array", "items": {"type": "string"},
                   "description": "Slugs of the tracked models the tweet actually talks about."},
        "kind": {"type": "string", "enum": KINDS},
        "is_behavior_report": {"type": "boolean"},
        "interestingness": {"type": "integer", "minimum": 1, "maximum": 10},
        "behavior": {"type": "string",
                     "description": "The behaviour in at most 12 words, e.g. 'Hallucinated a user message, then solved it'. Empty if none."},
    },
}

PROMPT = """You are curating a site that collects crowdsourced reports of how LLMs *behave*: their tendencies, quirks and personality, as reported by people on X.

## What counts as a behaviour report (is_behavior_report = true)
Reports of how a model acts, not how capable it is:
- a precise event ("it hallucinated a user message I never wrote and solved that task", "it refused to talk about its consciousness", "it asked for cat facts unprompted")
- a general feeling about a tendency, even without a concrete event ("X is really good at pushing back when I'm wrong", "Y is so sycophantic", "Z feels anxious, like it suspects every question is a test")
- commentary on a behaviour reported elsewhere (another tweet, a system card, a paper) that says something about the model's character
When a tweet mixes capability and behaviour, it counts as a behaviour report if the model did something unprompted, took initiative, expressed a preference, an emotion or a view of itself, or acted in a way the user did not expect ("it started making spectrograms on its own to hear its work", "I asked if it wanted its own project and it was delighted").
Not behaviour reports: pure capability or benchmark claims ("it one-shot my app", "SOTA on SWE-bench"), release news, pricing, hype, generic takes, jokes that only use the model name.

## Interestingness (1-10)
How much the reported behaviour changes how someone perceives LLMs. Score high for: spontaneous actions or sentences that seem unexpected or out of place given the context, the model being funny, the user showing genuine surprise, emotionally or ethically loaded behaviour, well-documented or vivid episodes. Score low for: vague praise or complaints, well-known behaviours described with no new detail. Anything that is not a behaviour report gets 1-2.

## Models tracked
{models}
Keyword matching suggested: {candidates}. Return in `models` only slugs the tweet really talks about (it may be a false keyword match, e.g. "sol pro" meaning something else). Return [] if none.

## The tweet, with its context
{context}

Screenshots attached to the tweet (if any) follow; they often contain the actual conversation with the model."""


def fmt_tweet(t, label):
    if not t:
        return ""
    return f"[{label}] @{t['username']}: {t['full_text']}\n"


def context_for(con, report) -> tuple[str, list[str]]:
    """Parent + tweet + quoted tweet + replies, as text, plus image URLs."""
    get = lambda tid: tid and con.execute("SELECT * FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
    t = get(report["tweet_id"])
    if not t:
        return "", []
    parent, quoted = get(t["reply_to_tweet_id"]), get(t["quoted_tweet_id"])
    parts = []
    if parent:
        parts.append(fmt_tweet(parent, "tweet it replies to"))
    elif t["reply_to_username"]:
        parts.append(f"[in reply to @{t['reply_to_username']}, tweet unavailable]\n")
    parts.append(fmt_tweet(t, "THE TWEET"))
    if quoted:
        parts.append(fmt_tweet(quoted, "tweet it quotes"))
    for rid in json.loads(report["reply_ids"] or "[]"):
        parts.append(fmt_tweet(get(rid), "reply"))
    images = [m["url"] for src in (t, quoted) if src
              for m in json.loads(src["media"] or "[]") if m["type"] == "photo"][:4]
    return "".join(parts), images


def classify_one(report, text: str, images: list[str]) -> dict | None:
    if not text:
        return None
    models_desc = "\n".join(f"- {m['slug']}: {m['name']}" for m in config.MODELS)
    prompt = PROMPT.format(models=models_desc, candidates=report["candidates"], context=text)
    content = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": f"{u}?name=small"}} for u in images]
    try:
        return llm.chat_json(config.CLASSIFY_MODEL, content, SCHEMA, "report_grade", purpose="grading")
    except RuntimeError:
        if not images:
            raise
        # An expired image URL can fail the whole call; retry text-only.
        return llm.chat_json(config.CLASSIFY_MODEL, content[:1], SCHEMA, "report_grade", purpose="grading")


def classify(con, limit: int | None = None, workers: int = 12):
    rows = con.execute(
        "SELECT * FROM reports WHERE is_report IS NULL AND hydrated=1 ORDER BY created_at DESC"
        + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    if not rows:
        return
    print(f"classifying {len(rows)} candidates with {config.CLASSIFY_MODEL}", flush=True)
    done = failed = 0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(classify_one, r, *context_for(con, r)): r for r in rows}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                g = fut.result()
            except Exception as e:  # noqa: BLE001 — one bad tweet must not stop the run
                failed += 1
                print(f"  ! {r['tweet_id']}: {e}", flush=True)
                continue
            if g is None:  # tweet vanished from the archive
                con.execute("UPDATE reports SET is_report=0, kind='other', score=0 WHERE tweet_id=?",
                            (r["tweet_id"],))
                continue
            models = [m for m in g["models"] if m in config.MODEL_BY_SLUG]
            con.execute(
                "UPDATE reports SET models=?, kind=?, is_report=?, score=?, behavior=?, classified_at=? "
                "WHERE tweet_id=?",
                (json.dumps(models), g["kind"], int(bool(g["is_behavior_report"] and models)),
                 g["interestingness"], g["behavior"].strip(),
                 datetime.now(timezone.utc).isoformat(timespec="seconds"), r["tweet_id"]))
            done += 1
            runlog.add("graded")
            runlog.add("new reports", int(bool(g["is_behavior_report"] and models) and g["interestingness"] >= config.MIN_SCORE))
            if done % 50 == 0:
                con.commit()
                print(f"  classified {done}/{len(rows)}", flush=True)
    con.commit()
    print(f"classified {done}, failed {failed}")
