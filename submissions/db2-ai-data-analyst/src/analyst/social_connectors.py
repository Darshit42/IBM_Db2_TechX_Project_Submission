"""Collectors that turn platform data into ``Post`` objects for SocialStore.ingest().

* Reddit  - official OAuth API (free "script" app: REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET).
* X       - API v2 recent search (paid tier: X_BEARER_TOKEN).
* File    - CSV or JSON export from any platform or social-listening tool.

Nothing here scrapes HTML; each connector uses the platform's documented API and is only
active when its credentials are present in .env.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
from pathlib import Path

import httpx

from .social import PLATFORMS, Post

USER_AGENT = "db2-ai-data-analyst/1.0 (social listening demo)"


class ConnectorError(RuntimeError):
    pass


# --------------------------------------------------------------------------- Reddit
def reddit_token(client_id: str, client_secret: str, client: httpx.Client) -> str:
    r = client.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": USER_AGENT},
    )
    if r.status_code != 200 or "access_token" not in r.json():
        raise ConnectorError(f"Reddit auth failed ({r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def parse_reddit_listing(payload: dict) -> list[Post]:
    out = []
    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        text = " ".join(x for x in (d.get("title"), d.get("selftext") or d.get("body")) if x).strip()
        if not text:
            continue
        out.append(Post(
            external_id=f"reddit-{d.get('name') or d.get('id')}",
            platform="REDDIT",
            channel=f"r/{d.get('subreddit', 'unknown')}",
            author=f"u/{d.get('author', 'unknown')}",
            posted_at=dt.datetime.fromtimestamp(float(d.get("created_utc", 0)), dt.timezone.utc).replace(tzinfo=None),
            text=text[:1000],
            url=f"https://www.reddit.com{d['permalink']}" if d.get("permalink") else None,
            engagement=int(d.get("score", 0)) + int(d.get("num_comments", 0)),
        ))
    return out


def fetch_reddit(query: str, subreddit: str | None = None, limit: int = 100,
                 client_id: str | None = None, client_secret: str | None = None,
                 client: httpx.Client | None = None) -> list[Post]:
    client_id = client_id or os.getenv("REDDIT_CLIENT_ID", "")
    client_secret = client_secret or os.getenv("REDDIT_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        raise ConnectorError("Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET (create a 'script' app at "
                             "https://www.reddit.com/prefs/apps).")
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        token = reddit_token(client_id, client_secret, client)
        path = f"/r/{subreddit.removeprefix('r/')}/search" if subreddit else "/search"
        params = {"q": query, "limit": min(limit, 100), "sort": "new", "type": "link"}
        if subreddit:
            params["restrict_sr"] = "1"
        r = client.get(f"https://oauth.reddit.com{path}", params=params,
                       headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
        if r.status_code != 200:
            raise ConnectorError(f"Reddit search failed ({r.status_code}): {r.text[:200]}")
        return parse_reddit_listing(r.json())
    finally:
        if own:
            client.close()


# --------------------------------------------------------------------------- X
def parse_x_search(payload: dict) -> list[Post]:
    users = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
    out = []
    for t in payload.get("data", []):
        user = users.get(t.get("author_id"), {})
        handle = user.get("username", t.get("author_id", "unknown"))
        m = t.get("public_metrics", {})
        out.append(Post(
            external_id=f"x-{t['id']}",
            platform="X",
            channel=f"@{handle}",
            author=f"@{handle}",
            posted_at=dt.datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")).replace(tzinfo=None),
            text=t.get("text", "")[:1000],
            url=f"https://x.com/{handle}/status/{t['id']}",
            engagement=int(m.get("like_count", 0)) + int(m.get("reply_count", 0)) + int(m.get("retweet_count", 0)),
        ))
    return out


def fetch_x(query: str, limit: int = 100, bearer: str | None = None,
            client: httpx.Client | None = None) -> list[Post]:
    bearer = bearer or os.getenv("X_BEARER_TOKEN", "")
    if not bearer:
        raise ConnectorError("Set X_BEARER_TOKEN (X API v2; recent search needs a paid access tier).")
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        r = client.get(
            "https://api.x.com/2/tweets/search/recent",
            params={"query": f"{query} -is:retweet", "max_results": max(10, min(limit, 100)),
                    "tweet.fields": "created_at,public_metrics,author_id", "expansions": "author_id",
                    "user.fields": "username"},
            headers={"Authorization": f"Bearer {bearer}", "User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            raise ConnectorError(f"X search failed ({r.status_code}): {r.text[:200]}")
        return parse_x_search(r.json())
    finally:
        if own:
            client.close()


# --------------------------------------------------------------------------- files
FILE_FIELDS = "platform, channel, author, posted_at (ISO), text, [url, engagement, id]"


def load_file(path: str | Path, default_platform: str = "OTHER") -> list[Post]:
    """CSV or JSON (list of objects) with the columns listed in FILE_FIELDS."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        records = json.loads(path.read_text(encoding="utf-8"))
    else:
        with path.open(encoding="utf-8", newline="") as fh:
            records = list(csv.DictReader(fh))
    out = []
    for i, rec in enumerate(records, start=1):
        rec = {str(k).strip().lower(): v for k, v in rec.items()}
        text = (rec.get("text") or "").strip()
        if not text:
            continue
        platform = str(rec.get("platform") or default_platform).upper()
        out.append(Post(
            external_id=str(rec.get("id") or f"file-{path.stem}-{i}"),
            platform=platform if platform in PLATFORMS else "OTHER",
            channel=str(rec.get("channel") or path.stem),
            author=str(rec.get("author") or "unknown"),
            posted_at=dt.datetime.fromisoformat(str(rec.get("posted_at")).replace("Z", "+00:00")).replace(tzinfo=None),
            text=text[:1000],
            url=rec.get("url") or None,
            engagement=int(float(rec.get("engagement") or 0)),
        ))
    return out
