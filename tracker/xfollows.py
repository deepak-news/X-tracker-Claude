"""Which X accounts are live, for the website's Admin page. X no longer gives the follow list
(friends/list answers 404, Nov 5), so this goes by what the sheet saw: since the sheet keeps only
the posts its notifications point at, an account in it is followed with the bell on. Rows from
before that fix can be someone else's post that a followed account reposted, so those count only
when no other row explains them (xaudit.py). Reads and hands over only; nothing is alerted.
"""
import datetime as dt
import os
import re
import sys

import requests

from . import sources, website
from .xaudit import QUOTE, RT, squeeze

DAYS = 7
CLEAN_FROM = "2026-11-05T12:50:00+00:00"       # when the fixed script went into the sheet


def live(posts: list) -> dict:
    """{handle: name} of the accounts these rows show to be followed with the bell on."""
    clean = dt.datetime.fromisoformat(CLEAN_FROM)
    reposted, quoted, replied = {}, set(), set()
    for p in posts:
        text = str(p.get("text") or "")
        m = RT.match(text)
        if m:
            reposted.setdefault(m.group(1).lower(), []).append(squeeze(m.group(2).rstrip("…"))[:40])
        quoted.update(QUOTE.findall(text))
        if p.get("reply"):
            replied.update(h.lower() for h in re.findall(r"^(?:@\w+\s+)*@(\w+)", text))
    found = {}
    for p in posts:
        handle, text = str(p.get("handle") or ""), str(p.get("text") or "")
        try:
            at = dt.datetime.fromisoformat(str(p.get("at")).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=dt.timezone.utc)
        except ValueError:               # a row the sheet saved without its time: left out, not a failed run
            continue
        flat, low = squeeze(text), handle.lower()
        explained = (any(flat.startswith(x) for x in reposted.get(low, []) if x)
                     or str(p.get("id")) in quoted or low in replied)
        if handle and (at >= clean or p.get("repost") or p.get("reply") or not explained):
            found[handle] = str(p.get("name") or "")
    return found


def main() -> int:
    url = os.environ.get("X_FEED_URL", "").strip()
    if not url:
        print("No X_FEED_URL secret here -- nothing to read.")
        return 0
    data = requests.get(url, params={"hours": DAYS * 24}, timeout=sources.TIMEOUT * 3).json()
    found = live(data.get("posts") or [])
    print(f"{len(found)} account(s) live in the last {DAYS} days")
    accounts = [{"handle": h, "name": n, "bell": None} for h, n in sorted(found.items())]
    handed = website.follows(accounts, data.get("last_ok") or "", data.get("error") or "")
    return 0 if handed and accounts else 1


if __name__ == "__main__":
    sys.exit(main())
