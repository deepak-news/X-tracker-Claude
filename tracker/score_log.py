"""A week of the AI's judgements, kept in scores.json for improving the rubric.

Every item the AI looks at -- on the Tech Desk and on the Tweets desk -- is
written down with what the AI was shown, the score it gave, its one-line
reason, and whether it was sent. Items it called old news or a duplicate
are written down too, since a wrong call there loses a story just as surely.

Nothing here changes what is sent. It only records answers the AI has
already given, so it costs no AI requests.

The file is public, like the rest of the repository; everything in it is
public news and official releases. It keeps the last KEEP_DAYS days, one
entry per line and oldest first, so each save changes only its two ends and
GitHub stores the history compactly.
"""

import datetime as dt
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILE = ROOT / "scores.json"
KEEP_DAYS = 7
TEXT_CHARS = 400        # enough to see what the AI saw, without bloating the file

_PENDING: list = []


def note(desk: str, post, verdict: str, score=None, headline: str = "", why: str = "") -> None:
    """Remember one judgement until save() is called.

    desk     "tech" or "tweets"
    verdict  "sent", "not sent", "old news" or "duplicate"
    """
    try:
        score = None if score is None else round(float(score), 1)
    except (TypeError, ValueError):
        score = None
    _PENDING.append({
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "desk": desk,
        "source": post.handle,
        "id": str(post.id),
        "score": score,
        "verdict": verdict,
        "headline": headline,
        "why": why,
        "text": " ".join((post.text or "").split())[:TEXT_CHARS],
        "url": post.url or "",
    })


def note_rows(desk: str, rows: list, threshold: float) -> None:
    """Every (post, score, headline, why) the AI returned, against the threshold."""
    for post, value, headline, why in rows:
        note(desk, post, "sent" if value >= threshold else "not sent", value, headline, why)


def _load(path: pathlib.Path) -> list:
    try:
        entries = json.loads(path.read_text())
        return entries if isinstance(entries, list) else []
    except (OSError, ValueError):
        return []


def _write(path: pathlib.Path, entries: list) -> None:
    lines = ",\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in entries)
    path.write_text("[\n" + lines + "\n]\n" if entries else "[]\n")


def _trim(entries: list) -> list:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    return sorted((e for e in entries if e.get("at", "") >= cutoff), key=lambda e: e.get("at", ""))


def save() -> None:
    """Add this run's judgements to the file. Never lets a failure here
    stop an alert: the log is a convenience, the alerts are the job."""
    global _PENDING
    if not _PENDING:
        return
    try:
        _write(FILE, _trim(_load(FILE) + _PENDING))
        print(f"  (kept {len(_PENDING)} AI judgement(s) in scores.json)")
        _PENDING = []
    except Exception as exc:                                      # noqa: BLE001
        print(f"  (could not update scores.json: {exc})")


def merge(ours_path: str, theirs_path: str) -> list:
    """For the save step: two overlapping runs, every judgement kept once."""
    combined, keys = [], set()
    for entry in _load(pathlib.Path(theirs_path)) + _load(pathlib.Path(ours_path)):
        key = (entry.get("desk"), entry.get("id"), entry.get("at"))
        if key not in keys:
            keys.add(key)
            combined.append(entry)
    return _trim(combined)


if __name__ == "__main__":
    import sys
    # python -m tracker.score_log ours.json theirs.json > merged.json
    entries = merge(sys.argv[1], sys.argv[2])
    out = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else None
    if out:
        _write(out, entries)
    else:
        print(json.dumps(entries, ensure_ascii=False))
