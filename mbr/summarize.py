"""Per-model personality summaries, written from the collected reports."""

import json
from datetime import datetime, timedelta, timezone

from . import config, llm
from .classify import context_for

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["one_liner", "aggregate", "portrait"],
    "properties": {
        "one_liner": {"type": "string"},
        "aggregate": {"type": "string"},
        "portrait": {"type": "string"},
    },
}

PROMPT = """Below are {n} reports posted on X by people describing how {name} behaves: its tendencies, quirks and personality. Each report comes with the tweet it replies to, the tweet it quotes, and its top replies, when available.

Write three things, in plain, concrete English, without hype:

1. `one_liner`: the single most important personality trait, in this exact format: a two-to-four word noun phrase, then one sentence explaining it. Example: "A paranoid. It tends to walk on tiptoes and suspects every question is an evaluation."
2. `aggregate`: two short sentences, 45 words at most in total, shown on a small card. Name only the 2-3 biggest themes, with counts. Example: "4 reports of pushing back when the user is wrong, 3 of taking actions it wasn't allowed to. Several people also find it unusually funny." Count only reports you can see below; never invent numbers. Do not list examples in parentheses.
3. `portrait`: five or six free-form sentences giving a fuller portrait of the model's character as reported: its recurring tendencies, what surprises people, where reports disagree.

Weigh a report more when it is vivid, specific, or widely discussed (quotes by community accounts are the best signal; likes and retweets are also given). Ignore reports that are really about capabilities or are unclear.

## Reports
{reports}"""


def _reports_for(con, slug):
    return con.execute(
        "SELECT r.*, t.likes, t.retweets FROM reports r JOIN tweets t USING(tweet_id), json_each(r.models) j "
        "WHERE j.value=? AND r.is_report=1 AND r.score>=? "
        "ORDER BY r.score DESC, r.quotes DESC, t.retweets DESC LIMIT ?",
        (slug, config.MIN_SCORE, config.SUMMARY_MAX_REPORTS)).fetchall()


def count_shown(con, slug) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM reports r, json_each(r.models) j "
        "WHERE j.value=? AND r.is_report=1 AND r.score>=?", (slug, config.MIN_SCORE)).fetchone()[0]


def summarize(con, force=False):
    now = datetime.now(timezone.utc)
    for m in config.MODELS:
        n = count_shown(con, m["slug"])
        if n < config.MIN_MODEL_REPORTS:
            continue
        old = con.execute("SELECT * FROM model_summaries WHERE slug=?", (m["slug"],)).fetchone()
        if old and not force:
            fresh = now - datetime.fromisoformat(old["updated_at"]) < timedelta(hours=config.SUMMARY_EVERY_HOURS)
            if fresh or n - old["n_reports"] < config.SUMMARY_MIN_NEW:
                continue
        rows = _reports_for(con, m["slug"])
        blocks = []
        for i, r in enumerate(rows, 1):
            text, _ = context_for(con, r)
            blocks.append(f"### Report {i} ({r['created_at'][:10]}, {r['quotes']} quotes by community accounts, {r['likes']} likes, {r['retweets']} retweets)"
                          f"\nGist: {r['behavior']}\n{text}")
        print(f"summarizing {m['name']} from {len(rows)} reports with {config.SUMMARY_MODEL}", flush=True)
        out = llm.chat_json(config.SUMMARY_MODEL, PROMPT.format(
            n=len(rows), name=m["name"], reports="\n".join(blocks)), SCHEMA, "model_summary", max_tokens=4000)
        con.execute("INSERT OR REPLACE INTO model_summaries VALUES (?,?,?,?,?,?)",
                    (m["slug"], out["one_liner"].strip(), out["aggregate"].strip(), out["portrait"].strip(),
                     n, now.isoformat(timespec="seconds")))
        con.commit()
