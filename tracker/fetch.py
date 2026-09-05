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

import asyncio
import os
from dataclasses import dataclass

from twscrape import API, gather

COOKIE_ENV = "X_COOKIES"
LIST_ENV = "X_LIST_ID"

# When X rate-limits a request, twscrape locks the account for that queue
# and quietly WAITS for the lock to clear (up to 15 minutes) instead of
# raising -- which once blocked a run long enough that GitHub's own job
# timeout had to kill it. This bounds any single X call to a sane wait, so
# a lock shows up as a fast, ordinary failure instead of a multi-minute hang.
PER_CALL_TIMEOUT = 30


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

    if "auth_token=" not in cookies or "ct0=" not in cookies:
        raise XUnavailable(
            f"{COOKIE_ENV} does not look right. It must contain both auth_token "
            "and ct0, on one line, like: auth_token=VALUE; ct0=VALUE"
        )

    api = API("accounts.db")
    try:
        await api.pool.add_account(
            "tracker", "unused", "tracker@example.invalid", "unused", cookies=cookies
        )
    except Exception as exc:  # noqa: BLE001
        # Adding can legitimately fail because the account is already stored
        # from earlier in this same job. Anything else is a real problem, and
        # swallowing it silently turns a bad cookie into a misleading
        # "session expired" report further down.
        print(f"  ! add_account said: {exc}")

    # Whatever happened above, the pool must actually hold a usable account --
    # otherwise every query below quietly returns nothing and the run blames X.
    accounts = await api.pool.get_all()
    if not accounts:
        raise XUnavailable(
            f"The {COOKIE_ENV} secret was rejected -- no usable account. "
            "Check the value is exactly: auth_token=VALUE; ct0=VALUE"
        )
    print(f"  session loaded ({len(accounts)} account in pool)")
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

    try:
        tweets = await asyncio.wait_for(
            gather(api.list_timeline(int(list_id), limit=limit)), timeout=PER_CALL_TIMEOUT
        )
    except asyncio.TimeoutError:
        print(f"  ! list timeline did not respond within {PER_CALL_TIMEOUT}s "
              "(account likely rate-limited by X) -- skipping it this run")
        return []

    posts = []
    for t in tweets:
        post = _to_post(t)
        # Keep only accounts on the watchlist, in case the list drifts.
        if post and post.handle.lower() in wanted:
            posts.append(post)
    return posts


async def _sweep(api, handles: list[str], id_cache: dict, per_account: int):
    """Check accounts directly. Works whether or not they are followed."""
    posts, failed = [], []
    for i, handle in enumerate(handles):
        try:
            user_id = id_cache.get(handle.lower())
            if not user_id:
                user = await asyncio.wait_for(api.user_by_login(handle), timeout=PER_CALL_TIMEOUT)
                if user is None:
                    failed.append(handle)
                    continue
                user_id = str(user.id)
                # Looking a handle up costs a request, so remember it forever.
                id_cache[handle.lower()] = user_id

            tweets = await asyncio.wait_for(
                gather(api.user_tweets(int(user_id), limit=per_account)), timeout=PER_CALL_TIMEOUT
            )
            for t in tweets:
                post = _to_post(t)
                if post:
                    posts.append(post)
        except asyncio.TimeoutError:
            # The account itself is rate-limited, not this one handle -- every
            # remaining handle would time out the same way, so stop burning
            # the run's time budget re-discovering that.
            print(f"  ! @{handle} timed out after {PER_CALL_TIMEOUT}s (account likely "
                  f"rate-limited) -- stopping the sweep, {len(handles) - i} handle(s) skipped")
            failed.extend(handles[i:])
            break
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
