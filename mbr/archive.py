"""Thin client for the Community Archive API v1 (https://analytics.community-archive.org/v1/openapi.json)."""

import os
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import httpx

from . import config

PAGE = 200      # max tweets per page
MAX_WINDOW = 31  # days per /tweets time window

_client = httpx.Client(base_url=config.CA_URL, timeout=90)
_last = 0.0


def get(path: str, params: dict) -> dict:
    """One API call, paced under the per-minute limit and retried on 429/503/5xx."""
    global _last
    error = None
    for attempt in range(6):
        time.sleep(max(0.0, _last + 60 / config.CA_PER_MINUTE - time.monotonic()))
        _last = time.monotonic()
        try:
            r = _client.get(path, params=params,
                            headers={"Authorization": f"Bearer {os.environ['CA_API_KEY']}"})
            if r.status_code in (429, 503):
                error = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(int(r.headers.get("Retry-After") or 5 * (attempt + 1)))
                continue
            if r.status_code < 400:
                return r.json()
            if r.status_code < 500:
                raise RuntimeError(f"{path} {params}: HTTP {r.status_code}: {r.text[:300]}")
            error = f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx.TransportError as e:
            error = repr(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"giving up on {path}: {error}")


def _pages(path: str, params: dict) -> Iterator[list[dict]]:
    cursor = None
    while True:
        page = get(path, {**params, **({"cursor": cursor} if cursor else {})})
        if page["data"]:
            yield [_normalize(t) for t in page["data"]] if "tweet_id" in page["data"][0] else page["data"]
        cursor = page.get("next_cursor")
        if cursor is None:
            return


def _q(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan(since: datetime, until: datetime) -> Iterator[list[dict]]:
    """Yield pages of full tweets created in [since, until), oldest first (so a
    reply comes after the tweet it answers)."""
    while since < until:
        end = min(until, since + timedelta(days=MAX_WINDOW))
        yield from _pages("tweets", {"since": _q(since), "until": _q(end),
                                     "order": "asc", "limit": PAGE})
        since = end


def account_tweets(account_id: str, since: datetime) -> Iterator[list[dict]]:
    yield from _pages(f"accounts/{account_id}/tweets", {"since": _q(since), "order": "asc", "limit": PAGE})


def members() -> list[dict]:
    return [m for page in _pages("members", {"limit": 1000}) for m in page]


def _chunks(ids, n=100):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _ts(s: str | None) -> str | None:
    """API timestamps (2026-10-16T17:01:54.000Z) in the format already stored (...54+00:00)."""
    return s and datetime.fromisoformat(s).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _normalize(r: dict) -> dict:
    return dict(
        tweet_id=r["tweet_id"], account_id=r.get("account_id"), username=r.get("username"),
        display_name=r.get("account_display_name"), avatar=r.get("avatar_media_url"),
        created_at=_ts(r.get("created_at")), full_text=r.get("full_text") or "",
        likes=r.get("favorite_count") or 0, retweets=r.get("retweet_count") or 0,
        reply_to_tweet_id=r.get("reply_to_tweet_id"), reply_to_username=r.get("reply_to_username"),
        quoted_tweet_id=r.get("quoted_tweet_id"), retweeted_tweet_id=r.get("retweeted_tweet_id"),
        media=[dict(url=m["media_url"], type=m["media_type"], w=m["width"], h=m["height"])
               for m in r.get("media") or []],
        urls=[dict(url=u["url"], expanded=u["expanded_url"], display=u["display_url"])
              for u in r.get("urls") or []],
    )


def fetch_tweets(ids) -> dict[str, dict]:
    """Full tweets (author, counts, media, urls) keyed by id; missing ids are absent."""
    out = {}
    for chunk in _chunks(set(ids)):
        for r in get("tweets", {"ids": ",".join(chunk)})["data"]:
            out[r["tweet_id"]] = _normalize(r)
    return out


def accounts(ids) -> dict[str, dict]:
    out = {}
    for chunk in _chunks(set(ids)):
        for a in get("accounts", {"ids": ",".join(chunk)})["data"]:
            out[a["account_id"]] = a
    return out


def legacy_quoting_tweets(ids) -> list[tuple[str, str, str]]:
    """(quoting tweet, quoted tweet, quoting account) from the retired Supabase API,
    to seed the local quotes table once. Remove after the API shuts down on 2026-11-19."""
    h = {"apikey": config.CA_LEGACY_KEY, "Authorization": f"Bearer {config.CA_LEGACY_KEY}"}
    base = "https://fabxmporizzqflnftavs.supabase.co/rest/v1/"
    quotes: dict[str, str] = {}
    ids = list(ids)
    for i in range(0, len(ids), 80):
        offset = 0
        while True:
            rows = httpx.get(base + "quote_tweets", headers=h, timeout=90, params=[
                ("select", "tweet_id,quoted_tweet_id"), ("quoted_tweet_id", f"in.({','.join(ids[i:i + 80])})"),
                ("order", "tweet_id"), ("limit", "1000"), ("offset", str(offset))]).raise_for_status().json()
            quotes.update((r["tweet_id"], r["quoted_tweet_id"]) for r in rows if r["tweet_id"])
            if len(rows) < 1000:
                break
            offset += 1000
    out = []
    q = list(quotes)
    for i in range(0, len(q), 80):
        rows = httpx.get(base + "tweets", headers=h, timeout=90, params=[
            ("select", "tweet_id,account_id"), ("tweet_id", f"in.({','.join(q[i:i + 80])})")]).raise_for_status().json()
        out += [(r["tweet_id"], quotes[r["tweet_id"]], r["account_id"]) for r in rows if r["account_id"]]
    return out
