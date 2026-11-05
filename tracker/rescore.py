"""One-off re-scoring (the user's ask, 2026-10-30): the general desk's score for the website's
items of the last hours, with the scale as it stands now (judge.GENERAL_SCALE). It only
writes rescore.json, kept as the run's artifact; nothing is handed to the website, so no one
is alerted. The scores are put into the website's database by hand from that file.

    RESCORE_SOURCES="official|PIB,watch|DoPT" RESCORE_HOURS=24 python -m tracker.rescore
"""
import json
import os

from . import judge, website


def gazettes(ids: list, hours: float) -> None:
    """RESCORE_IDS (2026-11-05): gazettes whose English text was missed, read again and judged
    by both desks (national = general news, bureau = beat), as a normal run would. Writes
    rescore.json with each id's scores, notes and the text read; put in by hand."""
    import yaml                                                       # noqa: PLC0415
    from . import national                                            # noqa: PLC0415
    from .sources import Post                                         # noqa: PLC0415
    whole = yaml.safe_load(national.WATCHLIST.read_text()) or {}
    items = [it for it in website.recent(["official|Gazette"], hours=hours) or [] if it["id"] in ids]
    print(f"found {len(items)} of {len(ids)} gazette(s)")
    out = []
    for it in items:
        text = national._gazette_text(it["url"])
        head = str(it.get("body") or "").split(" TEXT: ")[0]
        post = Post(id=it["id"], handle=f"Gazette {it.get('sub') or ''}".strip(),
                    text=f"{it['title']}. {head}" + (f" TEXT: {text}" if text else ""),
                    url=it["url"], created_at="", likes=0, reposts=0, is_reply=False, is_repost=False)
        row = {"id": it["id"], "body": post.text, "scores": {}, "notes": {}}
        for view, desk in (("national", national.NATIONAL), ("bureau", national.BUREAU)):
            rubric = ((whole.get(desk["section"]) or {}).get("newsworthy") or "").strip()
            entries, _ = judge.ask_json(national._prompt([post], rubric, [], desk["prompt"]), set())
            for e in entries:
                if int(e.get("i", -1)) == 0:
                    row["scores"][view] = float(e.get("score", 0))
                    row["notes"][view] = {"h": str(e.get("headline") or "").strip(),
                                          "w": str(e.get("why") or "").strip(),
                                          "e": str(e.get("event") or "").strip()}
        print(json.dumps(row, ensure_ascii=False)[:1500])
        out.append(row)
    with open("rescore.json", "w") as f:
        json.dump(out, f)


def main() -> None:
    ids = [s.strip() for s in os.environ.get("RESCORE_IDS", "").split(",") if s.strip()]
    if ids:
        gazettes(ids, float(os.environ.get("RESCORE_HOURS", "24")))
        return
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
