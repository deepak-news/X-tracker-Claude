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


def merge(ours: dict, theirs: dict) -> dict:
    merged = dict(theirs)
    merged.update({k: v for k, v in ours.items()
                   if k not in ("seen", "alerted", "dead_models", "watch")})

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

    for field in ("last_success",):
        if theirs.get(field, "") > ours.get(field, ""):
            merged[field] = theirs[field]
    return merged


def _load(path: str) -> dict:
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


if __name__ == "__main__":
    ours, theirs = _load(sys.argv[1]), _load(sys.argv[2])
    json.dump(merge(ours, theirs), sys.stdout, indent=2, sort_keys=True)
    print()
