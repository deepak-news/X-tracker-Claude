"""Three regulators whose documents appear before anyone writes about them.

    CCI       Competition Commission of India -- antitrust orders, merger
              filings and approvals, and its two lists of press releases
    CERT-In   the national cyber-security agency's advisories
    DGFT      Directorate General of Foreign Trade -- notifications (changes
              to import and export policy), public notices, trade notices

About fifty items a month between the three, each the primary document
itself. They are read by BOTH desks and judged by each desk's own AI
rubric: the Tech Desk (sources.py) and the Tweets desk (national.py). What
one desk sends has no bearing on the other.

Each reader returns plain dicts:
    {"key", "title", "detail", "url", "date"}
The key is unique and never changes; its prefix says what kind of document
it is (see KINDS).

As with Parliament, nothing older than "max_age_days" is ever RETURNED,
not merely left unsent. Each desk's memory of what it has seen is capped,
and a long list that keeps its old rows -- CCI's goes back to 2009 -- would
otherwise come back as "new" once memory had moved on.
"""

import datetime as dt
import html
import re
import time

import requests
from bs4 import BeautifulSoup

BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
           "Accept-Language": "en-IN,en;q=0.9"}
TIMEOUT = 40
ATTEMPTS = 3
RETRY_PAUSE = 3
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _tidy(text) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(text or "")))
    return re.sub(r"\s+", " ", text.replace("​", "")).strip()


def _date(text: str):
    """"09/09/2026" or "September 09, 2026" -> a date; anything else -> None."""
    text = _tidy(text)
    for pattern in ("%d/%m/%Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _request(session, method: str, url: str, **kwargs):
    """A request with retries; a 4xx other than 429 is an answer, not a fault."""
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = session.request(method, url, timeout=TIMEOUT, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status and 400 <= status < 500 and status != 429:
                raise
            if attempt < ATTEMPTS:
                time.sleep(RETRY_PAUSE)
    raise last


# ----------------------------------------------------------------------
#  CCI
#  The pages are Laravel "DataTables": the table arrives from a second
#  request the page makes, newest first. The antitrust order list wants a
#  POST with the page's CSRF token; the others take a plain GET with the
#  "this is the page's own script" header.
# ----------------------------------------------------------------------
CCI = "https://www.cci.gov.in/"
CCI_ROWS = 30          # newest thirty of each list; a busy week is about ten


def _cci_table(session, page: str, columns: list, post: bool = False) -> list:
    front = _request(session, "GET", CCI + page).text
    query = {"draw": 1, "start": 0, "length": CCI_ROWS,
             "order[0][column]": 0, "order[0][dir]": "desc"}
    for i, name in enumerate(columns):
        query[f"columns[{i}][data]"] = name
        query[f"columns[{i}][name]"] = name
    headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
               "Referer": CCI + page}
    if post:
        token = re.search(r"'X-CSRF-TOKEN':\s*\"([^\"]+)\"", front)
        if not token:
            raise RuntimeError(f"CCI {page}: the page's security token was not found")
        headers["X-CSRF-TOKEN"] = token.group(1)
        answer = _request(session, "POST", CCI + page + "/list", data=query, headers=headers)
    else:
        answer = _request(session, "GET", CCI + page, params=query, headers=headers)
    rows = (answer.json() or {}).get("data")
    if rows is None:
        raise RuntimeError(f"CCI {page}: the list came back in an unexpected shape")
    return rows


# What each kind of antitrust order means, in the words a reporter would use.
CCI_ORDER_MEANING = {
    "section 26(1)": "Investigation ordered",
    "section 26(2)": "Complaint closed without investigation",
    "section 26(6)": "Case closed after investigation",
    "section 26(9)": "Further investigation ordered",
    "section 27": "Final order -- penalty or cease-and-desist",
    "section 33": "Interim order",
    "section 48 a": "Commitment accepted",
    "section 48a": "Commitment accepted",
    "section 48 b": "Settlement accepted",
    "section 48b": "Settlement accepted",
}


def _first_link(cell) -> str:
    found = re.search(r'href="([^"]+)"', str(cell or ""))
    return html.unescape(found.group(1)) if found else ""


def cci(session) -> list:
    out = []

    for row in _cci_table(session, "antitrust/orders",
                          ["DT_RowIndex", "case_no", "description", "type",
                           "main_order_date", "order_date", "files"], post=True):
        parties = _tidy(row.get("description"))
        # The site writes "XYZ" where the parties are confidential.
        who = parties if parties and parties.upper() != "XYZ" else "parties not named"
        kind = _tidy(row.get("title")) or "Order"
        meaning = CCI_ORDER_MEANING.get(kind.lower())
        out.append({
            "key": f"cci-order:{row.get('id')}",
            "title": f"{meaning} ({kind}) -- {who}" if meaning else f"{kind} -- {who}",
            "detail": " | ".join(x for x in (f"Case {_tidy(row.get('case_no'))}",
                                             _tidy(row.get("type")),
                                             f"order dated {_tidy(row.get('order_date'))}") if x),
            "url": _first_link(row.get("files")) or CCI + "antitrust/orders",
            "date": _date(row.get("order_date")),
        })

    # A merger is reported when it is filed and again when its status moves
    # (approved, deemed approved, ...) -- hence the status in the key.
    for row in _cci_table(session, "combination/orders-section31",
                          ["DT_RowIndex", "combination_no", "party_name", "form_type",
                           "notification_date", "order_status", "decision_date",
                           "summary_files", "order_files"]):
        status = _tidy(row.get("order_status")) or "Filed"
        decided = _date(row.get("decision_date"))
        filed = _date(row.get("notification_date"))
        out.append({
            "key": f"cci-merger:{_tidy(row.get('combination_no'))}|{status}",
            "title": f"{status}: {_tidy(row.get('party_name'))}",
            "detail": " | ".join(x for x in (
                _tidy(row.get("combination_no")),
                f"Form {_tidy(row.get('form_type'))}" if row.get("form_type") else "",
                f"filed {_tidy(row.get('notification_date'))}" if filed else "",
                f"decided {_tidy(row.get('decision_date'))}" if decided else "") if x),
            "url": (_first_link(row.get("order_files")) or _first_link(row.get("summary_files"))
                    or CCI + "combination/orders-section31"),
            "date": decided or filed,
        })

    for page in ("media-gallery/press-release", "antitrust/press-release"):
        for row in _cci_table(session, page, ["DT_RowIndex", "title", "order_date", "files"]):
            out.append({
                "key": f"cci-pr:{page.split('/')[0]}|{row.get('id')}",
                "title": _tidy(row.get("title")),
                "detail": f"dated {_tidy(row.get('order_date'))}",
                "url": _first_link(row.get("files")) or CCI + page,
                "date": _date(row.get("order_date")),
            })
    return out


# ----------------------------------------------------------------------
#  CERT-In -- one plain page per year, newest first. In January last
#  year's page is read too, so December's last advisories are not missed.
# ----------------------------------------------------------------------
CERTIN = "https://www.cert-in.org.in/"
_ADVISORY = re.compile(
    r"CERT-In Advisory (CIAD-\d{4}-\d+)\s*\(\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})\s*\)\s*"
    r"(.*?)(?=CERT-In Advisory CIAD-|$)")


def certin(session) -> list:
    today = dt.datetime.now(IST).date()
    years = [today.year] + ([today.year - 1] if today.month == 1 else [])
    out = []
    for year in years:
        page = _request(session, "GET", CERTIN + f"s2cMainServlet?pageid=PUBADVLIST02&year={year}")
        text = BeautifulSoup(page.text, "html.parser").get_text(" ", strip=True)
        found = _ADVISORY.findall(text)
        if not found and year == today.year and today.month > 1:
            raise RuntimeError("the advisories list did not read as it usually does")
        for code, when, title in found:
            out.append({
                "key": f"certin:{code}",
                "title": _tidy(title)[:200] or code,
                "detail": f"{code} | {re.sub(r'\s+', ' ', when)}",
                "url": CERTIN + f"s2cMainServlet?pageid=PUBVLNOTES02&VLCODE={code}",
                "date": _date(re.sub(r"\s+", " ", when)),
            })
    return out


# ----------------------------------------------------------------------
#  DGFT -- three ordinary tables. Numbers are written inconsistently
#  ("36", "34/2026-27", "Corrigendum to ..."), so the key is the document's
#  own storage folder, which is unique and never changes.
# ----------------------------------------------------------------------
DGFT = "https://www.dgft.gov.in/CP/?opt="
DGFT_LISTS = {"notification": "Notification", "public-notice": "Public Notice",
              "trade-notice": "Trade Notice"}
DGFT_ROWS = 25


def dgft(session) -> list:
    out = []
    for opt, kind in DGFT_LISTS.items():
        page = _request(session, "GET", DGFT + opt)
        table = BeautifulSoup(page.text, "html.parser").find("table", id="metaTable")
        if table is None:
            raise RuntimeError(f"DGFT {kind.lower()}s: the table was not where it usually is")
        for row in table.find_all("tr")[1:DGFT_ROWS + 1]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) < 5:
                continue
            number, _, description, date = cells[1], cells[2], cells[3], cells[4]
            link = row.find("a", href=True)
            url = link["href"] if link else DGFT + opt
            folder = re.search(r"/dgftprod/([0-9a-f-]{36})/", url)
            ident = folder.group(1) if folder else f"{_tidy(number)}|{_tidy(date)}"
            label = _tidy(number)
            # A plain "36" means nothing on its own; "Notification 36" does.
            label = label if not label[:1].isdigit() else f"{kind} {label}"
            out.append({
                "key": f"dgft-{opt}:{ident}",
                "title": _tidy(description),
                "detail": f"{label} | dated {_tidy(date)}",
                "url": url.replace(" ", "%20"),
                "date": _date(date),
            })
    return out


READERS = {"cci": cci, "certin": certin, "dgft": dgft}
NAMES = {"cci": "CCI", "certin": "CERT-In", "dgft": "DGFT"}

# What each row is, in words, for the AI and for the email.
KINDS = {"cci-order": "CCI antitrust order",
         "cci-merger": "CCI merger decision",
         "cci-pr": "CCI press release",
         "certin": "CERT-In security advisory",
         "dgft-notification": "DGFT notification (import/export policy)",
         "dgft-public-notice": "DGFT public notice",
         "dgft-trade-notice": "DGFT trade notice"}


def describe(row: dict) -> str:
    """One line the AI can judge: what it is, what it says, the particulars."""
    kind = KINDS.get(row["key"].split(":", 1)[0], "Regulator")
    return f"{kind}: {row['title']}. {row.get('detail', '')}".strip()


def read(which: str, max_age_days: int = 30) -> list:
    """One regulator's rows, minus anything too old to be news."""
    session = requests.Session()
    session.headers.update(BROWSER)
    rows = READERS[which](session)
    today = dt.datetime.now(IST).date()
    return [r for r in rows if r.get("date") and (today - r["date"]).days <= max_age_days]
