"""Data for the hidden /status/ page: pipeline health and LLM cost per day."""

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db


def _credits():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        d = httpx.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"},
                      timeout=15).json()["data"]
        return {"total": d["total_credits"], "used": d["total_usage"], "left": d["total_credits"] - d["total_usage"]}
    except Exception:  # noqa: BLE001 — the page must build without it
        return None


def collect(con) -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=30)).date().isoformat()
    q = lambda sql, *a: con.execute(sql, a).fetchall()

    days = defaultdict(lambda: {"cost": defaultdict(float), "runs": 0, "errors": 0, "counters": defaultdict(int)})
    for r in q("SELECT substr(ts,1,10) day, purpose, SUM(cost) c FROM llm_calls WHERE ts >= ? GROUP BY 1, 2", since):
        days[r["day"]]["cost"][r["purpose"]] += r["c"]
    for r in q("SELECT substr(started_at,1,10) day, status, counters FROM runs WHERE started_at >= ?", since):
        d = days[r["day"]]
        d["runs"] += 1
        d["errors"] += r["status"] != "ok"
        for k, v in json.loads(r["counters"] or "{}").items():
            d["counters"][k] += v
    purposes = sorted({p for d in days.values() for p in d["cost"]})
    table = []
    for day in sorted(days, reverse=True):
        d = days[day]
        table.append(dict(day=day, total=sum(d["cost"].values()), cost=dict(d["cost"]), runs=d["runs"],
                          errors=d["errors"], counters=dict(d["counters"])))
    peak = max((t["total"] for t in table), default=0) or 1

    week = [t["total"] for t in table if t["day"] >= (now - timedelta(days=7)).date().isoformat()]
    runs = [dict(r, counters=json.loads(r["counters"] or "{}"))
            for r in q("SELECT * FROM runs ORDER BY started_at DESC LIMIT 30")]
    last_ok = next((r for r in runs if r["status"] == "ok" and r["command"] == "update"), None)
    tracking_since = q("SELECT MIN(ts) t FROM llm_calls")[0]["t"]

    pipeline = q(f"""SELECT COUNT(*) candidates, SUM(hydrated=0) unhydrated, SUM(is_report IS NULL) ungraded,
                     SUM(is_report=1 AND score>={config.MIN_SCORE}) shown FROM reports""")[0]
    return dict(
        now=now.isoformat(timespec="seconds"), table=table, peak=peak, purposes=purposes, runs=runs,
        last_ok=last_ok, credits=_credits(), tracking_since=tracking_since,
        avg_week=sum(week) / 7 if week else 0, month=sum(t["total"] for t in table),
        pipeline=dict(pipeline), cursor=db.get_state(con, "updated_cursor"),
        models_checked=db.get_state(con, "models_checked_at"),
        summaries=q("SELECT slug, updated_at, n_reports FROM model_summaries ORDER BY updated_at DESC"),
        timer_minutes=60,
    )
