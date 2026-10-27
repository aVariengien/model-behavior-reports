"""Find model-mentioning tweets and hydrate them with parent, quote and replies."""

import json
from datetime import datetime, timedelta, timezone

from . import archive, config, db, runlog


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _known(con, ids) -> set[str]:
    ids = list(set(ids))
    return {r[0] for r in con.execute(
        f"SELECT tweet_id FROM reports WHERE tweet_id IN ({','.join('?' * len(ids))})", ids)} if ids else set()


def _set_replies(con, tids):
    """Keep each report's most-liked replies, from the replies we have seen in scans."""
    for tid in tids:
        ids = [r[0] for r in con.execute("SELECT tweet_id FROM tweets WHERE reply_to_tweet_id=? "
                                         "ORDER BY likes DESC LIMIT ?", (tid, config.MAX_REPLIES))]
        con.execute("UPDATE reports SET reply_ids=? WHERE tweet_id=?", (json.dumps(ids), tid))


def ingest(con, rows: list[dict]) -> int:
    """Register new report candidates from a page of scanned tweets, refresh counts of
    tweets we already hold, and record replies to and quotes of known candidates.
    Returns the number of new candidates."""
    cutoff = _iso(_now() - timedelta(days=config.WINDOW_DAYS))
    is_rt = lambda r: r["retweeted_tweet_id"] or r["full_text"].startswith("RT @")  # noqa: E731
    retweets = [r for r in rows if is_rt(r)]
    rows = [r for r in rows if not is_rt(r)]

    # Engagement counts move after capture; keep ours current. Retweets carry the
    # original's current retweet count.
    con.executemany("UPDATE tweets SET likes=MAX(likes,?), retweets=MAX(retweets,?) WHERE tweet_id=?",
                    [(r["likes"], r["retweets"], r["tweet_id"]) for r in rows])
    con.executemany("UPDATE tweets SET retweets=MAX(retweets,?) WHERE tweet_id=?",
                    [(r["retweets"], r["retweeted_tweet_id"]) for r in retweets if r["retweeted_tweet_id"]])

    found: dict[str, set[str]] = {}
    for r in rows:
        if r["created_at"] < cutoff:
            continue
        models = config.match_models(r["full_text"], r["created_at"])
        if models:
            found.setdefault(r["tweet_id"], set()).update(models)

    runlog.add("scanned", len(rows) + len(retweets))
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

    # The API can't list a tweet's replies or quotes, so we collect them as they pass by.
    known = _known(con, [r["reply_to_tweet_id"] for r in rows if r["reply_to_tweet_id"]]
                   + [r["quoted_tweet_id"] for r in rows if r["quoted_tweet_id"]])
    replies = [r for r in rows if r["reply_to_tweet_id"] in known]
    for r in replies:
        db.upsert_tweet(con, r)
    _set_replies(con, {r["reply_to_tweet_id"] for r in replies})
    con.executemany("INSERT OR IGNORE INTO quotes(tweet_id, quoted_tweet_id, account_id) VALUES (?,?,?)",
                    [(r["tweet_id"], r["quoted_tweet_id"], r["account_id"]) for r in rows
                     if r["quoted_tweet_id"] in known and r["account_id"]])
    con.commit()
    runlog.add("candidates", new)
    return new


def backfill(con, days: int):
    """Scan every tweet created in the last `days` days, a day at a time (resumable)."""
    day = datetime.fromisoformat(db.get_state(con, "backfill_day") or _iso(_now() - timedelta(days=days)))
    total = new = 0
    while day < _now():
        for page in archive.scan(day, min(day + timedelta(days=1), _now())):
            total += len(page)
            new += ingest(con, page)
        day += timedelta(days=1)
        db.set_state(con, "backfill_day", _iso(day))
        print(f"  backfill: {total} scanned, {new} candidates, at {day.date()}", flush=True)
    db.set_state(con, "backfill_day", "")
    if not db.get_state(con, "created_cursor"):
        db.set_state(con, "created_cursor", _iso(_now()))
    print(f"backfill done: {total} tweets scanned, {new} new candidates")


def rescan(con, days: int):
    """Re-scan recent tweets, e.g. after new models or keywords were added."""
    total = new = 0
    for page in archive.scan(_now() - timedelta(days=days), _now()):
        total += len(page)
        new += ingest(con, page)
    print(f"rescan: {total} tweets from the last {days} days, {new} new candidates")


def incremental(con):
    """Scan tweets created since the last run, re-reading OVERLAP_HOURS for late arrivals."""
    started = _now()
    cursor = datetime.fromisoformat(db.get_state(con, "created_cursor") or _iso(started - timedelta(hours=24)))
    since = cursor - timedelta(hours=config.OVERLAP_HOURS)
    total = new = 0
    for page in archive.scan(since, started):
        total += len(page)
        new += ingest(con, page)
    db.set_state(con, "created_cursor", _iso(started))
    print(f"incremental: {total} tweets created since {_iso(since)}, {new} new candidates")


def new_members(con):
    """Archives uploaded late: scan the last WINDOW_DAYS of each new member, and of
    members who now have an uploaded archive (opted-in accounts may upload later)."""
    seen = db.get_state(con, "members")
    current = {f"{m['account_id']}:{int(m['has_archive'])}" for m in archive.members()}
    if seen is None:  # first run: nothing to catch up on
        db.set_state(con, "members", json.dumps(sorted(current)))
        return
    seen = set(json.loads(seen))
    since = _now() - timedelta(days=config.WINDOW_DAYS)
    for key in sorted(current - seen):
        account, has_archive = key.split(":")
        if has_archive == "1":
            total = new = 0
            for page in archive.account_tweets(account, since):
                total += len(page)
                new += ingest(con, page)
            print(f"members: scanned {total} tweets of new archive {account}, {new} new candidates", flush=True)
        seen.add(key)
        db.set_state(con, "members", json.dumps(sorted(seen)))


def hydrate(con, batch: int = 200):
    """Fetch full tweet, parent and quoted tweet for pending reports."""
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
        for t in [*main.values(), *extra.values()]:
            db.upsert_tweet(con, t)
        _set_replies(con, ids)
        for tid in ids:
            t = con.execute("SELECT created_at FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
            con.execute("UPDATE reports SET hydrated=1, created_at=? WHERE tweet_id=?",
                        (t["created_at"] if t else None, tid))
        con.commit()
        runlog.add("hydrated", len(ids))
        print(f"  hydrated {len(ids)} reports", flush=True)


def refresh_quotes(con):
    """Count distinct archive accounts quoting each report (author excluded), from the
    quotes collected by scans."""
    con.execute("UPDATE reports SET quotes=(SELECT COUNT(DISTINCT q.account_id) FROM quotes q, tweets t "
                "WHERE q.quoted_tweet_id=reports.tweet_id AND t.tweet_id=reports.tweet_id "
                "AND q.account_id IS NOT t.account_id), quotes_at=? WHERE is_report=1", (_iso(_now()),))
    con.commit()


def seed_quotes(con):
    """One-off, before the old API shuts down: copy existing quotes of reports into the quotes table."""
    ids = [r[0] for r in con.execute("SELECT tweet_id FROM reports WHERE is_report=1")]
    rows = archive.legacy_quoting_tweets(ids)
    con.executemany("INSERT OR IGNORE INTO quotes(tweet_id, quoted_tweet_id, account_id) VALUES (?,?,?)", rows)
    con.commit()
    print(f"quotes: seeded {len(rows)} quotes of {len(ids)} reports")


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
