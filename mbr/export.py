"""Plain-text exports for LLMs: /llms.txt, /llms-full.txt and /data/<model>.md."""

import html
from pathlib import Path

from . import config

INTRO = """# Model Behavior Reports

> Crowdsourced LLM behaviors from X, built from the Community Archive (https://www.community-archive.org).
> Tweets mentioning a tracked model are graded by {classifier} for whether they report a *behavior* (a tendency, quirk or
> personality trait, not a capability) and how interesting it is (1-10). Reports scoring {min_score}+ are kept.
> Per-model summaries are written by {summarizer} from those reports. Updated hourly; this file was built {built_at}.

Ranking: rank = {rq} * quotes + {rr} * log2(1 + min(retweets, {cap})) + {rs} * interestingness,
where `quotes` counts distinct Community Archive accounts that quote-tweeted the report.
"""


def _text(t) -> str:
    text = html.unescape(t["full_text"] or "").strip()
    for u in t.get("urls_raw", []):
        text = text.replace(u["url"], u["expanded"])
    return text


def _quote_block(label: str, t) -> str:
    body = _text(t).replace("\n", "\n> ")
    return f"> [{label} @{t['username']}] {body}\n"


def _media_line(t) -> str:
    items = []
    for m in t.get("media") or []:
        items.append(f"video {m['video']}" if m.get("video") else f"{m['type']} {m['url']}")
    return f"Media: {'; '.join(items)}\n" if items else ""


def report_md(i: int, t) -> str:
    out = [f"### Report {i}: {t['behavior'] or '(no gist)'}\n",
           f"- URL: {t['url']}\n",
           f"- Author: @{t['username']} ({t['display_name']}), {t['created_at'][:10]}\n",
           f"- Models: {', '.join(m['name'] for m in t['models'])}\n",
           f"- Interestingness: {t['score']}/10 ({t['kind']}) · quotes by archive accounts: {t['quotes']} · "
           f"retweets: {t['retweets']} · likes: {t['likes']} · rank: {t['rank']}\n\n"]
    if t.get("parent"):
        out.append(_quote_block("in reply to", t["parent"]))
        out.append("\n")
    out.append(f"@{t['username']}: {_text(t)}\n")
    out.append(_media_line(t))
    if t.get("quoted"):
        out.append("\n" + _quote_block("quoting", t["quoted"]))
    if t.get("replies"):
        out.append("\nTop replies:\n")
        out += [f"- @{r['username']}: {_text(r).replace(chr(10), ' ')}\n" for r in t["replies"]]
    return "".join(out) + "\n"


def model_md(m, items) -> str:
    s = m.get("summary") or {}
    head = [f"# {m['name']}: behavior reports\n\n",
            f"{m['n']} reports. Source: {config.SITE_URL}/model/{m['slug']}/\n\n"]
    if s:
        head += ["## Summary\n\n", f"**{s['one_liner']}**\n\n", f"{s['aggregate']}\n\n", f"{s['portrait']}\n\n",
                 f"(Summary written {s['updated_at'][:10]}.)\n\n"]
    head.append("## Reports (highest rank first)\n\n")
    return "".join(head) + "".join(report_md(i, t) for i, t in enumerate(items, 1))


def write(out: Path, models, items_by_slug, built_at: str):
    intro = INTRO.format(classifier=config.CLASSIFY_MODEL, summarizer=config.SUMMARY_MODEL,
                         min_score=config.MIN_SCORE, built_at=built_at, rq=config.RANK_QUOTE,
                         rr=config.RANK_RT, cap=config.RANK_RT_CAP, rs=config.RANK_SCORE)
    index = [intro, "\n## Models\n\n"]
    for m in models:
        s = m.get("summary") or {}
        index.append(f"### [{m['name']}]({config.SITE_URL}/data/{m['slug']}.md), {m['n']} reports\n\n")
        if s:
            index.append(f"{s['one_liner']} {s['aggregate']}\n\n")
    index.append(f"\n## Everything in one file\n\n- [llms-full.txt]({config.SITE_URL}/llms-full.txt): "
                 "all summaries and all reports with their context.\n")
    (out / "llms.txt").write_text("".join(index))

    data = out / "data"
    data.mkdir()
    full = [intro, "\n"]
    for m in models:
        md = model_md(m, items_by_slug[m["slug"]])
        (data / f"{m['slug']}.md").write_text(md)
        full.append(md + "\n---\n\n")
    (out / "llms-full.txt").write_text("".join(full))
