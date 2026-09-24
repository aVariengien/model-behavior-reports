"""SQLite storage. `tweets` caches every tweet we render (reports, parents,
quotes, replies); `reports` holds the model-mentioning tweets and their grades."""

import json
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id TEXT PRIMARY KEY,
    account_id TEXT,
    username TEXT,
    display_name TEXT,
    avatar TEXT,
    created_at TEXT,
    full_text TEXT,
    likes INTEGER DEFAULT 0,
    retweets INTEGER DEFAULT 0,
    reply_to_tweet_id TEXT,
    reply_to_username TEXT,
    quoted_tweet_id TEXT,
    media TEXT DEFAULT '[]',
    urls TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS reports (
    tweet_id TEXT PRIMARY KEY,
    created_at TEXT,
    candidates TEXT,           -- model slugs matched by keyword
    models TEXT,               -- model slugs confirmed by the classifier
    hydrated INTEGER DEFAULT 0,
    reply_ids TEXT DEFAULT '[]',
    is_report INTEGER,         -- NULL until classified
    kind TEXT,
    score INTEGER,
    behavior TEXT,
    classified_at TEXT
);
CREATE INDEX IF NOT EXISTS reports_created ON reports(created_at);

CREATE TABLE IF NOT EXISTS model_summaries (
    slug TEXT PRIMARY KEY,
    one_liner TEXT,
    aggregate TEXT,
    portrait TEXT,
    n_reports INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(reports)")}
    if "quotes" not in cols:
        con.execute("ALTER TABLE reports ADD COLUMN quotes INTEGER DEFAULT 0")
        con.execute("ALTER TABLE reports ADD COLUMN quotes_at TEXT")
        con.commit()
    return con


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(con, key, value):
    con.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (key, value))
    con.commit()


TWEET_COLS = ["tweet_id", "account_id", "username", "display_name", "avatar", "created_at",
              "full_text", "likes", "retweets", "reply_to_tweet_id", "reply_to_username",
              "quoted_tweet_id", "media", "urls"]


def upsert_tweet(con, t: dict):
    row = {c: t.get(c) for c in TWEET_COLS}
    row["media"] = json.dumps(t.get("media") or [])
    row["urls"] = json.dumps(t.get("urls") or [])
    con.execute(
        f"INSERT INTO tweets({','.join(TWEET_COLS)}) VALUES ({','.join('?' * len(TWEET_COLS))}) "
        "ON CONFLICT(tweet_id) DO UPDATE SET "
        + ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in TWEET_COLS[1:]),
        [row[c] for c in TWEET_COLS],
    )
