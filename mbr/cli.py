"""mbr backfill | update | classify | summarize | build"""

import argparse
import fcntl
import sys

from . import build, classify, collect, config, db, newmodels, summarize, xmedia


def main():
    p = argparse.ArgumentParser(prog="mbr")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="scan the last N days of the archive for candidates")
    b.add_argument("--days", type=int, default=config.WINDOW_DAYS)
    sub.add_parser("update", help="hourly job: scan new tweets, hydrate, classify, summarize, build")
    c = sub.add_parser("classify", help="hydrate and grade pending candidates")
    c.add_argument("--limit", type=int)
    s = sub.add_parser("summarize")
    s.add_argument("--force", action="store_true")
    sub.add_parser("build")
    sub.add_parser("quotes", help="refresh quote counts")
    sub.add_parser("videos", help="look up playable video URLs")
    sub.add_parser("stats")
    sub.add_parser("check-models", help="look for new models on OpenRouter now")
    args = p.parse_args()

    # One run at a time: the hourly timer must not overlap a long backfill.
    con = db.connect()
    lock = open(config.DB_PATH.parent / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if args.cmd not in ("build", "stats"):  # read-only commands may run alongside
            sys.exit("another mbr run is in progress")

    if args.cmd == "backfill":
        collect.backfill(con, args.days)
    elif args.cmd == "update":
        try:
            if newmodels.check(con):
                collect.rescan(con, 14)
        except Exception as e:  # noqa: BLE001 — never let the model check block the hourly update
            print(f"models: check failed: {e!r}")
        collect.incremental(con)
        collect.hydrate(con)
        xmedia.attach_videos(con)
        xmedia.fill_authors(con)
        xmedia.fill_avatars(con)
        classify.classify(con, config.CLASSIFY_PER_RUN)
        collect.refresh_quotes(con)
        summarize.summarize(con)
        build.build(con)
    elif args.cmd == "classify":
        collect.hydrate(con)
        classify.classify(con, args.limit)
    elif args.cmd == "summarize":
        summarize.summarize(con, args.force)
    elif args.cmd == "videos":
        xmedia.attach_videos(con)
        xmedia.fill_authors(con)
        xmedia.fill_avatars(con, limit=5000)
    elif args.cmd == "quotes":
        collect.refresh_quotes(con)
    elif args.cmd == "build":
        build.build(con)
    elif args.cmd == "check-models":
        if newmodels.check(con, force=True):
            collect.rescan(con, 14)
    elif args.cmd == "stats":
        for row in con.execute(
                "SELECT COUNT(*) candidates, SUM(hydrated) hydrated, SUM(is_report IS NOT NULL) classified, "
                "SUM(is_report) reports, SUM(is_report=1 AND score>=?) shown FROM reports", (config.MIN_SCORE,)):
            print(dict(row))
        for row in con.execute(
                "SELECT j.value model, COUNT(*) candidates, SUM(r.is_report=1 AND r.score>=?) shown "
                "FROM reports r, json_each(COALESCE(r.models, r.candidates)) j GROUP BY 1 ORDER BY 2 DESC",
                (config.MIN_SCORE,)):
            print(f"  {row['model']:<20} {row['candidates']:>6} {row['shown'] or 0:>6}")


if __name__ == "__main__":
    main()
