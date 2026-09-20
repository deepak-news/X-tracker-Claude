"""Watches Parliament's own website, sansad.in, for both Houses.

The rest of the government watch reads HTML pages and looks for new rows.
sansad.in cannot be read that way: every page draws itself in the browser
after fetching its content from an API, so there is nothing in the HTML to
read. The APIs it calls are public, need no key and answer in JSON, so this
reads those directly -- the same numbers the website itself shows you.

WHAT IS WATCHED, and why it is grouped the way it is:

  every run      Bills of the current year; committee meetings called;
                 the Lok Sabha Secretariat's press releases.
                 These are small, and a meeting notice is worth having the
                 moment it appears -- it is often a day or two's warning.

  slow lane      Committee reports presented; Rajya Sabha press releases
                 and its committees' press releases; the Rajya Sabha's own
                 "latest updates" list.
                 The reports list is not sorted by date -- it comes grouped
                 by committee -- so the only honest way to find what is new
                 is to read the whole list. For the Rajya Sabha that is
                 three megabytes. Nobody needs that every fifteen minutes,
                 so it is read on the slower clock ("slow_minutes").

  sitting days   The papers of the day: List of Business (the agenda) and
                 any revision of it, Bulletin Part I and Part II, the
                 Synopsis of the debate, the papers to be laid, and the
                 papers actually laid on the Table.
                 One request per House per date answers all of that, and an
                 empty answer is also how this knows the House did not sit.
                 So the last three dates are asked for every run -- six
                 small requests -- and between sessions all six come back
                 empty and nothing else is asked.

Questions and answers are deliberately NOT here. There are thousands of them
per session and they are handled separately.

ONE RULE THAT MATTERS: nothing older than "max_age_days" is ever returned.
Not "not emailed" -- not returned at all. The watch remembers what it has
sent by identifier, and that memory is capped; if this handed back every one
of the four thousand committee reports ever presented, the memory would fill
with 2014 and start forgetting this week. So the age window is what keeps
the remembering small enough to be reliable.
"""

import datetime as dt
import html
import re
import time

import requests

# The website's two back ends. The Lok Sabha one also serves the pages that
# cover both Houses -- committees, and the joint list of bills.
LS_API = "https://sansad.in/api_ls/"
RS_API = "https://sansad.in/api_rs/"
# The Rajya Sabha keeps its committees and press releases on a separate
# service of its own. This is the address its own pages call.
RS_COMMITTEE_API = ("https://integration.rajyasabha.digital/"
                    "committee-integration/api/v1/web/")

BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://sansad.in/ls",
}
TIMEOUT = 45

# Parliament's dates are Indian dates. A run just after midnight UTC is
# still the same working day in Delhi, and asking for "today" in UTC would
# miss the sitting that is still going on.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# How many days back to look for the papers of the day. A List of Business
# is published the evening before; a Synopsis and the papers laid can be
# uploaded a day or two after the sitting. Three days covers the lot and
# still costs nothing on days no House sat.
CATCH_DAYS = 3


# This site returns a 500 now and then for a request that works perfectly
# well on the next attempt -- measured at up to one call in two for the daily
# calendar of some dates, which is the worst possible place for it. That
# matters more here than elsewhere because a date on which nothing was
# published answers 200 with a row of nulls, so a 500 never means "nothing":
# it always means "ask again".
#
# Four attempts, because at a coin-flip failure rate three would still give
# up on one date in eight. When all four fail it is reported as a failure and
# not as an empty day -- and the same date is asked for again on the next run,
# fifteen minutes later, for three days running.
ATTEMPTS = 4
RETRY_PAUSE = 2
FLAKY = (500, 502, 503, 504)


def _get(url: str) -> object:
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=BROWSER, timeout=TIMEOUT)
            if response.status_code in FLAKY and attempt < ATTEMPTS:
                raise requests.HTTPError(f"{response.status_code} (transient)",
                                         response=response)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # A 400 or a 404 is an answer, not a fault: it is how some of
            # these endpoints say "no such thing". Do not keep asking.
            if status and status not in FLAKY:
                raise
            if attempt < ATTEMPTS:
                time.sleep(RETRY_PAUSE)
    raise last


def _tidy(text) -> str:
    """Trim, collapse spaces, and undo HTML escaping.

    The Rajya Sabha's press releases arrive with their punctuation still
    written as HTML -- "&#8220;Role of Technology&#8221;" -- and several
    fields are padded out with trailing spaces to a fixed column width.
    """
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(text))).strip()


# ------------------------------------------------------------------- dates

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _date(value) -> dt.date | None:
    """A date out of any of the shapes this website uses.

    Four different teams built four different parts of it, so the same kind
    of fact arrives as "12/08/2026", "12-Aug-2026", "2026-03-25" or
    "2026-08-10 00:00:00.0" depending on which API answered.
    """
    text = _tidy(value)
    if not text:
        return None
    text = text.split(" ")[0] if re.match(r"^\d{4}-\d{2}-\d{2} ", text) else text
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = (int(x) for x in match.groups())
    else:
        match = re.match(r"^(\d{1,2})[/-]([A-Za-z]{3,}|\d{1,2})[/-](\d{4})", text)
        if not match:
            return None
        day, middle, year = match.group(1), match.group(2), match.group(3)
        month = MONTHS.get(middle[:3].lower()) if middle[:1].isalpha() else int(middle)
        if not month:
            return None
        day, year = int(day), int(year)
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _newest(*values) -> dt.date | None:
    dates = [d for d in (_date(v) for v in values) if d]
    return max(dates) if dates else None


ROMAN = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
         (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
         (5, "V"), (4, "IV"), (1, "I")]


def _roman(number: int) -> str:
    """Lok Sabha sessions are numbered in Roman numerals in the API.

    Not a stylistic choice on our part: papersLaid?session=8 answers with an
    empty list and no error, and papersLaid?session=VIII answers with the
    session. This is the only place it matters.
    """
    out, left = "", int(number)
    for value, letters in ROMAN:
        while left >= value:
            out += letters
            left -= value
    return out


# --------------------------------------------------------------- the calendar


def _which_session() -> dict:
    """Which session each House is in, and for the Lok Sabha, which Lok Sabha.

    Needed only for the papers laid on the Table: that list is addressed by
    session, not by date. Two separate questions to two separate back ends,
    because the two Houses number their sessions differently -- the Rajya
    Sabha counts from 1952 and is on its 271st, the Lok Sabha counts from
    each general election.

    Note what is NOT taken from here: the list of sitting days. The Lok
    Sabha's answer leaves that empty for the session in progress, which is
    exactly the session that matters, so the sitting days are worked out by
    asking for each date's papers instead.
    """
    calendar = {"at": dt.datetime.now(dt.timezone.utc).isoformat()}

    houses = _get(LS_API + "business/getAllLoksabhaAndSession?locale=en") or []
    if houses:
        current = houses[-1]
        session = (current.get("sessions") or [{}])[-1]
        calendar["ls"] = {"loksabha": current.get("loksabha"),
                          "session": session.get("sessionNo")}

    sessions = _get(RS_API + "business/sessionDates?docType=LOB") or []
    if sessions:
        calendar["rs"] = {"session": sessions[0].get("session")}
    return calendar


# ---------------------------------------------------------- papers of the day
#
# One request answers the whole question for one House on one date. Its reply
# carries everything published for that sitting: the List of Business and any
# revision of it, Bulletin Part I and Part II, the Synopsis of the debate, and
# the list of papers to be laid. Asking it for the last three dates is also
# how this knows whether a House sat at all -- an empty reply means it did
# not, so there is no need to keep a sitting calendar in step with the site.
#
# The question list is in that reply too, and is deliberately ignored.

DAILY_CALENDAR = "ppHome/DailyCalendar"

# field in the reply,      kind,        what to call it
DAY_PAPERS = [
    ("listOfBusinessUrls", "lob",      "List of Business"),
    ("bulletin1Url",       "bulletin", "Bulletin Part I"),
    ("bulletin2Url",       "bulletin", "Bulletin Part II"),
    ("synopsisUrl",        "synopsis", "Synopsis of the debate"),
    ("papersToBeLaidUrl",  "papers",   "Papers to be laid"),
]

HOUSE_NAME = {"LS": "Lok Sabha", "RS": "Rajya Sabha"}
HOUSE_API = {"LS": LS_API, "RS": RS_API}


def _day_papers(house: str, day: dt.date) -> list:
    """Everything one House published for one sitting date.

    A date on which nothing was published is reported in three different
    ways depending on the House and the day -- an empty object, a row of
    nulls, or a 500 -- so all three are read as the same answer: the House
    did not sit, or has published nothing yet.
    """
    url = (HOUSE_API[house] + DAILY_CALENDAR
           + f"?day={day.day}&month={day.month}&year={day.year}&locale=en")
    try:
        answer = _get(url)
    except requests.HTTPError as exc:
        # 400 and 404 are how the two Houses say "that paper does not exist
        # here" -- the Rajya Sabha has no "papers to be laid" endpoint at
        # all. A 500 has already been retried inside _get, so if one gets
        # this far it is a real fault and is reported as one.
        if exc.response is not None and exc.response.status_code in (400, 404):
            return []
        raise
    if not isinstance(answer, dict):
        return []

    out = []
    for field, kind, label in DAY_PAPERS:
        value = answer.get(field)
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if not isinstance(row, dict) or not row.get("url"):
                continue
            # "Revised List of Business" comes back under the same field as
            # the original, with type "R". It is a different document and
            # frequently the more interesting one, so it is kept separately.
            name = _tidy(row.get("name")) or label
            out.append({
                "key": f"{kind}:{house}|{day.isoformat()}|{name[:40]}",
                "kind": kind,
                "title": f"{HOUSE_NAME[house]} -- {name}, {day.strftime('%d %b %Y')}",
                # No detail line: the badge and the title already say which
                # House, which paper and which sitting, and the document
                # itself is a PDF with nothing further to quote from.
                "detail": "",
                "url": _tidy(row.get("url")),
                "date": day,
            })
    return out


# ------------------------------------------------------------------- streams
#
# Every reader below returns a list of plain dicts, each with:
#   key    an identifier that never changes, so an item is reported once
#   kind   which badge the email shows
#   title  the one line worth reading
#   detail the supporting line
#   url    the document itself
#   date   what the item is dated, used for the age window
#   lines  optional extra lines, listed under the item


def _bills(year: int) -> list:
    """Bills of the current year, and what has happened to each.

    One list serves both Houses -- a bill belongs to Parliament, not to a
    House -- and every stage it has reached is a column on the same row.
    So a bill appears here once when it is introduced, again when a House
    passes it, and again when it receives assent: four possible items from
    one row, each remembered separately.
    """
    url = (RS_API + "legislation/getBills?page=1&size=300"
           f"&billYear={year}&sortOn=billIntroducedDate&sortBy=desc&locale=en")
    out = []
    for row in (_get(url) or {}).get("records") or []:
        name = _tidy(row.get("billName"))
        if not name:
            continue
        number = _tidy(row.get("billNumber")) or "-"
        who = _tidy(row.get("billType"))
        ministry = _tidy(row.get("ministryName"))
        introduced_in = _tidy(row.get("billIntroducedInHouse"))
        stages = [
            ("introduced", "introduced in " + (introduced_in or "Parliament"),
             row.get("billIntroducedDate"), row.get("billIntroducedFile")),
            ("passed-ls", "passed by the Lok Sabha",
             row.get("billPassedInLSDate"), row.get("billPassedInLSFile")),
            ("passed-rs", "passed by the Rajya Sabha",
             row.get("billPassedInRSDate"), row.get("billPassedInRSFile")),
            ("assent", "received the President's assent",
             row.get("billAssentedDate"), row.get("billGazettedFile")),
        ]
        for stage, wording, when, document in stages:
            day = _date(when)
            if not day:
                continue
            act = ""
            if stage == "assent" and row.get("actNo"):
                act = f"Act No. {_tidy(row.get('actNo'))} of {_tidy(row.get('actYear'))}"
            out.append({
                "key": f"bill:{number}|{year}|{_tidy(name)[:60]}|{stage}",
                "kind": "bill",
                "title": f"{name} -- {wording}",
                "detail": " | ".join(x for x in (
                    f"Bill No. {number}", who, ministry,
                    _tidy(row.get("billCategory")), act,
                    day.strftime("%d %b %Y")) if x),
                "url": _tidy(document) or "https://sansad.in/ls/legislation/bills",
                "date": day,
            })
    return out


def _ls_meetings(limit: int) -> list:
    """Lok Sabha committee meetings, newest first, future ones included.

    A meeting notice is the earliest public sign of what a committee is
    about to do, and it names the ministry being called in. This list is
    genuinely sorted by date, so the first page is the useful one.
    """
    url = (LS_API + "committee/lsRSallMeetings?committeeCode="
           f"&page=1&size={limit}&locale=en")
    out = []
    for row in (_get(url) or {}).get("records") or []:
        day = _date(row.get("meetingDate"))
        committee = _tidy(row.get("CommitteeName"))
        if not day or not committee:
            continue
        agenda = _tidy(row.get("MeetingAgenda"))
        out.append({
            "key": f"meeting:LS|{row.get('meetingId')}",
            "kind": "meeting",
            "title": f"{committee} meets on {day.strftime('%d %b %Y')}",
            "detail": " | ".join(x for x in (
                _tidy(row.get("CommitteeType")), _tidy(row.get("MeetingTime")),
                _tidy(row.get("MeetingVenue"))) if x),
            "url": _tidy(row.get("url")) or _tidy(row.get("noticeUrl"))
                   or "https://sansad.in/ls/committee/committee-meetings",
            "date": day,
            "lines": [agenda] if agenda else [],
        })
    return out


def _rs_meetings(limit: int) -> list:
    """The same for the Rajya Sabha, from its own committee service."""
    url = RS_COMMITTEE_API + f"committee-meetings?page=1&size={limit}"
    answer = (_get(url) or {}).get("data") or {}
    rows = answer.get("records") or answer.get("data") or []
    out = []
    for row in rows:
        day = _date(row.get("dmeetdate"))
        committee = _tidy(row.get("comName"))
        if not day or not committee:
            continue
        agenda = _tidy(row.get("agenda"))
        out.append({
            "key": f"meeting:RS|{row.get('nmeetid')}",
            "kind": "meeting",
            "title": f"{committee} Committee meets on {day.strftime('%d %b %Y')}",
            "detail": " | ".join(x for x in (
                "Rajya Sabha", _tidy(row.get("meet_time")),
                _tidy(row.get("vvenuename"))) if x),
            "url": _tidy(row.get("noticefile_path"))
                   or "https://sansad.in/rs/committees/committee-meetings",
            "date": day,
            "lines": [agenda] if agenda else [],
        })
    return out


def _reports(house: str, loksabha) -> list:
    """Committee reports presented to a House.

    Both Houses are served by one endpoint, but it hands the list back
    grouped by committee rather than by date -- the first hundred rows are
    three committees' worth, going back to 2022. There is no sort parameter
    that works (the one the website sends sorts the dates as text, so
    "31-Jul-2025" lands above "12-Aug-2026"). So the whole list is read and
    the age window does the filtering. That is why this sits in the slow
    lane: for the Rajya Sabha the whole list is three megabytes.
    """
    if house == "L" and not loksabha:
        # Without it the list is every report since 1999 -- seven thousand of
        # them. Reading a truncated version of that would look like success
        # while quietly hiding this week's reports, so refuse instead.
        raise RuntimeError("which Lok Sabha it is is not known yet")

    def page(size: int) -> dict:
        return _get(LS_API + f"committee/lsRSAllReports?house={house}&mpCode="
                    f"&lsNo={loksabha if house == 'L' else ''}"
                    f"&page=1&size={size}&locale=en") or {}

    answer = page(1200 if house == "L" else 3600)
    rows = answer.get("records") or []
    total = int((answer.get("_metadata") or {}).get("totalElements") or 0)
    if total > len(rows):
        # The list has outgrown the guess. Ask again for all of it rather
        # than work from the part that fitted -- it is not sorted by date,
        # so the part that was cut off is not the old part.
        rows = (page(total + 100).get("records") or []) or rows

    out = []
    for row in rows:
        subject = _tidy(row.get("SubjectOfTheReport"))
        committee = _tidy(row.get("CommitteeName"))
        if not subject or not committee:
            continue
        day = _newest(row.get("PresentedInLS"), row.get("LaidInRS"),
                      row.get("dateOfPresentation"), row.get("PresentedToSpeaker"))
        if not day:
            continue
        number = row.get("reportNo")
        where = "Lok Sabha" if house == "L" else "Rajya Sabha"
        out.append({
            "key": f"report:{house}|{row.get('Loksabha') or ''}|{committee[:40]}|{number}",
            "kind": "report",
            "title": f"{committee}: report {number} presented",
            "detail": " | ".join(x for x in (
                where, f"presented {day.strftime('%d %b %Y')}") if x),
            "url": _tidy(row.get("url")) or (
                "https://sansad.in/ls/committee/subjects-reports" if house == "L"
                else "https://sansad.in/rs/committees/introduction"),
            "date": day,
            "lines": [subject],
        })
    return out


def _ls_press(limit: int) -> list:
    """Lok Sabha Secretariat press releases."""
    url = (LS_API + "public/ppr/press-release?searchParam=&fromDate=&toDate="
           f"&page=1&size={limit}&locale=en")
    out = []
    for row in (_get(url) or {}).get("records") or []:
        title = _tidy(row.get("title"))
        if not title:
            continue
        day = _newest(row.get("publishedAt"), row.get("eventDate"))
        out.append({
            "key": f"pr:LS|{row.get('id')}",
            "kind": "pr",
            "title": title,
            "detail": " | ".join(x for x in (
                "Lok Sabha Secretariat",
                day.strftime("%d %b %Y") if day else "") if x),
            "url": _tidy(row.get("fileUrl")) or "https://sansad.in/ls/pressRelease",
            "date": day,
        })
    return out


def _rs_press(committee_only: bool) -> list:
    """Rajya Sabha press releases: the Chairman's, or its committees'.

    The committees' ones matter more than they sound: a committee usually
    publishes a press release summarising a report on the day it presents
    it, and that summary is the part a reporter actually wants.
    """
    endpoint = ("GetPress_relese_committee" if committee_only
                else "getpress_relese_otherhome")
    answer = (_get(RS_COMMITTEE_API + endpoint) or {}).get("data") or []
    rows = answer if isinstance(answer, list) else answer.get("records") or []
    out = []
    for row in rows:
        title = _tidy(row.get("txtpress"))
        if not title:
            continue
        day = _newest(row.get("pub_date"), row.get("pub_date_order"))
        committee = _tidy(row.get("vmaincomname"))
        out.append({
            "key": f"pr:{'RSC' if committee_only else 'RS'}|{row.get('pressid')}",
            "kind": "pr",
            "title": title,
            "detail": " | ".join(x for x in (
                committee + " Committee" if committee else "Rajya Sabha Secretariat",
                day.strftime("%d %b %Y") if day else "") if x),
            "url": _tidy(row.get("rptpressfilepath")) or _tidy(row.get("rptpressfile_dsp"))
                   or "https://sansad.in/rs/pressRelease",
            "date": day,
        })
    return out


def _rs_updates() -> list:
    """The Rajya Sabha's own "latest updates" strip.

    Two thirds of it is tenders for stationery and furniture, which are
    dropped. What is left is the interesting part: the lists of Zero Hour
    and Special Mention notices admitted for each sitting, which say who is
    raising what before the House hears it.
    """
    out = []
    for row in _get(RS_API + "ppHome/latestUpdates?locale=en") or []:
        kind = _tidy(row.get("type"))
        title = _tidy(row.get("title"))
        if not title or kind.lower() == "tenders":
            continue
        day = _date(row.get("startDate"))
        out.append({
            "key": f"notice:RS|{row.get('id')}",
            "kind": "notice",
            "title": title,
            "detail": " | ".join(x for x in (
                "Rajya Sabha", kind, _tidy(row.get("venue")),
                day.strftime("%d %b %Y") if day else "") if x),
            "url": _tidy(row.get("pdfFileUrl")) or "https://sansad.in/rs",
            "date": day,
        })
    return out


def _papers_laid(house: str, session) -> list:
    """Papers laid on the Table, grouped into one item per sitting day.

    There are around a thousand of these in a single session -- annual
    reports, audit reports, rules and regulations, a few dozen every
    sitting. One alert each would be unreadable, and the interesting fact
    is usually "what came to the Table today" rather than any single line.
    So a day's papers arrive as one item, with the titles listed under it
    and the ministries counted.
    """
    if not session:
        return []
    if house == "LS":
        loksabha, number = session
        if not loksabha or not number:
            return []
        url = (LS_API + f"business/papersLaid?loksabha={loksabha}"
               f"&session={_roman(number)}&minCodes=&paperDateFrom=&paperDateTo=&locale=en")
    else:
        url = RS_API + f"business/papersLaid?session={session}&page=1&size=4000"

    answer = _get(url) or []
    rows = answer if isinstance(answer, list) else answer.get("records") or []
    by_day: dict = {}
    for row in rows:
        day = _date(row.get("paperDate") or row.get("date"))
        title = _tidy(row.get("title"))
        if not day or not title:
            continue
        by_day.setdefault(day, []).append(
            (_tidy(row.get("ministryName") or row.get("ministry")), title))

    out = []
    for day, papers in by_day.items():
        ministries = sorted({m for m, _ in papers if m})
        out.append({
            "key": f"papers:{house}|{day.isoformat()}",
            "kind": "papers",
            "title": (f"{len(papers)} paper(s) laid on the Table of the "
                      f"{HOUSE_NAME[house]} on {day.strftime('%d %b %Y')}"),
            "detail": (f"{len(ministries)} ministries: "
                       + ", ".join(m.title() for m in ministries[:8])
                       + (" and others" if len(ministries) > 8 else "")),
            "url": ("https://sansad.in/ls/business/papers-laid" if house == "LS"
                    else "https://sansad.in/rs/house-business/papers-laid"),
            "date": day,
            "lines": [f"[{m.title()}] {t}" if m else t for m, t in papers[:25]],
        })
    return out


# ------------------------------------------------------------------- the read

# Which lists run every time, and which wait for the slower clock. These names
# are what appears in the log and what "streams:" in watchlist.yml switches.
FAST = ("bills", "ls_meetings", "rs_meetings", "ls_press")
SLOW = ("ls_reports", "rs_reports", "rs_press", "rs_committee_press", "rs_updates")
SITTING = ("day_papers", "papers")
ALL_STREAMS = FAST + SLOW + SITTING


def _due(memory: dict, name: str, minutes: int) -> bool:
    last = (memory.get("last") or {}).get(name)
    if not last or minutes <= 0:
        return True
    try:
        when = dt.datetime.fromisoformat(last)
    except ValueError:
        return True
    age = dt.datetime.now(dt.timezone.utc) - when
    return age >= dt.timedelta(minutes=minutes)


def _mark(memory: dict, name: str) -> None:
    memory.setdefault("last", {})[name] = dt.datetime.now(dt.timezone.utc).isoformat()


def read(entry: dict, memory: dict, out_of_time=lambda: False) -> list:
    """Every new thing on sansad.in, as rows for the watch to email.

    "memory" is the watch's own memory dict. Two things are kept in it that
    are not item identifiers: when each of the slower lists was last read,
    and which session each House is in. Everything else -- what has already
    been reported -- is remembered by the watch itself.
    """
    mine = memory.setdefault("sansad", {})
    wanted = set(entry.get("streams") or ALL_STREAMS)
    age_limit = int(entry.get("max_age_days", 30))
    slow_minutes = int(entry.get("slow_minutes", 120))
    calendar_minutes = int(entry.get("calendar_minutes", 360))
    today = dt.datetime.now(IST).date()

    found, failed, skipped = [], [], 0

    def run(name: str, minutes: int, job) -> list:
        """One list, its failure kept to itself, its age window applied."""
        nonlocal skipped
        if minutes and not _due(mine, name, minutes):
            skipped += 1
            return []
        if out_of_time():
            print(f"    {name}: out of time, left for the next run")
            return []
        try:
            rows = job()
        except Exception as exc:                                  # noqa: BLE001
            # One dead endpoint must not take the other eighteen with it.
            print(f"    ! sansad {name} failed: {str(exc)[:90]}")
            failed.append(name)
            return []
        if minutes:
            _mark(mine, name)
        # The age window. Anything older is dropped here and never reaches
        # the watch's memory, which is what keeps that memory small enough
        # to be trustworthy. A date in the future -- a meeting called for
        # next week -- is always inside the window.
        recent = [r for r in rows
                  if r.get("date") and (today - r["date"]).days <= age_limit]
        if rows:
            print(f"    {name}: {len(rows)} row(s), {len(recent)} within "
                  f"{age_limit} days")
        found.extend(recent)
        return recent

    # --- the papers of the day, for both Houses --------------------------
    #
    # Done first, because whether anything came back is also the answer to
    # "did either House sit?", which decides whether the papers laid on the
    # Table are worth asking for.
    days = [today - dt.timedelta(days=n) for n in range(CATCH_DAYS)]
    sat = set()
    if "day_papers" in wanted:
        for house in ("LS", "RS"):
            for day in days:
                if run(f"day_papers:{house}:{day.isoformat()}", 0,
                       lambda h=house, d=day: _day_papers(h, d)):
                    sat.add(house)

    # --- which session each House is in ---------------------------------
    #
    # Wanted for two things: addressing the papers laid on the Table, which
    # are listed by session rather than by date, and knowing which Lok Sabha
    # this is, without which the committee reports list is seven thousand
    # rows going back to 1999. It changes a handful of times a year, so it
    # is kept and refreshed on its own slow clock.
    calendar = mine.get("calendar") or {}
    if _due(mine, "calendar", calendar_minutes) or not calendar:
        try:
            calendar = _which_session()
            mine["calendar"] = calendar
            _mark(mine, "calendar")
            ls, rs = calendar.get("ls") or {}, calendar.get("rs") or {}
            print(f"    (latest session: {ls.get('loksabha')}th Lok Sabha "
                  f"session {ls.get('session')}, Rajya Sabha session "
                  f"{rs.get('session')})")
        except Exception as exc:                                  # noqa: BLE001
            print(f"    ! which session it is could not be read ({str(exc)[:60]})")
            failed.append("calendar")

    if "papers" in wanted and "LS" in sat:
        house = calendar.get("ls") or {}
        run("ls_papers_laid", 0, lambda: _papers_laid(
            "LS", (house.get("loksabha"), house.get("session"))))
    if "papers" in wanted and "RS" in sat:
        run("rs_papers_laid", 0, lambda: _papers_laid(
            "RS", (calendar.get("rs") or {}).get("session")))
    if "day_papers" in wanted and not sat:
        print("    (neither House sat in the last "
              f"{CATCH_DAYS} days -- no papers of the day to read)")

    # --- the lists that run every time ----------------------------------
    if "bills" in wanted:
        run("bills", 0, lambda: _bills(today.year))
    if "ls_meetings" in wanted:
        run("ls_meetings", 0, lambda: _ls_meetings(40))
    if "rs_meetings" in wanted:
        run("rs_meetings", 0, lambda: _rs_meetings(30))
    if "ls_press" in wanted:
        run("ls_press", 0, lambda: _ls_press(20))

    # --- the big lists, on the slower clock ------------------------------
    if "ls_reports" in wanted:
        current = (mine.get("calendar") or {}).get("ls") or {}
        run("ls_reports", slow_minutes,
            lambda: _reports("L", current.get("loksabha")))
    if "rs_reports" in wanted:
        run("rs_reports", slow_minutes, lambda: _reports("R", None))
    if "rs_press" in wanted:
        run("rs_press", slow_minutes, lambda: _rs_press(False))
    if "rs_committee_press" in wanted:
        run("rs_committee_press", slow_minutes, lambda: _rs_press(True))
    if "rs_updates" in wanted:
        run("rs_updates", slow_minutes, lambda: _rs_updates())

    if skipped:
        print(f"    ({skipped} list(s) on the slower clock, not due yet)")
    if failed:
        print(f"    (sansad lists unavailable this run: {', '.join(failed)})")
    return found
