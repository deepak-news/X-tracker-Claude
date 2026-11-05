"""One-off (2026-11-05): which accounts in the X sheet are really followed. A post by an account
nobody follows reaches the sheet only because a followed account reposted, quoted or replied to
it, so every one of its posts has such a row beside it. A followed account has posts that do not.
Writes xaudit.json (handle, posts, posts explained by another row); reads only.
"""
import collections
import json
import os
import re

import requests

from . import sources

HOURS = 24 * 10
QUOTE = re.compile(r"(?:x|twitter)\.com/\w+/status/(\d+)", re.I)
RT = re.compile(r"^RT @(\w+): (.*)", re.S)


def squeeze(text: str) -> str:
    return re.sub(r"\W+", "", re.sub(r"https?://\S+", "", text)).lower()


def main() -> None:
    data = requests.get(os.environ["X_FEED_URL"].strip(), params={"hours": HOURS}, timeout=sources.TIMEOUT * 3).json()
    posts = data.get("posts") or []
    print(f"{len(posts)} rows, last read {data.get('last_ok')}")
    reposted, quoted, replied = collections.defaultdict(list), set(), set()
    for p in posts:
        text = str(p.get("text") or "")
        m = RT.match(text)
        if m:
            reposted[m.group(1).lower()].append(squeeze(m.group(2).rstrip("…"))[:40])
        quoted.update(QUOTE.findall(text))
        if p.get("reply"):
            replied.update(h.lower() for h in re.findall(r"^(?:@\w+\s+)*@(\w+)", text))
    seen = collections.defaultdict(lambda: {"own": 0, "explained": 0, "sample": ""})
    for p in posts:
        if p.get("repost") or p.get("reply"):
            continue
        handle, text = str(p.get("handle") or ""), str(p.get("text") or "")
        row = seen[handle]
        row["own"] += 1
        flat = squeeze(text)
        by_rt = any(flat.startswith(x) for x in reposted.get(handle.lower(), []) if x)
        if by_rt or str(p.get("id")) in quoted or handle.lower() in replied:
            row["explained"] += 1
        elif not row["sample"]:
            row["sample"] = text[:80]
    out = [{"handle": h, **v} for h, v in sorted(seen.items(), key=lambda kv: -kv[1]["own"])]
    with open("xaudit.json", "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print(json.dumps(out, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    main()
