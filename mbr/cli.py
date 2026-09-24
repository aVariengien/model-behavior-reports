"""mbr backfill | update | classify | summarize | build"""

import argparse
import fcntl
import sys
from datetime import datetime, timezone

from . import build, classify, collect, config, db, newmodels, runlog, summarize, xmedia


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
    sub.add_parser("serve-editor", help="keyword-editing API for /status/ (127.0.0.1:3004)")
    sub.add_parser("check-models", help="look for new models on OpenRouter now")
    sub.add_parser("rematch", help="re-run keyword matching on stored reports")
    r = sub.add_parser("rekey", help="after keyword edits: rematch, rescan, grade, rebuild")
    r.add_argument("--days", type=int, default=config.WINDOW_DAYS)
    args = p.parse_args()
    if args.cmd == "serve-editor":
        from . import editor
        return editor.serve()

    # One run at a time: the hourly timer must not overlap a long backfill.
    con = db.connect()
    lock = open(config.DB_PATH.parent / ".lock", "w")
    try:
        # A re-key after a keyword edit waits for the hourly run instead of giving up.
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if args.cmd == "rekey" else fcntl.LOCK_NB))
    except BlockingIOError:
        if args.cmd not in ("build", "stats"):  # read-only commands may run alongside
            sys.exit("another mbr run is in progress")

    started = datetime.now(timezone.utc)
    logged = args.cmd not in ("build", "stats")
    try:
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
        elif args.cmd == "rematch":
            collect.rematch(con)
        elif args.cmd == "rekey":
            # Edits saved while a re-key runs leave a flag; loop until none is pending.
            pending = config.DB_PATH.parent / ".rekey_pending"
            while True:
                pending.unlink(missing_ok=True)
                config.load_models()
                collect.rematch(con)
                collect.rescan(con, args.days)
                collect.hydrate(con)
                xmedia.attach_videos(con)
                classify.classify(con)
                collect.refresh_quotes(con)
                build.build(con)
                if not pending.exists():
                    break
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

    except BaseException as e:
        if logged:
            runlog.save(con, args.cmd, started, "error", repr(e))
        raise
    if logged:
        runlog.save(con, args.cmd, started, "ok")

if __name__ == "__main__":
    main()
