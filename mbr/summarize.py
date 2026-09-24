"""Per-model personality summaries, written from the collected reports."""

import json
import re
from datetime import datetime, timedelta, timezone

from . import config, llm, runlog
from .classify import context_for

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["themes", "one_liner", "aggregate", "portrait"],
    "properties": {
        "themes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["theme", "reports"],
            "properties": {"theme": {"type": "string"},
                           "reports": {"type": "array", "items": {"type": "integer"}}}}},
        "one_liner": {"type": "string"},
        "aggregate": {"type": "string"},
        "portrait": {"type": "string"},
    },
}

PROMPT = """Below are {n} reports posted on X by people describing how {name} behaves: its tendencies, quirks and personality. Each report is one tweet, marked [THE TWEET]. The tweet it replies to, the tweet it quotes and its top replies are shown as context: they help you understand the report, but they are NOT separate reports. A reply agreeing with a report does not make a second report.

Write, in plain, concrete English, without hype:

1. `themes`: first, the recurring behaviours, each with the numbers of the reports that show it (e.g. {{"theme": "pushes back when the user is wrong", "reports": [2, 7, 11]}}). Only cite a report for a theme if its tweet itself describes that behaviour. A theme may have a single report.
2. `one_liner`: the single most important personality trait, in this exact format: a two-to-four word noun phrase, then one sentence explaining it. Example: "A paranoid. It tends to walk on tiptoes and suspects every question is an evaluation."
3. `aggregate`: two short sentences, 45 words at most in total, shown on a small card, about the 2-3 biggest themes. Numbers may only count reports: every count you write must equal the number of reports you listed for that theme. Replies, quoted tweets and other context never contribute to a count. You may mention them in words when they matter (e.g. "one report, with many replies agreeing"), but never as a number and never added to a report count. With {n} reports in total, no count can exceed {n}. When a theme rests on one report, say "one report" rather than implying a trend. Example: "4 reports of pushing back when the user is wrong, 3 of taking actions it wasn't allowed to. One report also finds it unusually funny." Do not list examples in parentheses.
4. `portrait`: five or six free-form sentences giving a fuller portrait of the model's character as reported: its recurring tendencies, what surprises people, where reports disagree. Be honest about thin evidence: if most themes rest on one or two reports, say so. The same counting rule applies here: any number refers to reports only; describe reply reactions in words.

Weigh a report more when it is vivid, specific, or widely discussed (quotes by community accounts are the best signal; likes and retweets are also given). Ignore reports that are really about capabilities or are unclear.

## Reports
{reports}"""

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _problems(out: dict, n: int) -> list[str]:
    """Counts that can't be right: more reports than exist, or cited reports that don't exist."""
    issues = []
    cited = {i for t in out["themes"] for i in t["reports"]}
    if any(i < 1 or i > n for i in cited):
        issues.append(f"themes cite report numbers outside 1..{n}")
    sizes = {len(set(t["reports"])) for t in out["themes"]}
    for num, _ in re.findall(r"\b(\d+|" + "|".join(WORDS) + r")\s+(?:\w+\s+){0,2}(reports?|posts?|users|people)\b",
                             out["aggregate"], re.I):
        k = int(num) if num.isdigit() else WORDS[num.lower()]
        if k > n:
            issues.append(f"aggregate says {k} reports but there are only {n}")
        elif k not in sizes and k > 1:
            issues.append(f"aggregate says {k} reports, but no theme lists {k} reports")
    return issues


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
            # Reports removed (e.g. keyword changes): the summary may cite them, rewrite now.
            shrunk = n < old["n_reports"]
            if not shrunk and (fresh or n - old["n_reports"] < config.SUMMARY_MIN_NEW):
                continue
        rows = _reports_for(con, m["slug"])
        blocks = []
        for i, r in enumerate(rows, 1):
            text, _ = context_for(con, r)
            blocks.append(f"### Report {i} ({r['created_at'][:10]}, {r['quotes']} quotes by community accounts, {r['likes']} likes, {r['retweets']} retweets)"
                          f"\nGist: {r['behavior']}\n{text}")
        print(f"summarizing {m['name']} from {len(rows)} reports with {config.SUMMARY_MODEL}", flush=True)
        prompt = PROMPT.format(n=len(rows), name=m["name"], reports="\n".join(blocks))
        out = llm.chat_json(config.SUMMARY_MODEL, prompt, SCHEMA, "model_summary", max_tokens=6000, purpose="summaries")
        issues = _problems(out, len(rows))
        if issues:
            print(f"  counts look wrong ({'; '.join(issues)}), rewriting once", flush=True)
            out = llm.chat_json(config.SUMMARY_MODEL, prompt + "\n\n## Correction\nA previous draft had these errors: "
                                + "; ".join(issues) + ". Recount from the report numbers.",
                                SCHEMA, "model_summary", max_tokens=6000, purpose="summaries")
        con.execute("INSERT OR REPLACE INTO model_summaries VALUES (?,?,?,?,?,?)",
                    (m["slug"], out["one_liner"].strip(), out["aggregate"].strip(), out["portrait"].strip(),
                     n, now.isoformat(timespec="seconds")))
        con.commit()
        runlog.add("summaries")
