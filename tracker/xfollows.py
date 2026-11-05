"""The accounts @DeepakatPTI follows, from the Google Sheet (apps-script/x_feed.gs, read daily
there), handed to the website for its Admin page: which have the bell off, which go with no
source. Reads and hands over only; nothing is alerted.
"""
import os
import sys

import requests

from . import sources, website


def main() -> int:
    url = os.environ.get("X_FEED_URL", "").strip()
    if not url:
        print("No X_FEED_URL secret here -- nothing to read.")
        return 0
    data = requests.get(url, params={"following": 1}, timeout=sources.TIMEOUT * 3).json()
    accounts = data.get("accounts")
    if accounts is None:
        print("The sheet does not know ?following=1 yet: its web app needs a new version (Deploy > Manage deployments).")
        return 1
    print(f"{len(accounts)} account(s), read {data.get('following_ok') or 'never'}; problem: {data.get('error') or 'none'}")
    handed = website.follows(accounts, data.get("following_ok") or "", data.get("error") or "")
    return 0 if handed and accounts else 1


if __name__ == "__main__":
    sys.exit(main())
