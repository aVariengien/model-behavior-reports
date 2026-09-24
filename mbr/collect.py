"""Find model-mentioning tweets and hydrate them with parent, quote and replies."""

import json
from datetime import datetime, timedelta, timezone

from . import archive, config, db, runlog

SCAN_SELECT = ("tweet_id,created_at,updated_at,full_text,favorite_count,retweet_count,"
               "reply_to_tweet_id")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ingest(con, rows: list[dict]) -> int:
    """Register new report candidates from a page of scanned tweets and refresh
    counts of tweets we already hold. Returns the number of new candidates."""
    cutoff = _iso(_now() - timedelta(days=config.WINDOW_DAYS))

    # Engagement counts move after capture; keep ours current.
    con.executemany(
        "UPDATE tweets SET likes=MAX(likes,?), retweets=MAX(retweets,?) WHERE tweet_id=?",
        [(r["favorite_count"] or 0, r["retweet_count"] or 0, r["tweet_id"]) for r in rows
         if not r["full_text"].startswith("RT @")])
    # A new reply to a known report: re-hydrate it to pick up the reply.
    reply_parents = {r["reply_to_tweet_id"] for r in rows if r.get("reply_to_tweet_id")}
    if reply_parents:
        con.executemany("UPDATE reports SET hydrated=0 WHERE tweet_id=? AND hydrated=1",
                        [(p,) for p in reply_parents])

    found: dict[str, set[str]] = {}
    retweets: dict[str, tuple[set[str], dict]] = {}
    for r in rows:
        if r["created_at"] < cutoff:
            continue
        models = config.match_models(r["full_text"], r["created_at"])
        if not models:
            continue
        if r["full_text"].startswith("RT @"):
            retweets[r["tweet_id"]] = (set(models), r)
        else:
            found.setdefault(r["tweet_id"], set()).update(models)

    # Retweets are not reports, but they carry the original's current retweet
    # count; use it to refresh originals we already hold.
    if retweets:
        for rt_id, orig_id in archive.retweet_targets(retweets).items():
            if orig_id:
                con.execute("UPDATE tweets SET retweets=MAX(retweets,?) WHERE tweet_id=?",
                            (retweets[rt_id][1]["retweet_count"] or 0, orig_id))

    runlog.add("scanned", len(rows))
    new = 0
    for tid, models in found.items():
        old = con.execute("SELECT candidates FROM reports WHERE tweet_id=?", (tid,)).fetchone()
        if old:
            merged = sorted(set(json.loads(old["candidates"])) | models)
            if merged != sorted(json.loads(old["candidates"])):
                # Mentions a model it wasn't graded against (e.g. one just added): grade again.
                con.execute("UPDATE reports SET candidates=?, is_report=NULL WHERE tweet_id=?",
                            (json.dumps(merged), tid))
        else:
            con.execute("INSERT INTO reports(tweet_id, candidates) VALUES (?,?)",
                        (tid, json.dumps(sorted(models))))
            new += 1
    con.commit()
    runlog.add("candidates", new)
    return new


def backfill(con, days: int):
    """Scan every tweet created in the last `days` days (resumable)."""
    started = _iso(_now())
    since = _iso(_now() - timedelta(days=days))
    until = db.get_state(con, "backfill_cursor")
    if not db.get_state(con, "updated_cursor"):
        db.set_state(con, "updated_cursor", started)
    total = new = 0
    for page in archive.scan("created_at", since, until, select=SCAN_SELECT):
        total += len(page)
        new += ingest(con, page)
        db.set_state(con, "backfill_cursor", page[-1]["created_at"])
        if total % 20000 < len(page):
            print(f"  backfill: {total} scanned, {new} candidates, at {page[-1]['created_at']}", flush=True)
    db.set_state(con, "backfill_cursor", "")
    print(f"backfill done: {total} tweets scanned, {new} new candidates")


def rescan(con, days: int):
    """Re-scan recent tweets, e.g. after new models or keywords were added."""
    total = new = 0
    for page in archive.scan("created_at", _iso(_now() - timedelta(days=days)), select=SCAN_SELECT):
        total += len(page)
        new += ingest(con, page)
    print(f"rescan: {total} tweets from the last {days} days, {new} new candidates")


def incremental(con):
    """Scan tweets inserted or updated since the last run."""
    cursor = db.get_state(con, "updated_cursor") or _iso(_now() - timedelta(hours=2))
    since = _iso(datetime.fromisoformat(cursor) - timedelta(minutes=10))
    total = new = 0
    newest = cursor
    for page in archive.scan("updated_at", since, select=SCAN_SELECT):
        total += len(page)
        new += ingest(con, page)
        newest = max(newest, page[0]["updated_at"])
    db.set_state(con, "updated_cursor", newest)
    print(f"incremental: {total} tweets scanned since {since}, {new} new candidates")


def hydrate(con, batch: int = 200):
    """Fetch full tweet, parent, quoted tweet and top replies for pending reports."""
    while True:
        ids = [r["tweet_id"] for r in con.execute(
            "SELECT tweet_id FROM reports WHERE hydrated=0 LIMIT ?", (batch,))]
        if not ids:
            return
        main = archive.fetch_tweets(ids)
        related = {t["reply_to_tweet_id"] for t in main.values() if t["reply_to_tweet_id"]}
        related |= {t["quoted_tweet_id"] for t in main.values() if t["quoted_tweet_id"]}
        # Stand-ins for retweeted originals may already sit in our table.
        related |= {r["quoted_tweet_id"] for r in con.execute(
            f"SELECT quoted_tweet_id FROM tweets WHERE tweet_id IN ({','.join('?' * len(ids))}) "
            "AND quoted_tweet_id IS NOT NULL", ids)}
        extra = archive.fetch_tweets(related - set(main)) if related else {}
        replies = archive.fetch_top_replies(ids, config.MAX_REPLIES)
        for t in [*main.values(), *extra.values(), *(r for rs in replies.values() for r in rs)]:
            db.upsert_tweet(con, t)
        for tid in ids:
            t = con.execute("SELECT created_at FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
            reply_ids = [r["tweet_id"] for r in replies.get(tid, [])]
            con.execute("UPDATE reports SET hydrated=1, reply_ids=?, created_at=? WHERE tweet_id=?",
                        (json.dumps(reply_ids), t["created_at"] if t else None, tid))
        con.commit()
        runlog.add("hydrated", len(ids))
        print(f"  hydrated {len(ids)} reports", flush=True)


def refresh_quotes(con, days: int = 30):
    """Count distinct archive accounts quoting each shown report (author excluded).
    Recent reports are refreshed every run; older ones once."""
    since = _iso(_now() - timedelta(days=days))
    rows = con.execute(
        "SELECT r.tweet_id, t.account_id FROM reports r JOIN tweets t USING(tweet_id) "
        "WHERE r.is_report=1 AND (r.created_at >= ? OR r.quotes_at IS NULL)", (since,)).fetchall()
    if not rows:
        return
    quoters = archive.quoting_accounts([r["tweet_id"] for r in rows])
    now = _iso(_now())
    con.executemany("UPDATE reports SET quotes=?, quotes_at=? WHERE tweet_id=?",
                    [(len(quoters.get(r["tweet_id"], set()) - {r["account_id"]}), now, r["tweet_id"]) for r in rows])
    con.commit()
    print(f"quotes: refreshed {len(rows)} reports, {sum(1 for r in rows if quoters.get(r['tweet_id']))} quoted")



def rematch(con):
    """Re-run keyword matching on every stored report (after keyword or version rules change).
    Reports whose models changed are graded again; ones matching nothing are set aside
    (hidden, untagged) so a later rescan can revive them if keywords are widened."""
    # Candidates never fetched or graded can't be re-checked (no text yet): drop them, the
    # rescan that follows a keyword change re-adds the ones that still match.
    stale = con.execute("DELETE FROM reports WHERE hydrated=0 AND is_report IS NULL").rowcount
    if stale:
        print(f"rematch: dropped {stale} unfetched candidates (the rescan re-adds real matches)")
    changed = dropped = trimmed = 0
    for r in con.execute("SELECT r.tweet_id, r.candidates, r.models, t.full_text, t.created_at "
                         "FROM reports r JOIN tweets t USING(tweet_id)").fetchall():
        models = config.match_models(r["full_text"], r["created_at"])
        if models != sorted(json.loads(r["candidates"] or "[]")):
            if not models:
                con.execute("UPDATE reports SET candidates='[]', models='[]', is_report=0 WHERE tweet_id=?",
                            (r["tweet_id"],))
                dropped += 1
            else:
                con.execute("UPDATE reports SET candidates=?, is_report=NULL WHERE tweet_id=?",
                            (json.dumps(models), r["tweet_id"]))
                changed += 1
        elif r["models"]:
            # The grader may only confirm models the tweet itself mentions.
            kept = [m for m in json.loads(r["models"]) if m in models]
            if kept != json.loads(r["models"]):
                con.execute("UPDATE reports SET models=?, is_report=CASE WHEN ?='[]' THEN 0 ELSE is_report END "
                            "WHERE tweet_id=?", (json.dumps(kept), json.dumps(kept), r["tweet_id"]))
                trimmed += 1
    con.commit()
    print(f"rematch: {changed} to regrade, {dropped} set aside, {trimmed} had models outside their keywords")
