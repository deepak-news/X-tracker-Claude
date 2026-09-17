"""Combines this run's state.json with whatever is already on the branch.

Two runs can occasionally overlap -- GitHub's own schedule firing at the
same moment as the external timer, for instance. When that happens the
loser used to fail on a merge conflict and throw its progress away, which
meant re-reading and re-sending everything it had just done.

Nothing in state.json actually conflicts: every field only moves forward.
So rather than merge text, this combines the two by meaning -- the furthest
bookmark, every remembered alert, the later timestamp.

    python -m tracker.merge_state ours.json theirs.json > merged.json
"""

import json
import sys


def merge_watch(ours: dict, theirs: dict) -> dict:
    """The government watch keeps its own memory in the same file."""
    merged = dict(theirs)

    # Identifiers of rows already reported. Order is only cosmetic; what
    # matters is that no run forgets what the other one sent.
    reported, already = [], set()
    for key in (theirs.get("seen") or []) + (ours.get("seen") or []):
        if key not in already:
            already.add(key)
            reported.append(key)
    merged["seen"] = reported[-2000:]

    # For a watched page, the more recently checked snapshot is the truth.
    pages = dict(theirs.get("pages") or {})
    for url, snapshot in (ours.get("pages") or {}).items():
        current = pages.get(url) or {}
        if snapshot.get("checked", "") >= current.get("checked", ""):
            pages[url] = snapshot
    merged["pages"] = pages
    return merged


def merge_national(ours: dict, theirs: dict) -> dict:
    """The national desk's memory: what was read, and what was sent."""
    merged = dict(theirs)
    for field, cap in (("seen", 3000), ("gazette_scanned", 2000)):
        combined, already = [], set()
        for key in (theirs.get(field) or []) + (ours.get(field) or []):
            if key not in already:
                already.add(key)
                combined.append(key)
        merged[field] = combined[-cap:]

    alerted, keys = [], set()
    for entry in (theirs.get("alerted") or []) + (ours.get("alerted") or []):
        key = entry.get("url") or json.dumps(entry.get("words"), sort_keys=True)
        if key not in keys:
            keys.add(key)
            alerted.append(entry)
    merged["alerted"] = sorted(alerted, key=lambda e: e.get("at", ""))[-400:]
    merged["last_run"] = max(ours.get("last_run", ""), theirs.get("last_run", ""))
    # Sources already introduced; once introduced, always introduced.
    merged["sources"] = sorted(set(theirs.get("sources") or []) | set(ours.get("sources") or []))
    # The run that finished later has the current picture of which sites fail.
    newer = ours if ours.get("last_run", "") >= theirs.get("last_run", "") else theirs
    merged["source_failures"] = newer.get("source_failures") or {}
    return merged


def merge(ours: dict, theirs: dict) -> dict:
    merged = dict(theirs)
    merged.update({k: v for k, v in ours.items()
                   if k not in ("seen", "alerted", "dead_models", "watch", "national",
                                "news_queue", "last_digest_hour",
                                "page_links")})

    if ours.get("national") or theirs.get("national"):
        merged["national"] = merge_national(ours.get("national") or {},
                                            theirs.get("national") or {})

    if ours.get("watch") or theirs.get("watch"):
        merged["watch"] = merge_watch(ours.get("watch") or {},
                                      theirs.get("watch") or {})

    # A bookmark only ever advances, so the higher number is the true one.
    seen = dict(theirs.get("seen", {}))
    for handle, mark in (ours.get("seen") or {}).items():
        try:
            seen[handle] = max(int(mark), int(seen.get(handle, 0)))
        except (TypeError, ValueError):
            seen[handle] = mark
    merged["seen"] = seen

    # Keep every story either run remembered sending, newest last.
    alerted, seen_urls = [], set()
    for entry in (theirs.get("alerted") or []) + (ours.get("alerted") or []):
        key = entry.get("url") or json.dumps(entry.get("words"), sort_keys=True)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        alerted.append(entry)
    merged["alerted"] = sorted(alerted, key=lambda e: e.get("at", ""))[-400:]

    # A model being out of quota is a fact about the key, not about the run.
    dead = dict(theirs.get("dead_models", {}))
    for name, when in (ours.get("dead_models") or {}).items():
        dead[name] = max(when, dead.get(name, ""))
    merged["dead_models"] = dead

    # Links seen on company pages. The EARLIER first sighting is the true
    # one -- it is what the post's id is built from.
    pages = {label: dict(links) for label, links in (theirs.get("page_links") or {}).items()}
    for label, links in (ours.get("page_links") or {}).items():
        mine = pages.setdefault(label, {})
        for url, first in links.items():
            if url not in mine or first < mine[url]:
                mine[url] = first
    merged["page_links"] = pages

    # Stories waiting for the hourly digest. A story either run has already
    # emailed must not come back into the queue from the other run's copy.
    sent_urls = {e.get("url") for e in alerted if e.get("url")}
    queue, queued = [], set()
    for entry in (theirs.get("news_queue") or []) + (ours.get("news_queue") or []):
        url = (entry.get("post") or {}).get("url")
        if url in queued or url in sent_urls:
            continue
        queued.add(url)
        queue.append(entry)
    merged["news_queue"] = queue

    for field in ("last_success", "last_digest_hour"):
        latest = max(theirs.get(field, ""), ours.get(field, ""))
        if latest:
            merged[field] = latest
    return merged


def _load(path: str) -> dict:
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def merge_mail_log(ours: dict, theirs: dict) -> dict:
    """Every email either run sent, counted once; the later warning time."""
    sent, keys = [], set()
    for entry in (theirs.get("sent") or []) + (ours.get("sent") or []):
        key = (entry.get("at"), entry.get("list"), entry.get("n"))
        if key not in keys:
            keys.add(key)
            sent.append(entry)
    return {"sent": sorted(sent, key=lambda e: e.get("at", 0)),
            "warned": max(ours.get("warned", 0), theirs.get("warned", 0))}


if __name__ == "__main__":
    if sys.argv[1] == "--mail-log":
        merged = merge_mail_log(_load(sys.argv[2]), _load(sys.argv[3]))
        json.dump(merged, sys.stdout, indent=1, sort_keys=True)
    else:
        ours, theirs = _load(sys.argv[1]), _load(sys.argv[2])
        json.dump(merge(ours, theirs), sys.stdout, indent=2, sort_keys=True)
    print()
