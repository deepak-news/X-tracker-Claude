"""Reads recent posts from X.

This is the ONLY file that knows how X works. When X breaks free access
again, this is the file that gets rewritten -- nothing else changes.

STRATEGY (this matters):
  We do NOT ask X about each account separately every run. With ~96
  accounts checked every 10 minutes that would be ~14,000 requests a day
  from one account, and the burner would be locked within days.

  Instead the burner keeps an X LIST containing every watched account, and
  we read that list's timeline -- one stream containing everyone, in a
  handful of requests. A list does NOT require following the accounts,
  which matters because new accounts hit follow limits almost immediately.

  A direct sweep then re-checks the priority accounts every run and rotates
  through the rest, so the tracker still works before the list exists and
  still catches anything the list timeline skips.
"""

import os
from dataclasses import dataclass

from twscrape import API, gather

COOKIE_ENV = "X_COOKIES"
LIST_ENV = "X_LIST_ID"


@dataclass
class Post:
    id: str
    handle: str
    text: str
    url: str
    created_at: str
    likes: int
    reposts: int
    is_reply: bool
    is_repost: bool


class XUnavailable(RuntimeError):
    """X itself is the problem -- not the watchlist, not the config."""


async def _connect() -> API:
    cookies = os.environ.get(COOKIE_ENV, "").strip()
    if not cookies:
        raise XUnavailable(f"No {COOKIE_ENV} secret found.")

    api = API("accounts.db")
    try:
        await api.pool.add_account(
            "tracker", "unused", "tracker@example.invalid", "unused", cookies=cookies
        )
    except Exception:
        pass  # already stored from an earlier run in this same job
    return api


def _to_post(t) -> Post | None:
    user = getattr(t, "user", None)
    if user is None or not getattr(user, "username", None):
        return None
    return Post(
        id=str(t.id),
        handle=user.username,
        text=(t.rawContent or "").strip(),
        url=t.url,
        created_at=t.date.isoformat() if t.date else "",
        likes=getattr(t, "likeCount", 0) or 0,
        reposts=getattr(t, "retweetCount", 0) or 0,
        is_reply=getattr(t, "inReplyToTweetId", None) is not None,
        is_repost=getattr(t, "retweetedTweet", None) is not None,
    )


async def _list_feed(api, wanted: set[str], limit: int) -> list[Post]:
    """One cheap stream covering every account in the burner's X list."""
    list_id = os.environ.get(LIST_ENV, "").strip()
    if not list_id:
        return []  # no list yet - the sweep below carries the whole load

    posts = []
    for t in await gather(api.list_timeline(int(list_id), limit=limit)):
        post = _to_post(t)
        # Keep only accounts on the watchlist, in case the list drifts.
        if post and post.handle.lower() in wanted:
            posts.append(post)
    return posts


async def _sweep(api, handles: list[str], id_cache: dict, per_account: int):
    """Check accounts directly. Works whether or not they are followed."""
    posts, failed = [], []
    for handle in handles:
        try:
            user_id = id_cache.get(handle.lower())
            if not user_id:
                user = await api.user_by_login(handle)
                if user is None:
                    failed.append(handle)
                    continue
                user_id = str(user.id)
                # Looking a handle up costs a request, so remember it forever.
                id_cache[handle.lower()] = user_id

            for t in await gather(api.user_tweets(int(user_id), limit=per_account)):
                post = _to_post(t)
                if post:
                    posts.append(post)
        except Exception as exc:  # noqa: BLE001 - one bad handle must not kill the run
            print(f"  ! sweep failed for @{handle}: {exc}")
            failed.append(handle)
    return posts, failed


async def collect(handles: list[str], cfg: dict, id_cache: dict, sweep_offset: int,
                  priority: list[str]):
    """Returns (posts, handles_that_failed, next_sweep_offset)."""
    api = await _connect()
    wanted = {h.lower() for h in handles}

    feed = await _list_feed(api, wanted, int(cfg.get("timeline_limit", 150)))
    print(f"  list timeline: {len(feed)} posts"
          + ("" if os.environ.get(LIST_ENV) else " (no X_LIST_ID set yet)"))

    prio = [h for h in priority if h]
    prio_lower = {h.lower() for h in prio}
    rest = [h for h in handles if h.lower() not in prio_lower]

    size = int(cfg.get("sweep_size", 10))
    rotating = [rest[(sweep_offset + i) % len(rest)] for i in range(min(size, len(rest)))] if rest else []
    batch = prio + rotating

    swept, failed = await _sweep(api, batch, id_cache, int(cfg.get("per_account_limit", 10)))
    print(f"  sweep: {len(swept)} posts from {len(prio)} priority + {len(rotating)} rotating")

    if not feed and not swept:
        raise XUnavailable(
            "The list timeline and the direct sweep both came back empty. "
            "The burner session has most likely expired or been blocked."
        )

    # Keep ONLY accounts on the watchlist. user_tweets hands back quotes and
    # conversation items authored by people we never asked about; without this
    # they get scored by the AI and can end up in your inbox.
    merged: dict[str, Post] = {}
    for post in feed + swept:
        if post.handle.lower() in wanted:
            merged[post.id] = post

    next_offset = (sweep_offset + size) % max(len(rest), 1)
    return list(merged.values()), failed, next_offset
