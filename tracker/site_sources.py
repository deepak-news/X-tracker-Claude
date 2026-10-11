"""Pages read only for the website (PTI Source Alerts), never mailed. Added 2026-10-11 for
the aviation and corporate-affairs reporter.

    dgca_comments   DGCA: draft rules out for public comment ("Comments invited")
    aaib            AAIB: What's new -- accident and incident investigation reports
    mca_whats_new   MCA: What's new
    mca_updates     MCA: Important updates
    cci_whats_new   CCI: What's new (its antitrust orders and press releases are already
                    in the official stream, read by regulators.py)
    ibbi            IBBI: What's new -- resolution plans, orders, notices

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
import re
import sys
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from . import regulators, website

TIMEOUT = 40
MAX_AGE_DAYS = 21
HEADERS = dict(regulators.BROWSER)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _clean(text) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(text or ""))).split())


def _get(url: str, **kw):
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
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


# ---------------------------------------------------------------- the step

# name in watchlist.yml -> (reader, source on the website, list within it)
READERS = {
    "dgca_comments": (dgca_comments, "DGCA", "Draft rules for comment"),
    "aaib": (aaib, "AAIB", "What's new"),
    "mca_whats_new": (mca_whats_new, "MCA", "What's new"),
    "mca_updates": (mca_updates, "MCA", "Important updates"),
    "cci_whats_new": (cci_whats_new, "CCI What's new", ""),
    "ibbi": (ibbi, "IBBI", "What's new"),
}


def run(dry_run: bool = False) -> int:
    names = website._cfg().get("pages") or []
    if not website.enabled() or not names:
        print("No website-only pages to read (\"site:\" in watchlist.yml).")
        return 0
    print("Reading the pages kept for the website only...")
    old = dt.datetime.now(IST).date() - dt.timedelta(days=MAX_AGE_DAYS)
    items, failed, catalogue = [], [], set()
    for name in names:
        if name not in READERS:
            print(f"  ! {name}: not a page this step knows")
            continue
        read, source, sub = READERS[name]
        label = f"{source} {sub}".strip()
        catalogue.add(("watch", source, sub) if sub else ("watch", source))
        try:
            rows = read()
        except Exception as exc:                                      # noqa: BLE001
            print(f"  ! {label} failed: {str(exc)[:160]}")
            failed.append(label)
            continue
        print(f"  {label}: {len(rows)} row(s)")
        for row in rows:
            items.append({"id": f"site:{row['key']}"[:300], "grp": "watch", "source": source, "sub": sub,
                          "title": row["title"][:500], "body": row.get("detail", "")[:1500], "url": row.get("url", ""),
                          "issued": f"{row['date']:%b} {row['date'].day}, {row['date'].year}" if row.get("date") else "",
                          "date": row["date"].isoformat() if row.get("date") else "",
                          "quiet": bool(row.get("date") and row["date"] < old)})
        if dry_run:
            for row in rows[:3]:
                print(f"      {row.get('date') or ''} {row['title'][:90]}  {row.get('url', '')[:80]}")
    website.send("site", items, sources=sorted(catalogue), failed=failed, all_listed=True, dry_run=dry_run)
    return 0


def main() -> int:
    return run(dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
