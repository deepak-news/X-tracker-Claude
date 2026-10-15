"""Pages read only for the website (PTI Source Alerts), never mailed. Added 2026-10-11 for
the aviation and corporate-affairs reporter.

    dgca_comments   DGCA: draft rules out for public comment ("Comments invited")
    dgca_traffic    DGCA: monthly domestic air traffic, city pair by city pair
    dgca_schedules  DGCA: airlines' approved domestic schedules (summer and winter)
    dgca_circulars  DGCA: circulars (general; surveillance and enforcement)
    dgca_notices    DGCA: public notices (AI-scored: "ai_pages" in watchlist.yml)
    aaib            AAIB: What's new -- accident and incident investigation reports
    mca_whats_new   MCA: What's new
    mca_updates     MCA: Important updates
    cci_whats_new   CCI: What's new (its antitrust orders and press releases are already
                    in the official stream, read by regulators.py)
    ibbi            IBBI: What's new -- resolution plans, orders, notices
    who_outbreaks   WHO: Disease Outbreak News (the page fills itself from WHO's own data)

More pages and feeds, added 2026-10-11, need no code of their own: each is one line under
"site: more:" in watchlist.yml, read by read_more() below --

    {source: GST Council, url: "https://gstcouncil.gov.in/", match: "/sites/default/files/"}
        every link on the page whose address matches "match" (a regular expression) is a row;
        "only" keeps rows whose title matches, "exclude" drops them, "strip" cuts a matching
        part out of the title (BCCI's "Press Releases" label); "within" (a CSS selector)
        reads only part of the page; "min" is the shortest title kept (default 20)
    {source: White House, feed: "https://www.whitehouse.gov/news/feed/", ai: true}
        an RSS or Atom feed; "only" keeps entries whose title matches
    relay: true      read through the mea-relay Worker on Cloudflare (sites refusing GitHub)
    home_only: true  read by the Mac's copy (run-watch.sh, WATCH_HOME=1), never on GitHub: for
                     sites that refuse GitHub and Cloudflare alike (ECI, Lokpal, NCB...)
    reader: eci      the site gives its list as data, read by a function below (HOME_READERS)
    new_ids: true    with "ai: true", for a source that used to count every row: fresh ids, so
                     its current list goes in quietly under the scored source
    ai: true         a busy page: each NEW row is scored by the AI against "site: newsworthy"
                     and handed over in the official stream with its score, so people's
                     score bar applies (website.known() says which rows are new, so each row
                     is judged once; a page's first list goes in unjudged and quietly)

Every row each page lists goes to the website on every run, and the website keeps track of
what it has already seen: this step needs no memory, and a page's first reading is noted
there quietly instead of arriving as a flood. Rows dated more than MAX_AGE_DAYS ago are
marked quiet too.

Which pages are read: "site: pages:" in watchlist.yml. MCA refuses any client that is not a
browser, so it is read with curl_cffi, which connects the way Chrome does.

    python -m tracker.site_sources            read and hand over
    python -m tracker.site_sources --dry-run  read and print, hand over nothing
"""

import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import sys
from urllib.parse import quote, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

from . import judge, regulators, website

TIMEOUT = 40
MAX_AGE_DAYS = 21
HEADERS = dict(regulators.BROWSER)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _clean(text) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(text or ""))).split())


def _get(url: str, **kw):
    response = requests.get(url, **{"headers": HEADERS, "timeout": TIMEOUT, **kw})
    response.raise_for_status()
    # a page that names no character set is read as Latin-1, which garbles "Air India's"
    if (response.encoding or "").lower() in ("", "iso-8859-1"):
        response.encoding = "utf-8"
    return response


def _day(text: str, *formats):
    for fmt in formats:
        try:
            return dt.datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- DGCA

DGCA_SCAN = "https://www.dgca.gov.in/digigov-portal/scan?"
DGCA_PAGE = "https://www.dgca.gov.in/digigov-portal/?baseLocale=en_US?page=4359/4235/servicename"
DGCA_FILES = "https://public-prd-dgca.s3.ap-south-1.amazonaws.com/"   # where the site's own links point


DGCA_KEEP_DAYS = 45      # the page keeps every draft since 2010; only these are still current


def dgca_comments() -> list:
    """The "Comments invited" page fills itself from this request; each draft is a table row.

    Only drafts whose comment deadline is still to come, or passed in the last DGCA_KEEP_DAYS,
    are kept: the page also carries the whole archive, some of it twice."""
    response = requests.post(DGCA_SCAN, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"}, timeout=TIMEOUT, data={
        "baseLocale": "", "screenId": "10000001", "classification": "", "actionVal": "viewStaticData",
        "requestType": "ApplicationRH", "attachId": "", "langType": "1", "ruleBookId": "4359",
        "contentId": "4235", "serviceName": "fetchRulebookContentDtlsList", "attr": ""})
    response.raise_for_status()
    data = response.json()
    content = data.get("contentText") or ((data.get("ruleBookContentDtlsList") or [{}])[0] or {}).get("contentText") or ""
    rows, keys = [], set()
    since = dt.datetime.now(IST).date() - dt.timedelta(days=DGCA_KEEP_DAYS)
    for tr in BeautifulSoup(content, "html.parser").find_all("tr"):
        link = tr.find("a", attrs={"data-url": True})
        if not link:
            continue
        title = _clean(link.get_text(" "))
        cells = tr.find_all("td")
        by = _day(_clean(cells[1].get_text(" ")) if len(cells) > 1 else "", "%d %B %Y", "%d %b %Y", "%d.%m.%Y", "%d/%m/%Y")
        path = link["data-url"].strip()
        key = f"dgca-comments:{path}|{title[:60]}"
        if not by or by < since or key in keys:
            continue
        keys.add(key)
        rows.append({"key": key, "title": title, "detail": f"Comments invited until {by:%b} {by.day}, {by.year}",
                     "url": DGCA_FILES + quote(path.replace("jsp/dgca/", "", 1)), "date": None})
    if not content:
        raise RuntimeError("DGCA's Comments invited page came back empty (it may have changed)")
    return rows


# DGCA's other lists (added 2026-10-11, the user's choice): monthly air traffic, airlines'
# approved schedules, circulars, and public notices (the last scored by the AI). The site
# fills each list from one request to "scan"; documents sit on S3 or behind "Upload".
DGCA_ATTACH = "https://www.dgca.gov.in/digigov-portal/Upload?flag=iframeAttachView&attachId="
DGCA_NOTICE_DAYS = 60     # the public notices go back years; older ones are no news


def _dgca(**form) -> str:
    data = {"baseLocale": "", "screenId": "10000001", "classification": "", "requestType": "ApplicationRH",
            "attachId": "", "langType": "1", "serviceName": "", "attr": "", **form}
    url = DGCA_SCAN + ("requestType=ApplicationRH&actionVal=viewAllService" if form.get("actionVal") == "viewAllService" else "")
    response = requests.post(url, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"}, timeout=TIMEOUT, data=data)
    response.raise_for_status()
    if form.get("serviceName"):
        data = response.json()
        return data.get("contentText") or ((data.get("ruleBookContentDtlsList") or [{}])[0] or {}).get("contentText") or ""
    return response.text


def _dgca_json(**form) -> dict:
    data = {"baseLocale": "", "screenId": "10000001", "classification": "", "requestType": "ApplicationRH",
            "attachId": "", "langType": "1", "serviceName": "fetchRulebookContentDtlsList", "attr": "",
            "actionVal": "viewStaticData", **form}
    response = requests.post(DGCA_SCAN, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"}, timeout=TIMEOUT, data=data)
    response.raise_for_status()
    return response.json()


def _dgca_static(book: str, content: str) -> BeautifulSoup:
    html_ = ""
    for _ in range(3):             # DGCA's servers sometimes answer with a stale, empty copy
        html_ = _dgca(actionVal="viewStaticData", ruleBookId=book, contentId=content, serviceName="fetchRulebookContentDtlsList")
        if html_:
            break
    if not html_:
        raise RuntimeError(f"DGCA's list {book}/{content} came back empty (it may have changed)")
    return BeautifulSoup(html_, "html.parser")


def _dgca_file(path: str) -> str:
    return DGCA_FILES + quote(path.strip().replace("jsp/dgca/", "", 1))


# DGCA's "Domestic City Pair Traffic" table for each year has a number of its own (2026: 9645).
# Its "Monthly statistics" page (259/4184) is unreliable to read whole, so the table is read
# directly, the way the site's own page asks for it ("getParentData"). A newer year's table is
# looked for among the numbers that page links to, above the newest known.
DGCA_CITY_PAIR = 9645


def _dgca_part(content) -> list:
    for _ in range(3):
        try:
            entries = _dgca_json(ruleBookId="259", contentId=str(content), serviceName="getParentData").get("ruleBookContentDtlsList") or []
        except ValueError:
            entries = []
        if entries:
            return entries
    return []


def dgca_traffic() -> list:
    """Data and reports > Monthly statistics: the year's "Domestic City Pair Traffic" table,
    one PDF a month, the newest about three weeks after the month ends. (The "Air traffic" list,
    4264/4206, stopped at July 2023; moved here 2026-10-12.)"""
    tables = [DGCA_CITY_PAIR]
    try:
        main = " ".join(e.get("contentText") or "" for e in _dgca_part(4184))
        tables += sorted({int(n) for n in re.findall(r"/259/(\d+)/html", main) if int(n) > DGCA_CITY_PAIR})[:4]
    except Exception:                                                 # noqa: BLE001
        pass
    rows = []
    for content in tables:
        for entry in _dgca_part(content):
            name = _clean(entry.get("contentName") or entry.get("contentIdentifier"))
            year = re.search(r"(20\d\d)", name)
            if "city pair" not in name.lower() or not year:
                continue
            soup = BeautifulSoup(entry.get("contentText") or "", "html.parser")
            for tr in soup.find_all("tr"):
                cells = tr.find_all("td")
                link = next((a for a in tr.find_all("a", attrs={"data-url": True}) if a["data-url"].lower().endswith(".pdf")), None)
                if not cells or not link:
                    continue
                month = _clean(cells[0].get_text(" ")).title()
                # only recent months: DGCA linking its 2020-2024 tables overnight (2026-10-15)
                # sent 60 old months as news. A month's figures come out about three weeks after it
                try:
                    start = dt.datetime.strptime(f"{month[:3]} {year.group(1)}", "%b %Y").date()
                except ValueError:
                    continue
                if (dt.date.today() - start).days > 120:
                    continue
                rows.append({"key": f"dgca-traffic:{link['data-url'].strip()}",
                             "title": f"Domestic air traffic by city pair: {month} {year.group(1)}",
                             "detail": "DGCA's monthly passenger figures for each domestic route", "url": _dgca_file(link["data-url"]), "date": None})
    if not rows:
        raise RuntimeError(f"DGCA's city pair table ({DGCA_CITY_PAIR}) came back empty (it may have changed)")
    return rows


def dgca_schedules() -> list:
    """Each airline's approved domestic schedule (summer "SS" or winter "WS"), one PDF each."""
    rows = []
    for link in _dgca_static("4201", "4200").find_all("a", attrs={"data-url": True}):
        path = link["data-url"].strip()
        if not path.lower().endswith(".pdf"):
            continue
        season = "winter" if re.search(r"_WS_", path) else "summer" if re.search(r"_SS_", path) else ""
        year = (re.findall(r"20\d\d", path) or [""])[-1]
        when = f", {season} schedule {year}".rstrip() if season else f", {year}" if year else ""
        rows.append({"key": f"dgca-schedule:{path}", "title": f"{_clean(link.get_text(' '))}: approved domestic flight schedule{when}",
                     "detail": "", "url": _dgca_file(path), "date": None})
    if not rows:
        raise RuntimeError("no airlines on DGCA's domestic schedule list (it may have changed)")
    return rows


def _dgca_attachments(soup) -> list:
    out = []
    for link in soup.find_all("a", attrs={"data-url": True}):
        ref = link["data-url"].strip()
        if "dynamicPdf/" in ref:
            out.append((link, ref.split("dynamicPdf/", 1)[1].split("&main")[0]))
    return out


def dgca_circulars() -> list:
    """Circulars: the general ones and the surveillance and enforcement ones."""
    rows = []
    for content, attr in (("3328", "circularsRulesGen"), ("3319", "circularsRulesEnforcement")):
        soup = BeautifulSoup(_dgca(actionVal="viewDynamicRuleContLvl2", ruleBookId="7", contentId=content, attr=attr), "html.parser")
        for link, attach in _dgca_attachments(soup):
            title = _clean(link.get_text(" "))
            if title:
                rows.append({"key": f"dgca-circular:{attach}", "title": title, "detail": "", "url": DGCA_ATTACH + attach, "date": None})
    if not rows:
        raise RuntimeError("no circulars on DGCA's lists (they may have changed)")
    return rows


def dgca_notices() -> list:
    """Public notices, with their dates; the last DGCA_NOTICE_DAYS only."""
    soup = BeautifulSoup(_dgca(actionVal="viewAllService", classification="publicNotices"), "html.parser")
    since = dt.datetime.now(IST).date() - dt.timedelta(days=DGCA_NOTICE_DAYS)
    rows, listed = [], 0
    for tr in soup.find_all("tr"):
        found = _dgca_attachments(tr)
        if not found:
            continue
        listed += 1
        link, attach = found[0]
        texts = [_clean(td.get_text(" ")) for td in tr.find_all("td")]
        day = next((d for d in (_day(t, "%d/%m/%Y") for t in texts if t) if d), None)
        title = _clean(link.get_text(" ")) or max(texts, key=len, default="")
        if title and (day is None or day >= since):
            rows.append({"key": f"dgca-notice:{attach}", "title": title, "detail": "", "url": DGCA_ATTACH + attach, "date": day})
    if not listed:
        raise RuntimeError("no public notices on DGCA's list (it may have changed)")
    return rows


# ---------------------------------------------------------------- AAIB

AAIB = "https://aaib.gov.in/"


def aaib() -> list:
    soup = BeautifulSoup(_get(AAIB + "whatsnew.html").text, "html.parser")
    rows = []
    for link in soup.select('a[href*="New Assets/"]'):
        href = link["href"].strip()
        title = _clean(link.get_text(" "))
        if title:
            rows.append({"key": f"aaib:{href}", "title": title, "detail": "",
                         "url": urljoin(AAIB, quote(href, safe="/:")), "date": None})
    if not rows:
        raise RuntimeError("nothing on AAIB's What's new (the page may have changed)")
    return rows


# ---------------------------------------------------------------- MCA

MCA = "https://www.mca.gov.in"
MCA_NEW = MCA + "/content/mca/global/en/notifications-tender/whats-new.html"
MCA_UPDATES = MCA + "/content/mca/global/en/notifications-tender/news-updates/updates.html"


def _mca():
    from curl_cffi import requests as browserlike                      # noqa: PLC0415
    return browserlike.Session(impersonate="chrome")


def mca_whats_new() -> list:
    session = _mca()
    page = session.get(MCA_NEW, timeout=TIMEOUT)
    page.raise_for_status()
    found = re.search(r'data-dialog="([^"]+)"', page.text)
    if not found:
        raise RuntimeError("MCA What's new: the list's settings were not on the page")
    dialog = json.dumps(json.loads(html.unescape(found.group(1))), separators=(",", ":"))
    answer = session.get(f"{MCA}/bin/dms/searchDocList", timeout=TIMEOUT, params={
        "page": 1, "perPage": 20, "sortField": "Date", "sortOrder": "D", "searchField": "Title",
        "searchKeyword": "", "startDate": "", "endDate": "", "filter": "", "dialog": dialog},
        headers={"Referer": MCA_NEW, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    answer.raise_for_status()
    docs = json.loads(answer.json().get("documentDetails") or "[]")
    rows = []
    for doc in docs:
        title = _clean(doc.get("column1"))
        if not title:
            continue
        rows.append({"key": f"mca-new:{doc.get('docID')}", "title": title, "detail": "",
                     "url": f"{MCA}/bin/dms/getdocument?mds={doc.get('docID')}&type=open",
                     "date": _day(str(doc.get("column2") or ""), "%d-%m-%Y")})
    if not rows:
        raise RuntimeError("nothing in MCA's What's new (the page may have changed)")
    return rows


def mca_updates() -> list:
    """Important updates: the page reads a spreadsheet, named on the page itself."""
    session = _mca()
    page = session.get(MCA_UPDATES, timeout=TIMEOUT)
    page.raise_for_status()
    found = re.search(r'data-path="([^"]+\.csv)"', page.text)
    if not found:
        raise RuntimeError("MCA Important updates: the spreadsheet was not named on the page")
    sheet = session.get(MCA + found.group(1), timeout=TIMEOUT, headers={"Referer": MCA_UPDATES})
    sheet.raise_for_status()
    rows = []
    for line in csv.DictReader(io.StringIO(sheet.text)):
        text = _clean(line.get("Title"))
        if not text:
            continue
        day = (line.get("Date") or "").strip()
        title, detail = (text, "") if len(text) <= 300 else (text[:300].rsplit(" ", 1)[0] + "…", text[:1200])
        link = (line.get("Link") or "").strip()
        rows.append({"key": "mca-update:" + hashlib.sha1(f"{text}|{day}".encode()).hexdigest()[:16],
                     "title": title, "detail": detail, "url": urljoin(MCA, link) if link else MCA_UPDATES,
                     "date": _day(day, "%d-%m-%Y")})
    if not rows:
        raise RuntimeError("nothing in MCA's Important updates (the spreadsheet may have changed)")
    return rows


# ---------------------------------------------------------------- CCI and IBBI

def cci_whats_new() -> list:
    session = requests.Session()
    session.headers.update(regulators.BROWSER)
    rows = []
    for row in regulators._cci_table(session, "whats-new", ["DT_RowIndex", "title", "file_name"]):
        link = re.search(r'href="([^"]+)"', str(row.get("file_name") or ""))
        rows.append({"key": f"cci-whatsnew:{row.get('id')}", "title": _clean(row.get("title")), "detail": "",
                     "url": html.unescape(link.group(1)) if link else regulators.CCI + "whats-new",
                     "date": _day(_clean(row.get("new_date")), "%d/%m/%Y")})
    return rows


IBBI = "https://ibbi.gov.in/"
_SIZE = re.compile(r"\s*PDF\s*\([\d.]+\s*[KMG]B\)\s*$", re.I)


def ibbi() -> list:
    soup = BeautifulSoup(_get(IBBI + "whats-new").text, "html.parser")
    table = soup.find("table")
    if not table:
        raise RuntimeError("no table on IBBI's What's new (the page may have changed)")
    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        title = _SIZE.sub("", _clean(cells[2].get_text(" ")))
        link = tr.find("a", href=True)
        day = _clean(cells[1].get_text(" "))
        if title:
            rows.append({"key": "ibbi:" + (link["href"] if link else f"{title[:80]}|{day}"), "title": title, "detail": "",
                         "url": urljoin(IBBI, link["href"]) if link else IBBI + "whats-new", "date": _day(day, "%Y-%m-%d")})
    return rows


# ---------------------------------------------------------------- the pages and feeds in "site: more:"

RELAY = "https://mea-relay.ptiinfographics.workers.dev/page"
MORE_ROWS = 60          # the newest this many entries of each feed
PAGE_LINKS = 150        # the first this many matching links on each page (menus come first)
AI_PER_RUN = 90         # rows the AI may judge in one run (six requests); the rest wait a run


def _text(entry) -> bytes:
    url = entry.get("url") or entry.get("feed")
    if entry.get("relay"):
        # the relay waits on a slow site itself (the State Department: 10 seconds and more)
        response = requests.get(RELAY, params={"u": url}, headers=HEADERS, timeout=70)
        response.raise_for_status()
        return response.content
    return _get(url).content


def _when(parsed):
    if not parsed:
        return None
    try:
        return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc).astimezone(IST).date()
    except (TypeError, ValueError):
        return None


def _feed_rows(entry) -> list:
    parsed = feedparser.parse(_text(entry))
    only = re.compile(entry["only"], re.I) if entry.get("only") else None
    rows = []
    for item in parsed.entries[:MORE_ROWS]:
        title, link = _clean(item.get("title")), (item.get("link") or "").strip()
        if not title or not link or (only and not only.search(title)):
            continue
        detail = _clean(item.get("summary"))
        rows.append({"key": link, "title": title, "detail": "" if detail == title else detail[:1200], "url": link,
                     "date": _when(item.get("published_parsed") or item.get("updated_parsed"))})
    if not parsed.entries:
        raise RuntimeError("the feed came back empty (it may have moved)")
    return rows


def _slug_title(href: str) -> str:
    """PFRDA cuts its titles short on the page ("Clarification regarding applicability of
    incentive..."); the address carries the whole of it."""
    slug = href.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    words = re.sub(r"[-_]+", " ", re.sub(r"\.\w{2,4}$", "", slug)).strip()
    return words[:1].upper() + words[1:]


def _page_rows(entry) -> list:
    base = entry["url"]
    soup = BeautifulSoup(_text(entry), "html.parser")
    scope = soup.select_one(entry["within"]) if entry.get("within") else soup
    if scope is None:
        raise RuntimeError(f"no {entry['within']} on the page (it may have changed)")
    match = re.compile(entry["match"]) if entry.get("match") else None
    exclude = re.compile(entry["exclude"], re.I) if entry.get("exclude") else None
    only = re.compile(entry["only"], re.I) if entry.get("only") else None
    strip = re.compile(entry["strip"], re.I) if entry.get("strip") else None
    shortest = int(entry.get("min", 20))
    rows, seen = [], set()
    for link in scope.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urljoin(base, href.replace(" ", "%20"))
        title = _clean(link.get("title") if entry.get("title") == "attr" else link.get_text(" "))
        if entry.get("title") == "slug" or (title.endswith(("...", "…")) and entry.get("title") != "text"):
            title = _slug_title(url) or title
        if len(title) < shortest or url in seen:
            continue
        if (match and not match.search(url)) or (exclude and exclude.search(title)) or (only and not only.search(title)):
            continue
        if strip:
            title = strip.sub("", title).strip()
        seen.add(url)
        rows.append({"key": url, "title": title, "detail": "", "url": url, "date": None})
        if len(rows) >= PAGE_LINKS:
            break
    if not rows:
        raise RuntimeError("no rows on the page (it may have changed)")
    return rows


def read_more(entry) -> list:
    if entry.get("reader"):
        return HOME_READERS[entry["reader"]]()
    return _feed_rows(entry) if entry.get("feed") else _page_rows(entry)


def _more_label(entry) -> str:
    return f"{entry['source']} {entry.get('sub') or ''}".strip()


def _screen(items: list, dry_run: bool) -> list:
    """The "ai: true" rows: only those the website does not have yet are judged, and only for
    a source whose first list is already in. A row the AI could not get to is held back, to
    be judged next run; one it skipped (a duplicate) goes in unscored, so it is not asked again."""
    if not items:
        return []
    keys = sorted({f"official|{it['source']}" for it in items})
    if dry_run:
        print(f"  (dry run) {len(items)} row(s) from {len(keys)} AI-screened source(s); nothing judged")
        return []
    have = website.known([it["id"] for it in items], keys)
    if have is None:
        return []
    new = [it for it in items if it["id"] not in have["known"]]
    first = [it for it in new if f"official|{it['source']}" not in have["started"]]
    waiting = [it for it in new if f"official|{it['source']}" in have["started"]]
    for it in first:
        it["quiet"] = True
    hourly = website._cfg().get("ai_hourly", True)
    if waiting and hourly and dt.datetime.now(IST).minute >= 15 and len(waiting) < judge.CHUNK:
        print(f"  {len(waiting)} new row(s) wait for the AI's hourly look")
        waiting = []
    if not waiting:
        return first
    batch, later = waiting[:AI_PER_RUN], len(waiting) - AI_PER_RUN
    print(f"  the AI is judging {len(batch)} new row(s)" + (f"; {later} wait for the next run" if later > 0 else ""))
    from .main import dead_models, load_state                        # noqa: PLC0415
    from .sources import Post                                         # noqa: PLC0415
    posts = [Post(id=it["id"], handle=it["source"], text=f"{it['source']}: {it['title']}. {it['body']}".strip(),
                  url=it["url"], created_at="", likes=0, reposts=0, is_reply=False, is_repost=False) for it in batch]
    rubric = website._cfg().get("newsworthy") or ""
    try:
        judged, unscreened, _ = judge.score(posts, rubric, dead_today=dead_models(load_state()),
                                            general=website.general_on())
    except Exception as exc:                                          # noqa: BLE001
        print(f"  ! the AI could not judge them ({str(exc)[:160]}); they wait for the next run")
        return first
    held = {p.id for p in unscreened}
    scored = {p.id: (score, headline, why, getattr(p, "general", None)) for p, score, headline, why in judged}
    out = list(first)
    for it in batch:
        if it["id"] in held:
            continue
        score, headline, why, general = scored.get(it["id"], (None, "", "", None))
        out.append({**it, "score": None if score is None else float(score), "general": general,
                    "headline": _clean(headline)[:300], "why": _clean(why)[:400]})
    return out


FILL_PER_RUN = 60       # items the general desk's AI may score in one run (four requests)


def fill_general(dry_run: bool) -> None:
    """Switch "site: general_fill": items on the website with no general desk's score yet
    (the watch pages have no AI of their own; rows from the Mac; anything a desk's AI missed)
    are scored for the general desk, so a person's one bar applies alike to every source.
    GitHub only, after every other step has handed over, so this run's rows are in."""
    if dry_run or not website.general_on() or not website._cfg().get("general_fill"):
        return
    waiting = website.unscored(limit=FILL_PER_RUN)
    if not waiting:
        return
    print(f"  the AI is scoring {len(waiting)} item(s) for the general desk")
    from .main import dead_models, load_state, record_dead_models, save_state  # noqa: PLC0415
    state = load_state()
    dead = dead_models(state)
    out = []
    for n in range(0, len(waiting), judge.CHUNK):
        batch = waiting[n:n + judge.CHUNK]
        listing = "\n\n".join(f"[{i}] {it.get('source', '')}{' - ' + it['sub'] if it.get('sub') else ''}: "
                               f"{it.get('title', '')}. {it.get('body', '')}"[:judge.MAX_POST_CHARS]
                               for i, it in enumerate(batch))
        try:
            entries, newly_dead = judge.ask_json(judge.GENERAL_PROMPT.format(items=listing), dead)
        except judge.AllModelsExhausted as exc:
            record_dead_models(state, exc.newly_dead)
            print(f"  ! no AI model is available ({str(exc)[:120]}); they wait for the next run")
            break
        except Exception as exc:                                      # noqa: BLE001
            print(f"  ! the AI could not score them ({str(exc)[:160]}); they wait for the next run")
            continue
        record_dead_models(state, newly_dead)
        dead |= newly_dead
        for entry in entries:
            try:
                idx, value = int(entry.get("i", -1)), float(entry.get("general"))
            except (TypeError, ValueError, AttributeError):
                continue
            if not (0 <= idx < len(batch) and 0 <= value <= 10):
                continue
            it = batch[idx]
            out.append({"id": it["id"], "grp": it["grp"], "source": it["source"], "sub": it.get("sub", ""),
                        "title": it["title"], "score": value,
                        "headline": _clean(entry.get("headline"))[:300], "why": _clean(entry.get("why"))[:400]})
    save_state(state)
    if out:
        # the general desk's view is the national desk's (its head of social media)
        website.send("general", out, view="national")


# ---------------------------------------------------------------- sites only the Mac can read

ECI_API = "https://www.eci.gov.in/eci-backend/public/api/"
# ECI's page encrypts each request parameter with a key in its own script ("4WS8851W824R456Y",
# AES-128, ECB, PKCS7, then base64). The two this step sends never change, so they are kept
# already encrypted: "11" (Press Releases) and "20" (how many).
ECI_PRESS = "Y6fPfuS/XeuZJeu7qk0stw=="
ECI_TWENTY = "qr1Y7/+72uof/ZFDh7O8Kw=="


def eci() -> list:
    """Press releases: the list ECI's "Press Releases" tab shows (category 11)."""
    data = _get(ECI_API + "get-event", params={"categories": ECI_PRESS, "limit": ECI_TWENTY}).json()
    rows = []
    for doc in (data.get("results") or {}).get("data") or []:
        files = doc.get("document_attachments") or []
        when = None
        found = re.search(r"(\d{1,2} \w{3} \d{4})", str(doc.get("date_of_creation") or ""))
        if found:
            when = _day(found.group(1), "%d %b %Y")
        title = _clean(doc.get("document_title"))
        if title.upper() == "PRESS NOTE":
            title = "Press note" + (f" of {when:%b} {when.day}, {when.year}" if when else "")
        if title:
            rows.append({"key": f"eci:{doc.get('id')}", "title": title, "detail": "",
                         "url": files[0].get("record_location") if files else "https://www.eci.gov.in/issue-details-page/press-releases",
                         "date": when})
    if not rows:
        raise RuntimeError("nothing in ECI's press releases (the site may have changed)")
    return rows


def ncb() -> list:
    """NCB's press releases, English ones (its site lists the Hindi versions too)."""
    rows = []
    for doc in _get("https://ncb.gov.in/backend/press-releases", headers={**HEADERS, "Accept": "application/json"}).json():
        if (doc.get("langcode") or "English") != "English" or not doc.get("title"):
            continue
        path = doc.get("field_document") or ""
        rows.append({"key": f"ncb:{path or doc['title'][:80]}", "title": _clean(doc["title"]), "detail": "",
                     "url": urljoin("https://ncb.gov.in/", path) if path else "https://ncb.gov.in/media/press-releases",
                     "date": _day(str(doc.get("field_date") or ""), "%Y-%m-%d")})
    if not rows:
        raise RuntimeError("nothing in NCB's press releases (the site may have changed)")
    return rows[:40]


def lokpal() -> list:
    """Lokpal's circulars and notifications."""
    data = _get("https://lokpal.gov.in/api/admin/website/circulars").json()
    rows = []
    for doc in data.get("data") or []:
        pdf = (doc.get("pdf") or [""])[0]
        title = _clean(doc.get("subject"))
        if title:
            rows.append({"key": f"lokpal:{doc.get('id')}", "title": title, "detail": _clean(doc.get("subject_related_to")),
                         "url": f"https://lokpal.gov.in/api/admin/website/pdf?path={quote(pdf, safe='')}" if pdf
                         else "https://lokpal.gov.in/circulars-notifications",
                         "date": _day(str(doc.get("issued_on_date") or ""), "%Y-%m-%d")})
    if not rows:
        raise RuntimeError("nothing in Lokpal's circulars (the site may have changed)")
    return rows


# ---------------------------------------------------------------- Delhi Police

# Delhi Police (delhipolice.gov.in, added 2026-10-12): its lists are ASP.NET grids whose rows
# open only by a form post, so each row is known by its date and title, and links to its list.
# Standing orders (the protest guidelines of Oct 11), circulars, prohibitory orders
# ("Notifications", BNSS 163), the monthly Delhi Police Gazette, the home page's "What's new"
# box, and the district press releases (busy: AI-scored, a source of their own).
DP = "https://delhipolice.gov.in/"


def _dp_grid(page: str, date_at: int | None, title_at: int, formats=("%d/%m/%Y",), more=None) -> list:
    soup = BeautifulSoup(_get(DP + page).text, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        if tr.find("table"):             # the pager: a table of page numbers inside a row
            continue
        cells = [_clean(c.get_text(" ")) for c in tr.find_all("td", recursive=False)]
        if len(cells) <= title_at or not cells[0].rstrip(".").isdigit() or not cells[title_at] or cells[title_at].isdigit():
            continue
        when = _day(cells[date_at], *formats) if date_at is not None else None
        stamp = cells[date_at] if date_at is not None else ""
        rows.append({"key": f"dp-{page.split('.')[0].lower()}:{stamp}|{cells[title_at][:200]}",
                     "title": cells[title_at], "detail": more(cells) if more else (stamp if date_at is not None else ""),
                     "url": DP + page, "date": when})
    if not rows:
        raise RuntimeError(f"nothing in Delhi Police's {page} (the page may have changed)")
    return rows


def dp_standing() -> list:
    return _dp_grid("standingorderNew.aspx", 1, 2, ("%Y-%m-%d",), lambda c: f"Standing order dated {c[1]}")


def dp_circulars() -> list:
    return _dp_grid("circulars.aspx", None, 2, more=lambda c: f"Circular, {c[1]}")


def dp_notifications() -> list:
    return _dp_grid("notification.aspx", 1, 2, more=lambda c: f"Dated {c[1]}")


def dp_gazette() -> list:
    return _dp_grid("Gazette.aspx", 2, 1, more=lambda c: f"Dated {c[2]}")


def dp_press() -> list:
    return _dp_grid("newpressrelease.aspx", 1, 2,
                    more=lambda c: " | ".join(x for x in (c[3] if len(c) > 3 else "", f"PS {c[4]}" if len(c) > 4 and c[4] else "",
                                                          c[5].title() if len(c) > 5 else "") if x))


def dp_whatsnew() -> list:
    """The home page's "What's new" box: documents put up as PDFs (the protest guidelines first)."""
    soup = BeautifulSoup(_get(DP).text, "html.parser")
    rows = []
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        title = _clean(link.get_text(" ")).rstrip(".")
        if "whatsnewfile/" in href.lower() and title:
            url = urljoin(DP, quote(href, safe="/:%?=&"))
            rows.append({"key": f"dp-whatsnew:{href}", "title": title, "detail": "", "url": url, "date": None})
    if not rows:
        raise RuntimeError("nothing in Delhi Police's What's new box (the page may have changed)")
    return rows


# ---------------------------------------------------------------- DoT's Documents tabs

DOT_API = "https://www.dot.gov.in/cms/wp-json/wp/v2/"
# the tabs under dot.gov.in/documents -> the category number in DoT's own data (children count)
DOT_TABS = {"dot_reports": 107, "dot_orders": 140, "dot_acts": 136, "dot_publications": 142,
            "dot_press": 144, "dot_gazettes": 146, "dot_guidelines": 147}
DOT_PAGE = {107: "documents", 140: "documents/orders-and-notices", 136: "documents/acts-and-policies",
            142: "documents/publications", 144: "documents/press-release", 146: "documents/gazettes-notifications",
            147: "documents/guidelines"}


def _dot_json(kind: str, **params):
    """DoT's site is a web app that fills its lists from this open data feed. Read directly; if
    DoT refuses GitHub's servers, through the relay (mea-relay, which lists www.dot.gov.in)."""
    url = DOT_API + kind
    try:
        return _get(url, params=params, headers={**HEADERS, "Accept": "application/json"}).json()
    except Exception as first:                                        # noqa: BLE001
        query = "&".join(f"{k}={quote(str(v), safe=',')}" for k, v in params.items())
        try:
            response = requests.get(RELAY, params={"u": f"{url}?{query}"}, headers=HEADERS, timeout=70)
            response.raise_for_status()
            return response.json()
        except Exception:                                             # noqa: BLE001
            raise first


def _dot(category: int) -> list:
    """The newest documents of one tab: each tab is a category (with its sub-categories) of
    DoT's "documents". The row links to the PDF itself when it can be found (document ->
    its file record in central_documents -> the media file), else to the tab. Rows are dated
    by when DoT put them up (the document's own date can be weeks older)."""
    items = _dot_json("documents", documents_category=category, per_page=30, orderby="date", order="desc",
                      _fields="id,date,title,acf")
    if not isinstance(items, list):
        raise RuntimeError("DoT's documents list did not come back as a list (the site may have changed)")
    files = {}
    for it in items:
        for f in (it.get("acf") or {}).get("file") or []:
            for n in f.get("file") or []:
                files.setdefault(it["id"], n)
    pdf = {}
    try:
        if files:
            ids = ",".join(str(n) for n in sorted(set(files.values())))
            recs = _dot_json("central_documents", include=ids, per_page=100, _fields="id,acf")
            media = {r["id"]: (r.get("acf") or {}).get("pdf") for r in recs if isinstance(r, dict)}
            ids = ",".join(str(m) for m in sorted({m for m in media.values() if m}))
            if ids:
                urls = {m["id"]: m["source_url"] for m in _dot_json("media", include=ids, per_page=100, _fields="id,source_url")}
                pdf = {doc: urls.get(media.get(n)) for doc, n in files.items()}
    except Exception:                                                 # noqa: BLE001
        pdf = {}                                                      # the link falls back to the tab
    rows = []
    for it in items:
        title = _clean((it.get("title") or {}).get("rendered"))
        if not title:
            continue
        rows.append({"key": f"dot:{it['id']}", "title": title, "detail": "",
                     "url": pdf.get(it["id"]) or "https://www.dot.gov.in/" + DOT_PAGE[category],
                     "date": _day(str(it.get("date") or "")[:10], "%Y-%m-%d")})
    return rows


def _dot_reader(category: int):
    return lambda: _dot(category)


HOME_READERS = {"eci": eci, "ncb": ncb, "lokpal": lokpal, "dgca_traffic": dgca_traffic, "dp_standing": dp_standing, "dp_circulars": dp_circulars,
                "dp_notifications": dp_notifications, "dp_gazette": dp_gazette, "dp_press": dp_press, "dp_whatsnew": dp_whatsnew,
                **{name: _dot_reader(cat) for name, cat in DOT_TABS.items()}}


# ---------------------------------------------------------------- WHO

WHO_DON = "https://www.who.int/api/news/diseaseoutbreaknews"


def who_outbreaks() -> list:
    data = _get(WHO_DON, params={"$top": 20, "$orderby": "PublicationDate desc"}).json()
    rows = []
    for item in data.get("value") or []:
        title = _clean(item.get("OverrideTitle") if item.get("UseOverrideTitle") else item.get("Title"))
        name = item.get("UrlName") or ""
        if not title or not name:
            continue
        rows.append({"key": f"who-don:{name}", "title": title, "detail": _clean(item.get("Summary"))[:1200],
                     "url": f"https://www.who.int/emergencies/disease-outbreak-news/item/{name}",
                     "date": _day(str(item.get("PublicationDate") or "")[:10], "%Y-%m-%d")})
    if not rows:
        raise RuntimeError("nothing in WHO's Disease Outbreak News (the address may have changed)")
    return rows


# ---------------------------------------------------------------- the step

# name in watchlist.yml -> (reader, source on the website, list within it)
READERS = {
    "dgca_comments": (dgca_comments, "DGCA", "Draft rules for comment"),
    "dgca_traffic": (dgca_traffic, "DGCA", "Air traffic by city pair"),   # new name 2026-10-12: its first list goes in quietly
    "dgca_schedules": (dgca_schedules, "DGCA", "Airline schedules"),
    "dgca_circulars": (dgca_circulars, "DGCA", "Circulars"),
    "dgca_notices": (dgca_notices, "DGCA", "Public notices"),
    "aaib": (aaib, "AAIB", "What's new"),
    "mca_whats_new": (mca_whats_new, "MCA", "What's new"),
    "mca_updates": (mca_updates, "MCA", "Important updates"),
    "cci_whats_new": (cci_whats_new, "CCI What's new", ""),
    "ibbi": (ibbi, "IBBI", "What's new"),
    "who_outbreaks": (who_outbreaks, "WHO outbreak news", ""),
}


def run(dry_run: bool = False) -> int:
    home = os.environ.get("WATCH_HOME") == "1"          # the Mac's copy (run-watch.sh)
    names = [] if home else website._cfg().get("pages") or []
    more = [m for m in website._cfg().get("more") or []
            if m.get("source") and (m.get("url") or m.get("feed") or m.get("reader")) and bool(m.get("home_only")) == home]
    if not website.enabled() or not (names or more):
        print("No website-only pages to read (\"site:\" in watchlist.yml).")
        return 0
    print("Reading the pages kept for the website only...")
    old = dt.datetime.now(IST).date() - dt.timedelta(days=MAX_AGE_DAYS)
    items, failed, catalogue, status = [], [], set(), []
    # "site: ai_pages": named pages whose rows the AI scores, like "ai: true" below (MCA and
    # IBBI, the user's choice 2026-10-11); remove a name to have every row count again
    ai_pages = set(website._cfg().get("ai_pages") or [])
    screened = []
    for name in names:
        if name not in READERS:
            print(f"  ! {name}: not a page this step knows")
            continue
        read, source, sub = READERS[name]
        label = f"{source} {sub}".strip()
        grp = "official" if name in ai_pages else "watch"
        catalogue.add((grp, source, sub) if sub else (grp, source))
        try:
            rows = read()
        except Exception as exc:                                      # noqa: BLE001
            print(f"  ! {label} failed: {str(exc)[:160]}")
            failed.append(label)
            status.append((label, source, exc, grp))
            continue
        status.append((label, source, None, grp))
        print(f"  {label}: {len(rows)} row(s)" + ("  [AI]" if grp == "official" else ""))
        for row in rows:
            # a scored page gets ids of its own, so its first list goes in (quietly) at once
            (screened if grp == "official" else items).append({"id": f"site:{'ai:' if grp == 'official' else ''}{row['key']}"[:300], "grp": grp, "source": source, "sub": sub,
                          "title": row["title"][:500], "body": row.get("detail", "")[:1500], "url": row.get("url", ""),
                          "issued": f"{row['date']:%b} {row['date'].day}, {row['date'].year}" if row.get("date") else "",
                          "date": row["date"].isoformat() if row.get("date") else "",
                          "quiet": bool(row.get("date") and row["date"] < old)})
        if dry_run:
            for row in rows[:3]:
                print(f"      {row.get('date') or ''} {row['title'][:90]}  {row.get('url', '')[:80]}")
    # "site: more:" -- read several at once, as the desks do
    from . import parallel                                            # noqa: PLC0415
    jobs = [(_more_label(m), lambda m=m: read_more(m)) for m in more]
    for m, (label, found, exc) in zip(more, parallel.run(jobs)):
        grp = "official" if m.get("ai") else "watch"
        catalogue.add((grp, m["source"], m["sub"]) if m.get("sub") else (grp, m["source"]))
        status.append((label, m["source"], exc, grp))
        if exc is not None:
            print(f"  ! {label} failed: {str(exc)[:160]}")
            failed.append(label)
            continue
        print(f"  {label}: {len(found)} row(s)" + ("  [AI]" if m.get("ai") else ""))
        for row in found:
            # "new_ids": a source moved from every-row to AI scoring takes ids of its own, so its
            # list goes in afresh (quietly) under the scored source
            item = {"id": f"site:{'ai:' if m.get('new_ids') else ''}{m['source']}|{row['key']}"[:300], "grp": grp, "source": m["source"], "sub": m.get("sub") or "",
                    "title": row["title"][:500], "body": row.get("detail", "")[:1500], "url": row.get("url", ""),
                    "issued": f"{row['date']:%b} {row['date'].day}, {row['date'].year}" if row.get("date") else "",
                    "date": row["date"].isoformat() if row.get("date") else "",
                    "quiet": bool(row.get("date") and row["date"] < old)}
            (screened if m.get("ai") else items).append(item)
        if dry_run:
            for row in found[:3]:
                print(f"      {row.get('date') or ''} {row['title'][:90]}  {row.get('url', '')[:80]}")
    # each source's reading, for the "last read" line beside it on the website
    by_group: dict = {}
    for entry in status:
        label, source, exc, grp = entry if len(entry) == 4 else (*entry, "watch")
        by_group.setdefault(grp, []).append((source, exc))
    report = [st for grp, results in by_group.items() for st in website.status(grp, results, lambda n: n)]
    website.send("site", items, sources=sorted(catalogue), failed=failed, all_listed=True, dry_run=dry_run,
                 status=report)
    judged = _screen(screened, dry_run)
    if judged:
        # the national bureau's view: "for a beat reporter"
        website.send("site", judged, view="bureau", all_listed=True, dry_run=dry_run)
    if not home:
        fill_general(dry_run)
    return 0


def main() -> int:
    return run(dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
