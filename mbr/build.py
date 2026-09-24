"""Render the static site from the database."""

import html
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape

from . import config, export

TEMPLATES = Path(__file__).parent / "templates"
NOTABLE_N = 50
LATEST_N = 40
MODEL_PAGE_MAX = 600

TOKEN_RE = re.compile(r"(https?://\S+|@\w{1,15}|#\w+)")
LEADING_MENTIONS_RE = re.compile(r"^(?:@\w{1,15}\s+)+")


def compact(n) -> str:
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n // 1000}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def render_text(text: str, urls: list, drop_trailing: bool) -> Markup:
    """Tweet text to safe HTML: expand t.co links, linkify mentions/hashtags,
    drop the trailing t.co link that points at attached media or a quote."""
    text = html.unescape(text or "").strip()
    expand = {u["url"]: u for u in urls}
    if drop_trailing:
        text = re.sub(r"\s*https://t\.co/\w+$", "", text)
    out = []
    for part in TOKEN_RE.split(text):
        if not part:
            continue
        if part.startswith("http"):
            u = expand.get(part)
            href, label = (u["expanded"], u["display"]) if u else (part, part.split("//", 1)[1])
            out.append(Markup('<a href="{}" rel="nofollow noopener" target="_blank">{}</a>').format(href, label))
        elif part.startswith("@"):
            out.append(Markup('<a href="https://x.com/{}" target="_blank" rel="noopener">{}</a>').format(part[1:], part))
        elif part.startswith("#"):
            out.append(Markup('<a href="https://x.com/hashtag/{}" target="_blank" rel="noopener">{}</a>').format(part[1:], part))
        else:
            out.append(escape(part))
    return Markup("").join(out)


def avatar(url: str | None, size="bigger") -> str | None:
    return url.replace("_normal.", f"_{size}.") if url and url.startswith("http") else None


class Store:
    def __init__(self, con):
        self.con = con
        self._cache = {}

    def tweet(self, tid, depth=0):
        """A tweet ready for the template, with parent/quote/replies attached."""
        if not tid:
            return None
        key = (tid, depth)
        if key in self._cache:
            return self._cache[key]
        row = self.con.execute("SELECT * FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
        if not row:
            return None
        t = dict(row)
        t["media"] = json.loads(t["media"] or "[]")
        urls = t["urls_raw"] = json.loads(t["urls"] or "[]")
        text = t["full_text"] or ""
        if t["reply_to_tweet_id"] or t["reply_to_username"]:
            text = LEADING_MENTIONS_RE.sub("", html.unescape(text))
        t["html"] = render_text(text, urls, bool(t["media"] or t["quoted_tweet_id"]))
        t["avatar"] = avatar(t["avatar"])
        t["url"] = f"https://x.com/{t['username'] or 'i'}/status/{tid}"
        t["ts"] = t["created_at"]
        t["likes_c"], t["retweets_c"] = compact(t["likes"]), compact(t["retweets"])
        t["display_name"] = t["display_name"] or t["username"]
        if depth == 0:
            t["quoted"] = self.tweet(t["quoted_tweet_id"], depth + 1)
            t["parent"] = self.tweet(t["reply_to_tweet_id"], depth + 1)
        self._cache[key] = t
        return t

    def report(self, r):
        t = self.tweet(r["tweet_id"])
        if not t:
            return None
        t = dict(t)
        t["models"] = [config.MODEL_BY_SLUG[s] for s in json.loads(r["models"] or "[]")
                       if s in config.MODEL_BY_SLUG]
        t["behavior"] = r["behavior"]
        t["score"] = r["score"]
        t["kind"] = r["kind"]
        t["quotes"] = r["quotes"] or 0
        t["rank"] = round(config.rank(t["quotes"], t["retweets"], t["score"]))
        t["replies"] = [x for x in (self.tweet(rid, 1) for rid in json.loads(r["reply_ids"] or "[]")) if x]
        return t


SHOWN = "r.is_report=1 AND r.score>=?"


def build(con, out_dir: Path = config.OUT_DIR):
    con.create_function("mbr_rank", 3, config.rank, deterministic=True)
    store = Store(con)
    now = datetime.now(timezone.utc)
    q = lambda sql, *a: con.execute(sql, a).fetchall()

    # Models with enough reports to show.
    stats = {row["slug"]: row for row in q(
        f"SELECT j.value slug, COUNT(*) n, AVG(julianday(r.created_at)) avg_day "
        f"FROM reports r, json_each(r.models) j WHERE {SHOWN} GROUP BY j.value", config.MIN_SCORE)}
    counts = {slug: row["n"] for slug, row in stats.items()}
    summaries = {row["slug"]: dict(row) for row in q("SELECT * FROM model_summaries")}
    models = []
    for m in config.MODELS:
        if counts.get(m["slug"], 0) < config.MIN_MODEL_REPORTS:
            continue
        top = q(f"SELECT r.* FROM reports r JOIN tweets t USING(tweet_id), json_each(r.models) j "
                f"WHERE j.value=? AND {SHOWN} ORDER BY mbr_rank(r.quotes, t.retweets, r.score) DESC, t.likes DESC LIMIT 1",
                m["slug"], config.MIN_SCORE)
        s = summaries.get(m["slug"])
        one = (s or {}).get("one_liner") or ""
        head, _, rest = one.partition(". ")
        models.append(dict(m, n=counts[m["slug"]], summary=s,
                           one_head=head + "." if rest else one, one_rest=rest,
                           top=store.report(top[0]) if top else None))
    # Newest conversations first: order by the mean date of each model's reports.
    models.sort(key=lambda m: -stats[m["slug"]]["avg_day"])
    visible = {m["slug"] for m in models}

    def reports(sql, *args):
        out = []
        for r in q(sql, *args):
            t = store.report(r)
            if t:
                t["models"] = [m for m in t["models"] if m["slug"] in visible] or t["models"]
                out.append(t)
        return out

    # Most notable, diversified: each round takes every model's next top-ranked
    # report, so the first screen shows one report per model.
    by_rank = lambda t: (-t["rank"], -(t["likes"] or 0))
    queues = [reports(f"SELECT r.* FROM reports r JOIN tweets t USING(tweet_id), json_each(r.models) j "
                      f"WHERE j.value=? AND {SHOWN} ORDER BY mbr_rank(r.quotes, t.retweets, r.score) DESC, t.likes DESC LIMIT {NOTABLE_N}",
                      m["slug"], config.MIN_SCORE) for m in models]
    notable, seen = [], set()
    while len(notable) < NOTABLE_N:
        batch = []
        for queue in queues:
            while queue and queue[0]["tweet_id"] in seen:
                queue.pop(0)
            if queue:
                t = queue.pop(0)
                seen.add(t["tweet_id"])
                batch.append(t)
        if not batch:
            break
        notable += sorted(batch, key=by_rank)
    notable = notable[:NOTABLE_N]
    latest = reports(f"SELECT r.* FROM reports r WHERE {SHOWN} ORDER BY r.created_at DESC LIMIT {LATEST_N}",
                     config.MIN_SCORE)

    reporters = []
    for row in q(f"SELECT t.username, MAX(t.display_name) name, MAX(CASE WHEN t.avatar LIKE 'http%' THEN t.avatar END) avatar, COUNT(DISTINCT r.tweet_id) n, "
                 f"COUNT(DISTINCT j.value) nm FROM reports r JOIN tweets t USING(tweet_id), json_each(r.models) j "
                 f"WHERE {SHOWN} AND t.username IS NOT NULL GROUP BY lower(t.username) "
                 f"ORDER BY nm DESC, n DESC LIMIT 5", config.MIN_SCORE):
        reporters.append(dict(row, avatar=avatar(row["avatar"], "200x200")))

    total = q(f"SELECT COUNT(*) n, COUNT(DISTINCT lower(t.username)) people FROM reports r "
              f"JOIN tweets t USING(tweet_id) WHERE {SHOWN}", config.MIN_SCORE)[0]

    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    env.filters["compact"] = compact
    common = dict(site=config.SITE_URL, umami_script=config.UMAMI_SCRIPT, umami_id=config.UMAMI_WEBSITE_ID,
                  built_at=now.isoformat(timespec="seconds"), n_reports=total["n"],
                  n_people=total["people"], n_models=len(models), github=config.GITHUB_URL,
                  classifier=config.CLASSIFY_MODEL_NAME, summarizer=config.SUMMARY_MODEL_NAME,
                  min_score=config.MIN_SCORE, rank_quote=config.RANK_QUOTE, rank_rt=config.RANK_RT,
                  rank_cap=config.RANK_RT_CAP, rank_score=config.RANK_SCORE)

    tmp = out_dir.with_name(out_dir.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    (tmp / "index.html").write_text(env.get_template("index.html").render(
        **common, notable=notable, latest=latest, models=models, reporters=reporters))
    items_by_slug = {}
    for m in models:
        items = items_by_slug[m["slug"]] = reports(f"SELECT r.* FROM reports r JOIN tweets t USING(tweet_id), json_each(r.models) j "
                        f"WHERE j.value=? AND {SHOWN} ORDER BY mbr_rank(r.quotes, t.retweets, r.score) DESC, t.likes DESC LIMIT {MODEL_PAGE_MAX}",
                        m["slug"], config.MIN_SCORE)
        page = tmp / "model" / m["slug"]
        page.mkdir(parents=True)
        (page / "index.html").write_text(env.get_template("model.html").render(
            **common, model=m, items=items, models=models))
    (tmp / "about").mkdir()
    (tmp / "about" / "index.html").write_text(env.get_template("about.html").render(**common))
    shutil.copy(TEMPLATES / "favicon.svg", tmp / "favicon.svg")
    export.write(tmp, models, items_by_slug, common["built_at"])

    # Swap the new build in place.
    old = out_dir.with_name(out_dir.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out_dir.exists():
        out_dir.rename(old)
    tmp.rename(out_dir)
    shutil.rmtree(old, ignore_errors=True)
    print(f"built {out_dir}: {len(models)} models, {total['n']} reports shown")
