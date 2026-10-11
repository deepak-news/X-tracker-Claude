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
    official   also the website-only pages marked "ai: true" (site_sources.py), each new row
               scored by the AI against "site: newsworthy" before it is handed over
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


def general_on() -> bool:
    """Switch "site: general_scores": each desk's AI also gives the general desk's score
    (judge.GENERAL_ASK), sent beside the desk's own as "general"."""
    return enabled() and bool(_cfg().get("general_scores"))


def _general(post):
    value = getattr(post, "general", None)
    return float(value) if isinstance(value, (int, float)) else None


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
         failed: list | None = None, all_listed: bool = False, dry_run: bool = False,
         status: list | None = None) -> None:
    """One hand-off. origin names the step ("national", "bureau", "tech", "watch", "site").

    sources: every (group, source) the step reads -- or (group, source, its lists or
    ministries) -- so the website can offer a source, or PIB's Ministry of Civil Aviation,
    before it has published anything. all_listed: the items are everything the pages list,
    not only what is new; the website works out what is new (site_sources.py).

    status: how each source's reading went this run, [(group, source, ok, why)] -- the
    website shows people when each source was last read and since when one has been
    failing (status(), below, builds these). A fifth value, minutes between reads, marks a
    source not read every run (the ED pages), so it is not shown as late in between.
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
    if not items and not sources and not failed and not status:
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
                    "failed": (failed or []) if n == 0 else [],
                    "status": [{"grp": st[0], "source": st[1], "ok": bool(st[2]), "why": str(st[3] or "")[:200],
                                "every": int(st[4]) if len(st) > 4 and st[4] else 0}
                               for st in (status or [])] if n == 0 else []}
            answer = requests.post(url, json=body, timeout=TIMEOUT,
                                   headers={"Authorization": auth, "User-Agent": "news-tracker"})
            answer.raise_for_status()
            fresh += int(answer.json().get("fresh", 0))
        print(f"  website: handed over {len(items)} item(s), {fresh} new there")
    except Exception as exc:                                          # noqa: BLE001
        print(f"  ! website hand-off failed ({str(exc)[:160]}); the emails are not affected")


def known(ids: list, sources: list) -> dict | None:
    """Which of these item ids the website already has, and which sources ("official|White
    House") have had their first list taken in. None when it cannot be asked: the caller then
    judges nothing this run (site_sources.py, the pages its AI screens)."""
    if not enabled():
        return None
    try:
        auth = _auth()
        if not auth:
            return None
        url = (_cfg().get("url") or DEFAULT_URL).rstrip("/") + "/api/known"
        answer = requests.post(url, json={"ids": ids, "sources": sources}, timeout=TIMEOUT,
                               headers={"Authorization": auth, "User-Agent": "news-tracker"})
        answer.raise_for_status()
        data = answer.json()
        return {"known": set(data.get("known") or []), "started": set(data.get("started") or [])}
    except Exception as exc:                                          # noqa: BLE001
        print(f"  ! could not ask the website what it already has ({str(exc)[:160]})")
        return None


def unscored(hours: int = 6, limit: int = 90) -> list:
    """Recent items on the website (not a first list's backlog) that have no general desk's
    score yet: the watch pages, rows from the Mac, anything a desk's AI could not get to.
    [] when it cannot be asked. site_sources.py scores them (switch "site: general_fill")."""
    if not enabled():
        return []
    try:
        auth = _auth()
        if not auth:
            return []
        url = (_cfg().get("url") or DEFAULT_URL).rstrip("/") + "/api/unscored"
        answer = requests.post(url, json={"hours": hours, "limit": limit}, timeout=TIMEOUT,
                               headers={"Authorization": auth, "User-Agent": "news-tracker"})
        answer.raise_for_status()
        return answer.json().get("items") or []
    except Exception as exc:                                          # noqa: BLE001
        print(f"  ! could not ask the website what still needs a score ({str(exc)[:160]})")
        return []


def status(group: str, results, name_of) -> list:
    """[(group, source, ok, why)] from one step's [(reader name, error or None)]; name_of turns a
    reader's name into the source people see ("PIB, all ministries" -> "PIB"). Two readers of
    one source (RBI's press releases and notifications) report as one: failing if either did."""
    out: dict = {}
    for name, exc in results:
        source = name_of(name)
        if not source:
            continue
        ok, why = out.get(source, (True, ""))
        if exc is not None:
            ok, why = False, "; ".join(x for x in (why, f"{name}: {str(exc)[:120]}") if x)
        out[source] = (ok, why)
    return [(group, source, ok, why) for source, (ok, why) in out.items()]


def official_name(name: str) -> str:
    """A national-desk reader's name -> its source on the website."""
    if name.startswith("PIB"):
        return "PIB"
    if name.startswith("Gazette"):
        return "Gazette"
    if name.startswith("Earthquakes"):
        return "Earthquakes"
    if name.startswith("State: "):
        return "States"
    if name in MEA_LISTS:
        return "MEA"
    if name.startswith("RBI"):
        return "RBI"
    return name


# ---------------------------------------------------------------- what each item is

def _clean(text) -> str:
    return " ".join(str(text or "").split())


# The ministries and bodies whose extraordinary gazettes people choose from on the website, in
# the e-Gazette site's own spelling. The site's own menu is no help: 607 names, most of them
# long gone ("Ministry of Works") or a single rural bank. Anyone not listed here still appears
# in the list the first time they publish a gazette.
GAZETTE_LIST = (
    "Ministry of Agriculture and Farmers Welfare", "Ministry of Ayush",
    "Ministry of Chemicals and Fertilizers", "Ministry of Civil Aviation", "Ministry of Coal",
    "Ministry of Commerce and Industry", "Ministry of Communications",
    "Ministry of Consumer Affairs, Food and Public Distribution", "Ministry of Cooperation",
    "Ministry of Corporate Affairs", "Ministry of Culture", "Ministry of Defence",
    "Ministry of Development of North Eastern Region", "Ministry of Earth Sciences",
    "Ministry of Education", "Ministry of Electronics and Information Technology",
    "Ministry of Environment, Forest and Climate Change", "Ministry of External Affairs",
    "Ministry of Finance", "Ministry of Fisheries, Animal Husbandry and Dairying",
    "Ministry of Food Processing Industries", "Ministry of Health and Family Welfare",
    "Ministry of Heavy Industries", "Ministry of Home Affairs",
    "Ministry of Housing and Urban Affairs", "Ministry of Information and Broadcasting",
    "Ministry of Jal Shakti", "Ministry of Labour and Employment", "Ministry of Law and Justice",
    "Ministry of Micro, Small and Medium Enterprises", "Ministry of Mines",
    "Ministry of Minority Affairs", "Ministry of New and Renewable Energy",
    "Ministry of Panchayati Raj", "Ministry of Parliamentary Affairs",
    "Ministry of Personnel, Public Grievances and Pensions", "Ministry of Petroleum and Natural Gas",
    "Ministry of Ports, Shipping and Waterways", "Ministry of Power", "Ministry of Railways",
    "Ministry of Road Transport and Highways", "Ministry of Rural Development",
    "Ministry of Science and Technology", "Ministry of Skill Development and Entrepreneurship",
    "Ministry of Social Justice and Empowerment",
    "Ministry of Statistics and Programme Implementation", "Ministry of Steel",
    "Ministry of Textiles", "Ministry of Tourism", "Ministry of Tribal Affairs",
    "Ministry of Women and Child Development", "Ministry of Youth Affairs and Sports",
    "Department of Space",
    "Cabinet Secretariat", "Prime Ministers Office", "Presidents Secretariat",
    "Lok Sabha Secretariat", "Rajya Sabha Secretariat",
    "National Institution for Transforming India (NITI Aayog)", "Election Commission of India",
    "Comptroller and Auditor General of India", "Union Public Service Commission",
    "Central Information Commission", "Central Vigilance Commission", "Lokpal of India",
    "National Human Rights Commission", "Government of The National Capital Territory of Delhi",
    "Reserve Bank of India", "Securities and Exchange Board of India",
    "Insurance Regulatory and Development Authority", "Pension Fund Regulatory and Development Authority",
    "Insolvency and Bankruptcy Board of India", "International Financial Services Centres Authority",
    "The Competition Commission of India", "Telecom Regulatory Authority of India",
    "Central Electricity Regulatory Commission", "Petroleum and Natural Gas Regulatory Board",
    "Airports Economic Regulatory Authority of India", "Airports Authority of India",
    "Office of the Directorate General of Civil Aviation", "Atomic Energy Regulatory Board",
    "Food Safety and Standards Authority of India", "Bureau of Indian Standards",
    "Unique Identification Authority of India", "National Highways Authority of India",
    "Inland Waterways Authority of India", "Tariff Authority for Major Ports",
    "Warehousing Development and Regulatory Authority", "National Medical Commission",
    "University Grants Commission", "Institute of Chartered Accountants of India",
    "Institute of Company Secretaries of India", "Institute of Cost Accountants of India",
)
# The same issuer spelt two ways on the e-Gazette site
GAZETTE_ALIASES = {"Ministry of Jalshakti": "Ministry of Jal Shakti",
                   "Insurance Regulatory And Development Authority": "Insurance Regulatory and Development Authority"}

# Names still on PIB's menu that no longer issue anything (merged or renamed years ago), and a
# stray "home". One comes back by itself if a release ever carries it.
PIB_RETIRED = {
    "home", "Department of Ocean Development", "Ministry of Agro & Rural Industries",
    "Ministry of Company Affairs", "Ministry of Disinvestment", "Ministry of Drinking Water & Sanitation",
    "Ministry of Overseas Indian Affairs", "Ministry of Surface Transport", "Ministry of Urban Development",
    "Ministry of Water Resources, River Development and Ganga Rejuvenation",
}

_SMALL = {"of", "the", "and", "for", "in", "on", "to"}


def gazette_issuer(name: str) -> str:
    """One spelling per issuer: spacing tidied, a SHOUTED or all-lowercase name in title case."""
    name = _clean(name)
    if name.isupper() or name.islower():
        name = " ".join(w if i and w in _SMALL else w[:1].upper() + w[1:]
                        for i, w in enumerate(name.lower().split()))
    return GAZETTE_ALIASES.get(name, name)


MEA_LISTS = {"MEA press releases": "Press releases", "MEA statements": "Statements"}
CCI_LISTS = {"cci-order": "Antitrust orders", "cci-merger": "Mergers",
             "cci-pr:media-gallery": "Press releases", "cci-pr:antitrust": "Antitrust press releases"}


def official_source(post) -> tuple:
    """(source, sub) for an item of the official stream, as people choose it on the website."""
    handle = post.handle
    if handle.startswith("PIB "):
        return "PIB", handle[4:]
    if handle.startswith("Gazette "):
        return "Gazette", gazette_issuer(handle[8:])
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
    for lead in (f"{sub}: ", f"{post.handle}: ", f"{source}: ", f"{post.handle} published: ", f"{source} published: "):
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
            "score": None if score is None else float(score), "general": _general(post),
            "headline": _clean(headline)[:300], "why": _clean(why)[:400]}


def official_sources(cfg: dict, pib_ministries=()) -> list:
    """Every (group, source) the official stream reads, from its section of watchlist.yml,
    with PIB's list of ministries (from its own menu, when this run has read it) and the
    gazettes' list. A list sent here replaces the one on the website."""
    out = set()
    if cfg.get("pib", True):
        out.add(("official", "PIB", tuple(m for m in pib_ministries if m not in PIB_RETIRED)))
    if cfg.get("gazette", True):
        out.add(("official", "Gazette", GAZETTE_LIST))
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
            "score": None if score is None else float(score), "general": _general(post),
            "headline": _clean(headline)[:300], "why": _clean(why)[:400]}


def regulator_item(post, score, headline: str = "", why: str = "") -> dict | None:
    """The Tech Desk's score for a CCI, CERT-In or DGFT document, as the official stream's item
    (the national desk sends the same document under the same id), so the website shows the
    Tech Desk's score beside the others. Switch: "site: tech_regulator_scores"."""
    key = getattr(post, "doc_key", "")
    if not key or not post.handle.startswith("REG ") or _cfg().get("tech_regulator_scores", True) is False:
        return None
    from types import SimpleNamespace                                  # noqa: PLC0415
    doc = SimpleNamespace(id=key, handle=post.handle[4:], text=post.text, url=post.url,
                          general=getattr(post, "general", None))
    return official_item(doc, score, headline, why)


def tech_sources(cfg: dict) -> list:
    sources = cfg.get("sources") or {}
    return sorted(("tech", entry["name"]) for entry in (sources.get("rss") or []) + (sources.get("pages") or []))


def _gazette_watch(name: str = "", key: str = "") -> bool:
    """The e-Gazette watches are mail lists -- whose gazettes go to whom -- not sources. The
    website offers every gazette, ministry by ministry, as the official stream's "Gazette".
    Switch: "site: watch_gazette: true" sends them as well."""
    return (name.startswith("e-Gazette") or str(key).startswith("gazette:")) and not _cfg().get("watch_gazette")


# Parliament's lists, so people can tick only some (switch "site: parliament_lists"; added
# 2026-10-11): the part of a row's key before ":" (sansad.py) -> the list it shows under.
PARLIAMENT_LISTS = {"bill": "Bills", "meeting": "Committee meetings", "report": "Committee reports",
                    "pr": "Press releases", "notice": "Rajya Sabha notices", "papers": "Papers laid",
                    "lob": "Agenda, bulletins and synopsis", "bulletin": "Agenda, bulletins and synopsis",
                    "synopsis": "Agenda, bulletins and synopsis"}


def _parliament_lists() -> bool:
    return _cfg().get("parliament_lists", True) is not False


def watch_sources(names) -> list:
    out = []
    for name in names:
        if _gazette_watch(name=name):
            continue
        source, sub = watch_source(name)
        out.append(("watch", source, sorted(set(PARLIAMENT_LISTS.values())))
                   if source == "Parliament" and _parliament_lists() else ("watch", source, sub))
    return out


def watch_items(items) -> list:
    return [watch_item(item) for item in items if not _gazette_watch(key=item.key)]


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
    if source == "Parliament" and _parliament_lists():
        sub = PARLIAMENT_LISTS.get(str(item.key).split(":")[0], "")
    body = _clean(item.detail)
    if item.lines:
        body = (body + " " + " | ".join(_clean(line) for line in item.lines[:12])).strip()
    return {"id": "watch:" + str(item.key), "grp": "watch", "source": source, "sub": sub,
            "title": _clean(item.title)[:500], "body": body[:1500], "url": item.url or ""}
