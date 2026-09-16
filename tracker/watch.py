"""Watches government pages where every change matters.

This is deliberately NOT the news pipeline. The news tracker reads dozens of
sources, throws most of it away, and asks an AI whether what is left is worth
your attention. These pages are the opposite case: they are hand-picked, they
move rarely, and when they move it is always worth knowing. So nothing here is
scored, nothing is filtered, and no AI request is made -- if a page gained a
row, you get told.

Three kinds of watching, because the pages behave in three ways:

  uploads   A list page that gains rows. Each row is remembered by its own
            identifier, so the same order is never reported twice even though
            it stays on the page for a week.
  gazette   Same idea, but the e-Gazette needs a session opened first and
            lists every ministry together, so rows are filtered by ministry.
  pages     A page whose content is expected to stay identical for weeks.
            Its text is fingerprinted; when the fingerprint moves, the
            actual lines that were added or removed are reported.

Alerts go to their own recipient list (WATCH_MAIL_TO), not to the news list.

    python -m tracker.watch            # normal
    python -m tracker.watch --dry-run  # show what it would send, send nothing
"""

import argparse
import datetime as dt
import os
import hashlib
import html
import json
import pathlib
import re
import sys
import time
from dataclasses import dataclass, field

import requests
import urllib3
import yaml
from bs4 import BeautifulSoup

from . import email_out

ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "watchlist.yml"
STATE = ROOT / "state.json"

RECIPIENT_ENV = "WATCH_MAIL_TO"

# Some sites refuse hosting networks outright -- they answer a home broadband
# connection and reset the connection from any data centre, in any country.
# DoPT is one of them, proven: an Indian data centre in Mumbai is reset just
# as GitHub's runners are, while the same machine reaches e-Gazette and ED
# normally. Those sources are marked "home_only: true" in the watchlist and
# read by a copy running on a home machine, which sets WATCH_HOME=1.
#
# The two halves never overlap, so they cannot send the same alert twice:
# a run with WATCH_HOME=1 reads ONLY the home-only sources, and a run
# without it reads only the others.
HOME_ENV = "WATCH_HOME"

# The other way to reach a site that only answers consumer connections: send
# the request through one. A residential proxy exits through a real broadband
# or mobile line, so the site sees an ordinary reader. Set WATCH_PROXY to the
# provider's endpoint (http://user:pass@host:port) and the blocked sources
# move back onto the server, checked as often as the server runs.
#
# Only the hosts that actually need it are routed this way -- these services
# charge by the gigabyte, and everything else here is reachable directly.
PROXY_ENV = "WATCH_PROXY"
PROXIED_HOSTS = ("doptcirculars.nic.in", "dopt.gov.in")


def _proxies(url: str) -> dict:
    endpoint = os.environ.get(PROXY_ENV, "").strip()
    if endpoint and any(host in url for host in PROXIED_HOSTS):
        return {"http": endpoint, "https": endpoint}
    return {}

BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 30

# These government servers drop connections, return 500s and time out on a
# regular basis, for seconds at a time, with nothing wrong at our end. One
# failed attempt means nothing; three in a row means something.
ATTEMPTS = 3
RETRY_PAUSE = 4

# How many item identifiers to remember. A week of DoPT orders is a couple of
# dozen and the gazette list holds 100, so this is months of headroom.
MEMORY = 2000

# Several NIC-hosted sites serve an incomplete certificate chain: the
# certificate itself is fine, but the intermediate is missing, so Python
# cannot build a path to a root it trusts. Browsers hide this by fetching the
# missing link themselves. Nothing is sent to these hosts -- no login, no
# secret, only a plain GET of a public page -- so the check is turned off for
# them by name rather than globally.
UNVERIFIED_HOSTS = ("doptcirculars.nic.in", "egazette.gov.in", "egazette.nic.in")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass
class Item:
    key: str            # unique and stable, used to avoid repeat alerts
    source: str         # the label you gave this watch
    title: str
    detail: str = ""
    url: str = ""
    lines: list = field(default_factory=list)   # for page changes


def _verify(url: str) -> bool:
    return not any(host in url for host in UNVERIFIED_HOSTS)


def _fetch(url: str, session=None, referer: str = "") -> BeautifulSoup:
    getter = session or requests
    headers = dict(BROWSER)
    if referer:
        headers["Referer"] = referer
    response = getter.get(url, headers=headers, timeout=TIMEOUT,
                          verify=_verify(url), proxies=_proxies(url) or None)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _try(label: str, action):
    """Run a source reader, giving a flaky server a couple more chances."""
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return action()
        except Exception as exc:                                  # noqa: BLE001
            last = exc
            if attempt < ATTEMPTS:
                print(f"    {label}: attempt {attempt} failed ({str(exc)[:70]}), retrying")
                time.sleep(RETRY_PAUSE)
    raise last


def _cells(row) -> list:
    return [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]


def _table_with(soup, *headings):
    """The table whose header row mentions all of these words."""
    wanted = [h.lower() for h in headings]
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header = " | ".join(_cells(rows[0])).lower()
        if all(word in header for word in wanted):
            return table
    return None


def _tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ----------------------------------------------------------------- uploads

def _dopt(entry: dict) -> list:
    """DoPT EO Division orders: one row per order, with a PDF beside it."""
    soup = _fetch(entry["url"])
    table = _table_with(soup, "order no", "date")
    if table is None:
        # A report with nothing in it simply draws no table -- the promotions
        # report sits empty for weeks at a time. That is an answer, not a
        # fault, so only complain if the page itself never arrived.
        page = soup.get_text(" ", strip=True).lower()
        if "no record exists" in page or "query form for eo division" in page:
            return []
        raise RuntimeError("the orders table was not where it usually is")

    items = []
    for row in table.find_all("tr")[1:]:
        cells = _cells(row)
        if len(cells) < 4:
            continue
        _, order_no, date, officers = cells[0], cells[1], cells[2], cells[3]
        if not order_no:
            continue
        link = row.find("a", href=True)
        items.append(Item(
            # The order number alone repeats across years; pairing it with the
            # order date makes a key that is unique and never changes.
            key=f"dopt:{_tidy(order_no)}|{_tidy(date)}",
            source=entry["name"],
            title=f"{_tidy(order_no)} - {_tidy(date)}",
            detail=_tidy(officers),
            url=link["href"] if link else entry["url"],
        ))
    return items


def _next_page(session, url: str, soup: BeautifulSoup, grid: str, page: int):
    """Click a page number on an ASP.NET grid, which is a form post."""
    form = {i.get("name"): (i.get("value") or "")
            for i in soup.find_all("input") if i.get("name")}
    # The per-row download buttons are image inputs; including one would be
    # read as a click on it and return a PDF instead of the next page.
    for name in [n for n in form if "imgbtndownload" in n]:
        form.pop(name)
    form["__EVENTTARGET"], form["__EVENTARGUMENT"] = grid, f"Page${page}"
    response = session.post(url, data=form, timeout=TIMEOUT, verify=False,
                            headers={**BROWSER, "Referer": url})
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _gazette_rows(table) -> tuple:
    header = _cells(table.find_all("tr")[0])
    index = {name.lower(): i for i, name in enumerate(header)}

    def col(cells, name, default=""):
        position = index.get(name.lower())
        return cells[position] if position is not None and position < len(cells) else default

    rows = [_cells(row) for row in table.find_all("tr")[1:]]
    # The last row is the page-number strip, not a gazette.
    return [cells for cells in rows if len(cells) >= len(header) - 1], col


def _gazette_pdf(gazette_id: str) -> str:
    """The gazette's own PDF, worked out from its ID.

    An ID like CG-DL-E-16092026-276236 carries the publication date and the
    file number, and the site stores every gazette at
    WriteReadData/<year>/<number>.pdf -- checked against real MHA gazettes,
    which open as the right document. Any ID not in that shape falls back to
    the site's front page, because the list page itself cannot be linked to:
    it turns away anyone arriving without the session it hands out.
    """
    match = re.search(r"-\d{4}(\d{4})-(\d+)$", gazette_id)
    if not match:
        return "https://egazette.gov.in/"
    year, number = match.groups()
    return f"https://egazette.gov.in/WriteReadData/{year}/{number}.pdf"


def _egazette(entry: dict, scanned: set) -> list:
    """Extraordinary gazettes, filtered to one ministry.

    Two awkward things about this site. RecentUploads.aspx refuses to open on
    its own -- it hands out a session in the URL path and bounces anything
    that did not come through the front door -- so the front page is opened
    first and its session reused. And the newest 100 gazettes are spread over
    five pages of twenty, with the ministry column mixed together, so one
    ministry's notification is frequently not on page one.

    Pages are walked newest-first and the walk stops at the first page where
    every gazette has been looked at before -- which is why the record of
    what has been looked at covers all ministries, not just the one being
    watched. In the steady state that stops after page one; it only goes
    deeper when the list has moved a long way since the last look.
    """
    session = requests.Session()
    session.headers.update(BROWSER)
    home = session.get("https://egazette.gov.in/", timeout=TIMEOUT, verify=False)
    home.raise_for_status()
    base = home.url.rsplit("/", 1)[0]        # carries the (S(...)) session part

    category = int(entry.get("category", 6))
    url = f"{base}/RecentUploads.aspx?Category={category}"
    wanted = _tidy(entry.get("ministry", "")).lower()
    max_pages = int(entry.get("max_pages", 5))

    soup = _fetch(url, session=session, referer=home.url)
    items, counted = [], 0
    for page in range(1, max_pages + 1):
        table = _table_with(soup, "ministry", "gazette id")
        if table is None:
            if page == 1:
                raise RuntimeError("the gazette list did not load (session refused?)")
            break

        rows, col = _gazette_rows(table)
        counted += len(rows)
        anything_new = False
        for cells in rows:
            gazette_id = _tidy(col(cells, "gazette id"))
            if not gazette_id:
                continue
            if gazette_id not in scanned:
                anything_new = True
                scanned.add(gazette_id)
            if wanted and wanted not in col(cells, "ministry / organization").lower():
                continue
            where = " / ".join(x for x in (col(cells, "department"), col(cells, "office"))
                               if x and x.lower() != "not applicable")
            items.append(Item(
                key=f"gazette:{gazette_id}",
                source=entry["name"],
                title=_tidy(col(cells, "subject")) or gazette_id,
                detail=_tidy(" | ".join(x for x in (
                    where,
                    col(cells, "part & section"),
                    f"issued {col(cells, 'issue date')}, "
                    f"published {col(cells, 'publish date')}",
                    gazette_id,
                ) if x)),
                url=_gazette_pdf(gazette_id),
            ))

        if not anything_new or page == max_pages:
            break
        soup = _next_page(session, url, soup, "gvGazetteList", page + 1)

    print(f"    ({counted} gazettes read across all ministries, {len(scanned)} known)")
    return items


# ------------------------------------------------------------ page changes

def _page_lines(soup) -> list:
    """The part of a page worth comparing, as a list of lines.

    Menus, scripts and the accessibility bar are stripped. Where the page is
    built of tables -- which is what these statistics pages are -- only the
    table rows are compared, so a change is always a change to the numbers.
    """
    for junk in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        junk.decompose()

    tables = soup.find_all("table")
    if tables:
        lines = []
        for table in tables:
            for row in table.find_all("tr"):
                text = _tidy(" | ".join(_cells(row)))
                if text:
                    lines.append(text)
        if lines:
            return lines

    body = soup.find("main") or soup.find("article") or soup.body or soup
    return [_tidy(line) for line in body.get_text("\n", strip=True).split("\n")
            if _tidy(line)]


def _page_change(entry: dict, remembered: dict) -> tuple:
    """Returns (item_or_None, new_snapshot)."""
    soup = _fetch(entry["url"])
    lines = _page_lines(soup)
    if not lines:
        raise RuntimeError("the page came back empty")

    fingerprint = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    snapshot = {"hash": fingerprint, "lines": lines,
                "checked": dt.datetime.now(dt.timezone.utc).isoformat()}

    before = remembered.get(entry["url"]) or {}
    if not before.get("hash"):
        return None, snapshot                      # first sighting: just record
    if before["hash"] == fingerprint:
        return None, {**before, "checked": snapshot["checked"]}

    old, new = before.get("lines") or [], lines
    added = [line for line in new if line not in old]
    removed = [line for line in old if line not in new]
    summary = []
    if added:
        summary.append(f"{len(added)} line(s) added")
    if removed:
        summary.append(f"{len(removed)} line(s) removed")

    item = Item(
        # Keyed on the new content, so the same change is never reported
        # twice, but a further change always is.
        key=f"page:{entry['url']}|{fingerprint[:16]}",
        source=entry["name"],
        title=f"This page changed - {', '.join(summary) or 'content edited'}",
        detail="",
        url=entry["url"],
        lines=[f"+ {line}" for line in added[:12]] + [f"- {line}" for line in removed[:12]],
    )
    return item, snapshot


# ------------------------------------------------------------------ email

BADGE = {"dopt": ("#1d4ed8", "DoPT ORDER"),
         "gazette": ("#7c2d12", "e-GAZETTE"),
         "page": ("#166534", "PAGE CHANGED")}


def build_email(items: list, gap_note: str = "") -> tuple:
    kinds = {item.key.split(":", 1)[0] for item in items}
    if len(items) == 1:
        subject = f"{items[0].source}: {items[0].title}"[:120]
    else:
        names = {"dopt": "DoPT", "gazette": "gazette",
                 "page": "page change"}
        subject = (f"{len(items)} government updates: "
                   + ", ".join(sorted(names.get(k, k) for k in kinds)))

    blocks = []
    for item in items:
        colour, label = BADGE.get(item.key.split(":", 1)[0], ("#374151", "UPDATE"))
        extra = ""
        if item.lines:
            rows = "".join(
                f'<div style="font:400 13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;'
                f'color:{"#166534" if line.startswith("+") else "#b91c1c"};'
                f'padding:1px 0;">{html.escape(line)}</div>'
                for line in item.lines)
            extra = (f'<div style="background:#f6f7f9;border-radius:4px;'
                     f'padding:10px 12px;margin-top:10px;overflow-x:auto;">{rows}</div>')
        blocks.append(f"""
        <div style="border:1px solid #e5e7eb;border-radius:6px;padding:16px 18px;margin-bottom:14px;">
          <div style="margin-bottom:8px;">
            <span style="background:{colour};color:#fff;font:600 10px/1 -apple-system,Segoe UI,sans-serif;
                         letter-spacing:.08em;padding:4px 7px;border-radius:3px;">{label}</span>
            <span style="color:#6b7280;font:400 12px/1 -apple-system,Segoe UI,sans-serif;
                         margin-left:8px;">{html.escape(item.source)}</span>
          </div>
          <div style="font:600 16px/1.4 -apple-system,Segoe UI,sans-serif;color:#111827;">
            {html.escape(item.title)}</div>
          {f'<div style="font:400 14px/1.55 -apple-system,Segoe UI,sans-serif;color:#4b5563;margin-top:6px;">{html.escape(item.detail)}</div>' if item.detail else ''}
          {extra}
          <div style="margin-top:12px;">
            <a href="{html.escape(item.url)}" style="font:500 13px/1 -apple-system,Segoe UI,sans-serif;
               color:#1d4ed8;text-decoration:none;">Open the source &rarr;</a>
          </div>
        </div>""")

    body = f"""<div style="max-width:640px;margin:0 auto;padding:24px 20px;background:#fff;">
      <div style="font:600 11px/1 -apple-system,Segoe UI,sans-serif;letter-spacing:.12em;
                  color:#6b7280;text-transform:uppercase;margin-bottom:16px;">Government watch</div>
      {''.join(blocks)}
      <div style="color:#9ca3af;font:400 12px/1.5 -apple-system,Segoe UI,sans-serif;
                  margin-top:20px;border-top:1px solid #e5e7eb;padding-top:12px;">
        Every change to these pages is reported. Nothing here is filtered or scored.
        {gap_note}
      </div>
    </div>"""
    return subject, body


# -------------------------------------------------------------------- run

def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _due(entry: dict, remembered: dict, every_minutes: int) -> bool:
    """Pages that change once a month do not need looking at every 15 minutes."""
    if every_minutes <= 0:
        return True
    last = (remembered.get(entry["url"]) or {}).get("checked")
    if not last:
        return True
    try:
        when = dt.datetime.fromisoformat(last)
    except ValueError:
        return True
    age = dt.datetime.now(dt.timezone.utc) - when
    return age >= dt.timedelta(minutes=every_minutes)


def run(dry_run: bool) -> int:
    cfg = (yaml.safe_load(WATCHLIST.read_text()) or {}).get("watch") or {}
    if not cfg:
        print("No 'watch:' section in watchlist.yml -- nothing to do.")
        return 0

    at_home = os.environ.get(HOME_ENV, "").strip() == "1"
    via_proxy = bool(os.environ.get(PROXY_ENV, "").strip())
    # Whether THIS run can see the sites that refuse hosting networks.
    direct = at_home or via_proxy
    scope = "home" if at_home else "server"
    print("Reading the sources that need a consumer connection"
          + (" (through the proxy)." if via_proxy and not at_home else ".")
          if direct else "Reading the sources any server can reach.")

    state = _load_state()
    memory = state.setdefault("watch", {})
    seen = memory.setdefault("seen", [])
    pages = memory.setdefault("pages", {})
    # Every gazette identifier ever looked at, whatever the ministry.
    # Without this the walk through the list could never stop early.
    scanned = set(memory.get("scanned") or [])
    first_ever = not seen and not pages

    # How long since this half last ran. The home half lives on a machine
    # that sleeps, so a gap is normal -- but a gap worth knowing about is
    # worth saying out loud rather than leaving you to assume it ran.
    now = dt.datetime.now(dt.timezone.utc)
    previous = memory.get(f"last_{scope}_run") or ""
    gap_note = ""
    try:
        hours = (now - dt.datetime.fromisoformat(previous)).total_seconds() / 3600
        if hours >= 6:
            gap_note = (f"This watch had not run for {hours:.0f} hours before now. "
                        f"The DoPT reports being read list a rolling thirty days, "
                        f"so every order published during the gap is still picked up.")
            print(f"  (first check in {hours:.0f} hours)")
    except ValueError:
        pass

    found, failed = [], []

    already = set(seen)
    for entry in cfg.get("uploads") or []:
        if bool(entry.get("home_only")) and not direct:
            continue
        if not entry.get("home_only") and at_home:
            continue
        try:
            items = _try(entry["name"],
                         lambda: _egazette(entry, scanned) if entry.get("category")
                         else _dopt(entry))
            print(f"  {entry['name']}: {len(items)} row(s) on the page")
            found.extend(items)
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ! {entry['name']} failed: {exc}")
            failed.append(entry["name"])

    page_cfg = cfg.get("pages") or {}
    every = int(page_cfg.get("every_minutes", 120))
    for entry in page_cfg.get("urls") or []:
        if bool(entry.get("home_only")) and not direct:
            continue
        if not entry.get("home_only") and at_home:
            continue
        if not _due(entry, pages, every):
            print(f"  {entry['name']}: checked recently, skipping")
            continue
        try:
            item, snapshot = _try(entry["name"], lambda: _page_change(entry, pages))
            pages[entry["url"]] = snapshot
            print(f"  {entry['name']}: {'CHANGED' if item else 'unchanged'}")
            if item:
                found.append(item)
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ! {entry['name']} failed: {exc}")
            failed.append(entry["name"])

    # "already" is the memory as it stood before this run started.
    #
    # The DoPT reports deliberately overlap -- today's order also sits on the
    # thirty-day list -- so the same order arrives more than once in a single
    # run. Because an order is keyed by its number and date, the second copy
    # is recognised as the same order and dropped here rather than emailed.
    fresh, counted = [], set(already)
    for item in found:
        if item.key in counted:
            continue
        counted.add(item.key)
        fresh.append(item)

    if first_ever:
        # Everything on these pages is "new" the first time they are read.
        # Record where they stand and stay quiet, exactly as the news tracker
        # does, so the first run cannot arrive as one unreadable email.
        print(f"First run: noting {len(fresh)} existing item(s), sending nothing.")
        fresh = []

    if fresh:
        subject, body = build_email(fresh, gap_note)
        if dry_run:
            print(f"\n(dry run) would have emailed: {subject}")
            for item in fresh:
                print(f"    - [{item.source}] {item.title}")
        else:
            email_out.send(subject, body, recipient_env=RECIPIENT_ENV,
                           sender_name="Government Watch")
            print(f"\nEmailed: {subject}")
    else:
        print("\nNothing new on the watched pages.")

    if not dry_run:
        # Remember every identifier seen this run, not just the new ones, so
        # a row that scrolls off a page and comes back is not re-reported.
        for item in found:
            if item.key not in already:
                already.add(item.key)
                seen.append(item.key)
        memory["seen"] = seen[-MEMORY:]
        memory["pages"] = pages
        memory["scanned"] = sorted(scanned)[-MEMORY:]
        memory[f"last_{scope}_run"] = now.isoformat()
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    # A source being down is normal for these sites and is NOT a failure of
    # this program: it is named in the log and the run stays green. Exiting
    # non-zero here turned every transient government 500 into a red run and
    # an email from GitHub, which is noise about something nobody can fix.
    if failed:
        print(f"\nsources unavailable this run (will retry next time): {', '.join(failed)}")
    return 0


def test_email() -> int:
    """Send one harmless email, to prove the settings work.

    Without this the first proof that email is working correctly is the
    first real order, which may be days away -- and a wrong password fails
    silently until then.
    """
    item = Item(
        key="dopt:TEST/2026-EO(SM-I)|01/01/2026",
        source="Settings test",
        title="This is a test - the government watch can send email",
        detail=("If this has arrived, the sending account, the app password "
                "and the recipient list are all correct. Nothing was read "
                "from any government site to produce it."),
        url="https://doptcirculars.nic.in/Report.aspx",
    )
    subject, body = build_email([item])
    email_out.send(subject, body, recipient_env=RECIPIENT_ENV,
                   sender_name="Government Watch")
    print("Sent. Check the inbox -- every address on the list should have it.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be sent, send nothing, remember nothing")
    parser.add_argument("--test-email", action="store_true",
                        help="send one test email and stop, to check the settings")
    args = parser.parse_args()
    if args.test_email:
        return test_email()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
