"""One-off re-scoring (the user's ask, 2026-10-30): the general desk's score for the website's
items of the last hours, with the scale as it stands now (judge.GENERAL_SCALE). It only
writes rescore.json, kept as the run's artifact; nothing is handed to the website, so no one
is alerted. The scores are put into the website's database by hand from that file.

    RESCORE_SOURCES="official|PIB,watch|DoPT" RESCORE_HOURS=24 python -m tracker.rescore
"""
import json
import os

from . import judge, website


def main() -> None:
    sources = [s.strip() for s in os.environ.get("RESCORE_SOURCES", "").split(",") if s.strip()]
    hours = float(os.environ.get("RESCORE_HOURS", "24"))
    items = website.recent(sources, hours=hours) or []
    print(f"{len(items)} item(s) from {len(sources)} source(s) in the last {hours:g} hours")
    out, dead = [], set()
    for n in range(0, len(items), judge.CHUNK):
        batch = items[n:n + judge.CHUNK]
        listing = "\n\n".join(f"[{i}] {it.get('source', '')}{' - ' + it['sub'] if it.get('sub') else ''}: "
                               f"{it.get('title', '')}. {it.get('body', '')}"[:judge.MAX_POST_CHARS]
                               for i, it in enumerate(batch))
        try:
            entries, newly_dead = judge.ask_json(judge.GENERAL_PROMPT.format(items=listing), dead)
        except Exception as exc:                                      # noqa: BLE001
            print(f"  ! batch {n // judge.CHUNK + 1} not scored ({str(exc)[:160]})")
            continue
        dead |= newly_dead
        for entry in entries:
            try:
                idx, value = int(entry.get("i", -1)), float(entry.get("general"))
            except (TypeError, ValueError, AttributeError):
                continue
            if 0 <= idx < len(batch) and 0 <= value <= 10:
                out.append({"id": batch[idx]["id"], "score": value,
                            "headline": str(entry.get("headline") or "").strip()[:300],
                            "why": str(entry.get("why") or "").strip()[:400]})
    with open("rescore.json", "w") as f:
        json.dump(out, f)
    print(f"scored {len(out)} of {len(items)}")


if __name__ == "__main__":
    main()
