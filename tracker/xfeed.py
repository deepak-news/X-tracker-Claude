"""Posts from X, by way of a Google Sheet.

X refuses GitHub's servers but answers Google's (tested 14 Oct 2026). So a
small script inside a Google Sheet (apps-script/x_feed.gs) reads
@DeepakatPTI's bell-notification feed every five minutes and keeps what it
finds, and this asks it for the recent posts. Only accounts followed with
the bell on ever appear. Reposts and replies to other people are dropped
here; the account's own post is what counts. Used by xdesk.py.

The sheet's address is the X_FEED_URL secret. Without it, nothing is read.
"""

import datetime as dt
import os

import requests

from . import sources

# The sheet reads X every five minutes. Longer than this without a good read
# and X has stopped answering -- nearly always the login expiring.
STALE_MINUTES = 30
HOURS = 12

_FEED: dict = {}


def enabled() -> bool:
    return bool(os.environ.get("X_FEED_URL", "").strip())


def _feed() -> list:
    """The sheet's recent posts, asked for once per run."""
    if "data" not in _FEED:
        response = requests.get(os.environ["X_FEED_URL"].strip(), params={"hours": HOURS},
                                timeout=sources.TIMEOUT)
        response.raise_for_status()
        _FEED["data"] = response.json()
    data = _FEED["data"]
    last_ok = data.get("last_ok") or ""
    behind = None
    if last_ok:
        behind = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_ok.replace("Z", "+00:00"))
    if behind is None or behind > dt.timedelta(minutes=STALE_MINUTES):
        raise RuntimeError(
            f"the Google Sheet has not read X since {last_ok[:16] or 'it was set up'} "
            f"({data.get('error') or 'no reason given'}). Usually the @DeepakatPTI login has "
            "expired: copy fresh auth_token and ct0 into the sheet's Script properties.")
    return data.get("posts") or []


def posts(handles=None, label: str = "X") -> list:
    """Posts from the accounts in handles, or from every account when it is None."""
    wanted = None if handles is None else {str(h).lower().lstrip("@") for h in handles}
    found = []
    for item in _feed():
        handle = str(item.get("handle") or "")
        if item.get("repost") or item.get("reply"):
            continue
        if wanted is not None and handle.lower() not in wanted:
            continue
        name = item.get("name") or handle
        found.append(sources.Post(
            id=str(item["id"]),
            handle=label,
            text=f"@{handle} ({name}) on X: " + " ".join(str(item.get("text") or "").split())[:1100],
            url=item.get("url") or f"https://x.com/{handle}/status/{item['id']}",
            created_at=str(item["at"]).replace("Z", "+00:00"),
            likes=0,
            reposts=0,
            is_reply=False,
            is_repost=False,
            origin=f"@{handle}",
        ))
    return found
