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
import re

import requests

from . import sources

# The sheet reads X every five minutes. Longer than this without a good read
# and X has stopped answering -- nearly always the login expiring.
STALE_MINUTES = 30
HOURS = 12

_FEED: dict = {}

# X hands over a long post cut off at about 275 characters, ending "…" and a link (Nov 18:
# Jensen Huang's post offering to return as Nvidia's CEO was judged on its opening thanks
# and scored 2). The whole text comes from a public mirror of X that answers GitHub.
# X_FULL_TEXT=0 in the environment switches the fetching off.
FULL_TEXT_URL = "https://api.fxtwitter.com/{handle}/status/{id}"
MAX_TEXT = 4000
_TAIL = re.compile(r"(?:\s*https?://\S+)+\s*$")


def cut_off(text: str) -> bool:
    """True when X gave only the start of a long post."""
    body = _TAIL.sub("", str(text or "")).rstrip()
    return len(body) >= 180 and body.endswith(("…", "..."))


def _full_text(handle: str, post_id: str) -> str:
    """The whole post, or "" when it cannot be had."""
    if os.environ.get("X_FULL_TEXT", "1").strip() == "0":
        return ""
    try:
        response = requests.get(FULL_TEXT_URL.format(handle=handle, id=post_id),
                                headers={"User-Agent": "Mozilla/5.0 (news tracker)"},
                                timeout=sources.TIMEOUT)
        response.raise_for_status()
        tweet = response.json().get("tweet") or {}
        if str(tweet.get("id") or post_id) != str(post_id):
            return ""
        return str(tweet.get("text") or "")
    except Exception as exc:                                    # noqa: BLE001
        print(f"    could not read the whole of {handle}'s post {post_id}: {str(exc)[:100]}")
        return ""


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
        text = str(item.get("text") or "")
        post = sources.Post(
            id=str(item["id"]),
            handle=label,
            text=f"@{handle} ({name}) on X: " + " ".join(text.split())[:MAX_TEXT],
            url=item.get("url") or f"https://x.com/{handle}/status/{item['id']}",
            created_at=str(item["at"]).replace("Z", "+00:00"),
            likes=0,
            reposts=0,
            is_reply=False,
            is_repost=False,
            origin=f"@{handle}",
        )
        post.cut_off = cut_off(text)      # only the start: whole() fetches the rest
        found.append(post)
    return found


def whole(post) -> bool:
    """Puts the whole text into a post X gave only the start of. Called for the posts about to
    be judged only, never the whole twelve hours. False when it is still cut off."""
    if not getattr(post, "cut_off", False):
        return True
    lead, _, start = post.text.partition(" on X: ")
    full = _full_text(post.origin.lstrip("@"), post.id.split(":")[-1])
    if len(full) > len(_TAIL.sub("", start).rstrip(" ….")):
        post.text = f"{lead} on X: " + " ".join(full.split())[:MAX_TEXT]
        post.cut_off = False
    return not post.cut_off
