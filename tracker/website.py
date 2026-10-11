"""Hands what the tracker finds to PTI Source Alerts, the website.

The website (pti-source-alerts.ptiinfographics.workers.dev) lets each reporter pick their own
sources, AI score bar and keywords, and alerts them by notification and in a feed. So it needs
everything, not only what the emails carry: every item each desk's AI judged, with its score
and one-line reason, and every new row on the watched pages. Who hears about what is decided
there, not here.

    official   the official stream (national.py): PIB, gazettes, RBI, SEBI, MEA, the
               regulators, ministries and states, earthquakes -- scored by the Tweets desk
               ("national" view) and by the bureau ("bureau" view)
    watch      the government watch pages (watch.py), and the pages read only for the
               website (site_sources.py): no AI, every new row counts
    tech       the Tech Desk's company and AI-lab newsrooms (main.py) -- primary sources
               only; Google News reports are never sent, and PIB and the regulators are
               already in the official stream

Switch: "site: send: true" in watchlist.yml; false stops every hand-off. Nothing here can stop
a run or an email: any failure is printed and the run carries on.

No secret is needed on GitHub: the workflow may ask GitHub for a token signed by GitHub that
says "this is a run of deepak-news/X-tracker-Claude on main" ("id-token: write" in check.yml),
and the website checks GitHub's signature. On the Mac, SITE_KEY in ~/.watch-env does the job.
"""

import os
import pathlib
import re

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "watchlist.yml"
AUDIENCE = "pti-source-alerts"
DEFAULT_URL = "https://pti-source-alerts.ptiinfographics.workers.dev"
TIMEOUT = 30
BATCH = 800          # items per request


def _cfg() -> dict:
    try:
        return (yaml.safe_load(WATCHLIST.read_text()) or {}).get("site") or {}
    except (OSError, yaml.YAMLError):
        return {}


def enabled() -> bool:
    return bool(_cfg().get("send"))


def _auth() -> str:
    """GitHub's signed token on GitHub; the Mac's key at home; nothing otherwise."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if url and token:
        answer = requests.get(url, params={"audience": AUDIENCE}, timeout=TIMEOUT,
                              headers={"Authorization": f"bearer {token}"})
        answer.raise_for_status()
        return "Bearer " + answer.json()["value"]
    key = os.environ.get("SITE_KEY", "").strip()
    return f"Key {key}" if key else ""


def send(origin: str, items: list, *, view: str = "", sources: list | None = None,
         failed: list | None = None, all_listed: bool = False, dry_run: bool = False) -> None:
    """One hand-off. origin names the step ("national", "bureau", "tech", "watch", "site").

    sources: every (group, source) the step reads -- or (group, source, its lists or
    ministries) -- so the website can offer a source, or PIB's Ministry of Civil Aviation,
    before it has published anything. all_listed: the items are everything the pages list,
    not only what is new; the website works out what is new (site_sources.py).
    """
    if not enabled():
        return
    catalogue: dict = {}
    for entry in sources or []:
        group, source, *rest = entry
        subs = rest[0] if rest else []
        catalogue.setdefault((group, source), set()).update([subs] if isinstance(subs, str) else subs)
    sources = sorted(catalogue)
    if dry_run:
        print(f"  (dry run) would hand {len(items)} item(s) to the website")
        return
    if not items and not sources and not failed:
        return
    try:
        auth = _auth()
        if not auth:
            print("  website: no way to sign the hand-off here (not on GitHub, no SITE_KEY) -- skipped")
            return
        url = (_cfg().get("url") or DEFAULT_URL).rstrip("/") + "/api/ingest"
        parts = [items[i:i + BATCH] for i in range(0, len(items), BATCH)] or [[]]
        fresh = 0
        for n, part in enumerate(parts):
            body = {"origin": origin, "view": view, "all_listed": all_listed, "items": part,
                    # the catalogue and failures once is enough
                    "sources": [{"grp": g, "source": s, "subs": sorted(x for x in catalogue[(g, s)] if x)}
                                for g, s in sources] if n == 0 else [],
                    "failed": (failed or []) if n == 0 else []}
            answer = requests.post(url, json=body, timeout=TIMEOUT,
                                   headers={"Authorization": auth, "User-Agent": "news-tracker"})
            answer.raise_for_status()
            fresh += int(answer.json().get("fresh", 0))
        print(f"  website: handed over {len(items)} item(s), {fresh} new there")
    except Exception as exc:                                          # noqa: BLE001
        print(f"  ! website hand-off failed ({str(exc)[:160]}); the emails are not affected")


# ---------------------------------------------------------------- what each item is

def _clean(text) -> str:
    return " ".join(str(text or "").split())


MEA_LISTS = {"MEA press releases": "Press releases", "MEA statements": "Statements"}
CCI_LISTS = {"cci-order": "Antitrust orders", "cci-merger": "Mergers",
             "cci-pr:media-gallery": "Press releases", "cci-pr:antitrust": "Antitrust press releases"}


def official_source(post) -> tuple:
    """(source, sub) for an item of the official stream, as people choose it on the website."""
    handle = post.handle
    if handle.startswith("PIB "):
        return "PIB", handle[4:]
    if handle.startswith("Gazette "):
        return "Gazette", handle[8:]
    if handle.startswith("Quake "):
        return "Earthquakes", ""
    if handle in MEA_LISTS:
        return "MEA", MEA_LISTS[handle]
    if handle.startswith("State: "):
        return "States", handle[7:]
    if handle == "RBI":
        return "RBI", "Press releases"
    if handle == "RBI notifications":
        return "RBI", "Notifications"
    if handle == "CCI":
        key = str(post.id)
        for prefix, name in CCI_LISTS.items():
            if key.startswith(prefix):
                return "CCI", name
        return "CCI", ""
    return handle, ""


def _title(post, source: str, sub: str) -> str:
    """The item's own title: the posts carry "Who: what" for the AI; the website shows who apart."""
    text = _clean(post.text)
    for lead in (f"{sub}: ", f"{post.handle}: ", f"{source}: "):
        if lead.strip(": ") and text.startswith(lead):
            text = text[len(lead):]
            break
    if source == "Gazette":
        text = re.sub(r"^Gazette of India \(Extraordinary\), [^:]+: ", "", text)
    return text


def official_item(post, score, headline: str = "", why: str = "") -> dict:
    source, sub = official_source(post)
    text = _title(post, source, sub)
    title, _, rest = text.partition(". ") if source in ("Gazette", "Earthquakes") else (text, "", "")
    return {"id": str(post.id), "grp": "official", "source": source, "sub": sub,
            "title": (title or text)[:500], "body": rest[:1500], "url": post.url or "",
            "score": None if score is None else float(score),
            "headline": _clean(headline)[:300], "why": _clean(why)[:400]}


def official_sources(cfg: dict, pib_ministries=()) -> list:
    """Every (group, source) the official stream reads, from its section of watchlist.yml,
    with PIB's whole list of ministries (from its own menu) when this run has read it."""
    out = set()
    if cfg.get("pib", True):
        out.add(("official", "PIB", tuple(pib_ministries)))
    if cfg.get("gazette", True):
        out.add(("official", "Gazette"))
    if cfg.get("earthquakes") is not False:
        out.add(("official", "Earthquakes"))
    from . import regulators                                          # noqa: PLC0415
    for which in cfg.get("regulators") or []:
        out.add(("official", regulators.NAMES[which]))
    for entry in (cfg.get("feeds") or []) + (cfg.get("pages") or []) + (cfg.get("mea") or []):
        name = entry["name"]
        out.add(("official", "States" if name.startswith("State: ") else
                 "MEA" if name in MEA_LISTS else "RBI" if name.startswith("RBI") else name))
    return sorted(out)


def tech_item(post, score, headline: str = "", why: str = "") -> dict | None:
    """A company or AI-lab newsroom post; None for anything else."""
    handle = post.handle
    if handle.lower().startswith(("news:", "pib ", "reg ", "bse ")):
        return None
    text = _clean(post.text)
    for lead in (f"{handle} published: ", f"{handle}: "):
        if text.startswith(lead):
            text = text[len(lead):]
            break
    # a feed's post is "Title. Summary"; a full stop early on is more likely "Inc." than the end
    title, _, rest = text.partition(". ")
    if len(title) < 25 or not rest:
        title, rest = text, ""
    return {"id": "url:" + (post.url or str(post.id)), "grp": "tech", "source": handle, "sub": "",
            "title": title[:500], "body": rest[:1500], "url": post.url or "",
            "score": None if score is None else float(score),
            "headline": _clean(headline)[:300], "why": _clean(why)[:400]}


def tech_sources(cfg: dict) -> list:
    sources = cfg.get("sources") or {}
    return sorted(("tech", entry["name"]) for entry in (sources.get("rss") or []) + (sources.get("pages") or []))


def watch_source(name: str) -> tuple:
    """"e-Gazette - MHA extraordinary" -> ("e-Gazette", "MHA extraordinary")."""
    if name.startswith("DoPT"):
        return "DoPT", "Orders"
    if name == "CBI FIRs":
        return "CBI", "FIRs"
    source, _, sub = name.partition(" - ")
    return source.strip(), sub.strip()


def watch_item(item) -> dict:
    source, sub = watch_source(item.source)
    body = _clean(item.detail)
    if item.lines:
        body = (body + " " + " | ".join(_clean(line) for line in item.lines[:12])).strip()
    return {"id": "watch:" + str(item.key), "grp": "watch", "source": source, "sub": sub,
            "title": _clean(item.title)[:500], "body": body[:1500], "url": item.url or ""}
