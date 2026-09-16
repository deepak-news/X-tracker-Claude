"""The national desk: official news with broad public value.

A third prong, separate from the other two. The news tracker (main.py) looks
for stories on one reporter's beat. The government watch (watch.py) reports
every new row on a handful of pages, unfiltered. This one reads the whole
stream of official India -- every PIB release from every ministry, every
extraordinary gazette, the RBI and SEBI -- and asks the AI which items a
general audience would actually care about.

The alerts go to the head of social media (SOCIAL_MAIL_TO), so the bar is
"would a large part of the country want to know this", not "would a
specialist". Official sources only, by design: no Google News.

    python -m tracker.national              check, score, email
    python -m tracker.national --dry-run    score and print, send nothing, save nothing
    python -m tracker.national --test-email one test email to SOCIAL_MAIL_TO
"""

import argparse
import datetime as dt
import html
import io
import json
import os
import pathlib
import re
import sys
import time

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from pypdf import PdfReader

from . import email_out, judge, sources, watch
from .main import dead_models, record_dead_models
from .sources import Post

ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "watchlist.yml"
STATE = ROOT / "state.json"

RECIPIENT_ENV = "SOCIAL_MAIL_TO"
SENDER_NAME = "National Desk"

# Identifiers remembered. About 250 official items a day pass through here,
# so this is roughly a fortnight -- longer than anything stays on the pages
# that carry no dates (PIB shows one day, the gazette about two).
MEMORY = 3000

# Items per AI request. These are headlines and short summaries, not whole
# articles, so far more fit in one request than the news tracker's posts.
CHUNK = 40
MAX_CHARS = 1000

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc
PIB_URL = "https://www.pib.gov.in/allRel.aspx?reg=3&lang=1"


# ---------------------------------------------------------------- sources

def _blank(**fields) -> Post:
    return Post(likes=0, reposts=0, is_reply=False, is_repost=False, **fields)


def _pib_rows(soup: BeautifulSoup) -> list:
    """Every release on a PIB listing, labelled with the ministry above it."""
    now = dt.datetime.now(UTC).isoformat()
    posts, found = [], set()
    for link in soup.select('a[href*="PressReleaseDetail.aspx"]'):
        title = " ".join(link.get_text(" ", strip=True).split())
        match = re.search(r"PRID=(\d+)", link.get("href", ""))
        if not title or not match or match.group(1) in found:
            continue
        prid = match.group(1)
        found.add(prid)
        # The page lists releases under one heading per ministry.
        heading = link.find_previous("h3")
        ministry = " ".join(heading.get_text(" ", strip=True).split()) if heading else ""
        ministry = ministry or "Government of India"
        posts.append(_blank(
            id=f"pib:{prid}",
            handle=f"PIB {ministry}",
            text=f"{ministry}: {title}",
            url=f"https://www.pib.gov.in/PressReleasePage.aspx?PRID={prid}",
            created_at=now,
        ))
    return posts


def _pib() -> list:
    """Today's releases from every ministry at once.

    The English listing opens on today's date with "All Ministry" selected,
    so a plain visit is enough. In the first hour after midnight the page
    has already rolled over to the new day, so yesterday's list is asked for
    as well -- otherwise anything released late last night would be skipped.
    """
    session = requests.Session()
    session.headers["User-Agent"] = sources.UA
    page = session.get(PIB_URL, timeout=sources.TIMEOUT)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")
    posts = _pib_rows(soup)

    now = dt.datetime.now(IST)
    if now.hour == 0:
        yesterday = now - dt.timedelta(days=1)
        fields = {i.get("name"): i.get("value", "")
                  for i in soup.select("input[type=hidden]") if i.get("name")}
        fields.update({
            "ctl00$ContentPlaceHolder1$ddlMinistry": "0",
            "ctl00$ContentPlaceHolder1$ddlday": str(yesterday.day),
            "ctl00$ContentPlaceHolder1$ddlMonth": str(yesterday.month),
            "ctl00$ContentPlaceHolder1$ddlYear": str(yesterday.year),
            "__EVENTTARGET": "ctl00$ContentPlaceHolder1$ddlday",
            "__EVENTARGUMENT": "",
        })
        response = session.post(PIB_URL, data=fields, timeout=sources.TIMEOUT)
        response.raise_for_status()
        posts += _pib_rows(BeautifulSoup(response.text, "html.parser"))
    return posts


def _gazette(scanned: set) -> list:
    """Every extraordinary gazette, whichever ministry issued it."""
    entry = {"name": "Gazette of India", "category": 6, "ministry": ""}
    now = dt.datetime.now(UTC).isoformat()
    posts = []
    for item in watch._egazette(entry, scanned):
        # The detail reads "Ministry / Department | Part | dates | ID".
        ministry = item.detail.split(" | ")[0].split(" / ")[0].strip()
        if not ministry or ministry.lower().startswith(("part", "issued")):
            ministry = "Government of India"
        posts.append(_blank(
            id=item.key,
            handle=f"Gazette {ministry}",
            text=f"Gazette of India (Extraordinary), {ministry}: {item.title}. {item.detail}",
            url=item.url,
            created_at=now,
        ))
    return posts


# A gazette's subject line is often just "Publication of notification". The
# notification itself -- the English half of the PDF -- says what it does.
GAZETTE_CHARS = 600
GAZETTE_BUDGET_SECONDS = 150
_ENGLISH = re.compile(r"[\x20-\x7E\u2013\u2014\u2018\u2019\u201c\u201d]+")
_OPENING = re.compile(r"\b(NOTIFICATION|ORDER|RESOLUTION|CORRIGENDUM|NOTICE)\b")


def _gazette_text(url: str) -> str:
    """The English opening of a gazette, or "" if it cannot be read."""
    if not url.lower().endswith(".pdf"):
        return ""
    try:
        response = requests.get(url, timeout=40, verify=False,
                                headers={"User-Agent": sources.UA})
        response.raise_for_status()
        reader = PdfReader(io.BytesIO(response.content))
        raw = " ".join((page.extract_text() or "") for page in reader.pages[:3])
    except Exception:                                             # noqa: BLE001
        return ""
    # Gazettes print Hindi first, then English. Keep the English words only,
    # and start where the notification itself starts.
    english = " ".join(word for word in raw.split() if _ENGLISH.fullmatch(word))
    opening = _OPENING.search(english)
    return english[opening.start():][:GAZETTE_CHARS] if opening else english[:GAZETTE_CHARS]


def read_gazettes(posts: list) -> None:
    """Add each new gazette's own wording to what the AI is shown.

    Only new items reach this, so a normal run reads a handful of PDFs. The
    time budget stops a slow gazette server from eating the whole run; any
    gazette not read in time is still judged on its subject line.
    """
    started, read = time.monotonic(), 0
    for post in posts:
        if not post.handle.startswith("Gazette "):
            continue
        if time.monotonic() - started > GAZETTE_BUDGET_SECONDS:
            print("  (gazette reading stopped at the time limit; the rest are judged on their subject)")
            break
        text = _gazette_text(post.url)
        if len(text) > 80:
            post.text = f"{post.text} TEXT: {text}"
            read += 1
    if read:
        print(f"  read {read} gazette notification(s) in full")


_LOOSE_DATE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*,?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")


def _when(entry) -> dt.datetime:
    """A feed item's date.

    Indian official feeds write dates feedparser cannot read -- SEBI's
    "14 Sep, 2026", RBI's times with no time zone -- so those are read here
    as Indian time. A date with no time at all is taken as the END of that
    day: guessing too early would make a late-evening release look hours
    old and get it thrown away unread.
    """
    raw = entry.get("published") or entry.get("updated") or ""
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if stamp and re.search(r"(GMT|UTC|Z|[+-]\d{2}:?\d{2})\s*$", raw) and re.search(r"\d:\d\d", raw):
        return dt.datetime(*stamp[:6], tzinfo=UTC)     # states its own zone; trust it
    match = _LOOSE_DATE.search(raw)
    if match:
        day, month, year, hour, minute = match.groups()
        try:
            when = dt.datetime.strptime(f"{day} {month} {year}", "%d %b %Y")
            when = (when.replace(hour=int(hour), minute=int(minute)) if hour
                    else when.replace(hour=23, minute=59))
            return min(when.replace(tzinfo=IST).astimezone(UTC), dt.datetime.now(UTC))
        except ValueError:
            pass
    if stamp:
        return dt.datetime(*stamp[:6], tzinfo=UTC)
    return dt.datetime.now(UTC)


def _feed(entry: dict) -> list:
    response = requests.get(entry["url"], timeout=sources.TIMEOUT,
                            headers={"User-Agent": sources.UA})
    response.raise_for_status()
    parsed = feedparser.parse(response.content)

    posts = []
    for item in parsed.entries[:sources.MAX_ITEMS]:
        title = " ".join((item.get("title") or "").split())
        link = item.get("link") or ""
        if not title:
            continue
        summary = sources._plain(item.get("summary") or "")
        if summary.lower().startswith(title.lower()[:40]):
            summary = ""
        text = f"{entry['name']}: {title}" + (f". {summary[:400]}" if summary else "")
        posts.append(_blank(
            id=f"feed:{link or title}",
            handle=entry["name"],
            text=text,
            url=link,
            created_at=_when(item).isoformat(),
        ))
    return posts


def collect(cfg: dict, scanned: set) -> tuple:
    """Returns (posts, names of sources that failed)."""
    posts, failed = [], []

    readers = []
    if cfg.get("pib", True):
        readers.append(("PIB, all ministries", _pib))
    if cfg.get("gazette", True):
        readers.append(("Gazette of India (Extraordinary)", lambda: _gazette(scanned)))
    for entry in cfg.get("feeds") or []:
        readers.append((entry["name"], lambda entry=entry: _feed(entry)))

    for name, read in readers:
        try:
            found = watch._try(name, read)
            print(f"  {name}: {len(found)} item(s)")
            posts.extend(found)
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ! {name} failed: {str(exc)[:160]}")
            failed.append(name)
    return posts, failed


# ---------------------------------------------------------------- scoring

PROMPT = """You screen India's official news for the HEAD OF SOCIAL MEDIA at a
national news agency. They decide what goes out on the agency's social
accounts to a general audience of millions. They are busy: every item you
pass must be worth their attention, and missing a big story is a failure.

WHAT COUNTS AS BROAD NEWS VALUE:
{rubric}

Score each item 0-10:

  0-3  routine administration: auctions and their results, tenders,
       recovery and release orders, routine postings and transfers,
       meetings held, visits and events attended, greetings, quotes,
       anniversaries, awareness drives, minor MoUs, procedural amendments
  4-6  real but niche: matters mainly to one sector, one state, or to
       specialists, or is a follow-up with nothing new for the public
  7-8  clear general news value: a decision, rule or scheme that affects
       large numbers of people, their money, safety, rights or daily life;
       a major appointment or removal; a significant national security,
       diplomatic, economic or legal development
  9-10 a major national story that would lead a news bulletin

{already_sent}Rules:
- Judge the item itself, not the importance of the office that issued it.
  The Prime Minister's office issues routine items too.
- Plain-language impact matters more than bureaucratic weight: a small
  change to a tax, price, benefit or deadline that touches millions can
  outrank a long notification that touches almost no one.
- If several items describe the SAME underlying event, give the fullest or
  most authoritative one its real score and set "dupe_of" to its number on
  all the others.

For each item write a "headline": at most 14 words, plain English, saying
what actually happened, the way it could open a social media post. No
jargon, no reference numbers.

Return ONLY a JSON array, one object per item, same order:
[{{"i": <item number>, "score": <0-10>, "headline": "<max 14 words>", "why": "<one sentence on why the public would care>", "dupe_of": <item number or null>, "seen_before": <true or false>}}]

ITEMS:
{items}
"""

ALREADY_SENT = """ALREADY SENT TO THEM IN THE LAST 48 HOURS:
{sent}

- Set "seen_before": true for any item telling the SAME story as one of the
  above, however it is worded. But a genuine development -- a decision
  taken, a date fixed, a figure announced -- is fresh news: leave it false.

"""


def _prompt(batch: list, rubric: str, sent: list) -> str:
    listing = "\n\n".join(f"[{i}] {p.text[:MAX_CHARS]}" for i, p in enumerate(batch))
    block = ALREADY_SENT.format(sent="\n".join(f"- {h}" for h in sent)) if sent else ""
    return PROMPT.format(rubric=rubric, items=listing, already_sent=block)


def score(posts: list, rubric: str, state: dict, memory: dict) -> tuple:
    """Returns (rows, settled_ids).

    rows    = [(post, score, headline, why)] for every item actually judged
    settled = ids of every item the AI dealt with, including the ones it
              called duplicates or old news. Anything NOT settled -- a batch
              that failed, an item the model skipped -- stays unseen and is
              tried again next run rather than being lost.
    """
    rows, settled = [], set()
    dead = dead_models(state)
    sent = judge.recent_headlines(memory)
    batches = [posts[i:i + CHUNK] for i in range(0, len(posts), CHUNK)]

    for n, batch in enumerate(batches, 1):
        print(f"  batch {n} of {len(batches)} ({len(batch)} items)")
        try:
            entries, newly_dead = judge.ask_json(_prompt(batch, rubric, sent), dead)
        except judge.AllModelsExhausted as exc:
            record_dead_models(state, exc.newly_dead)
            print(f"  ! no AI model is available: {exc}. The rest wait for next run.")
            break
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ! batch {n} failed ({str(exc)[:120]}); it will be tried again next run")
            continue
        record_dead_models(state, newly_dead)
        dead |= newly_dead

        for entry in entries:
            try:
                idx = int(entry.get("i", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(batch):
                continue
            post = batch[idx]
            settled.add(post.id)
            if entry.get("seen_before") is True or entry.get("dupe_of") is not None:
                continue
            try:
                value = float(entry.get("score", 0))
            except (TypeError, ValueError):
                value = 0.0
            rows.append((post, value,
                         str(entry.get("headline") or "").strip(),
                         str(entry.get("why") or "").strip()))

        skipped = [p for p in batch if p.id not in settled]
        if skipped:
            print(f"  ! the model skipped {len(skipped)} item(s); they will be tried again next run")
    return rows, settled


# ------------------------------------------------------------------ email

BADGES = (
    ("PIB ", "#1e3a8a", "PIB"),
    ("Gazette ", "#7c2d12", "GAZETTE"),
    ("RBI", "#065f46", "RBI"),
    ("SEBI", "#5b21b6", "SEBI"),
)


def _badge(post) -> tuple:
    for prefix, colour, label in BADGES:
        if post.handle.startswith(prefix):
            return colour, label
    return "#374151", post.handle.upper()[:18]


def _issuer(post) -> str:
    for prefix in ("PIB ", "Gazette "):
        if post.handle.startswith(prefix):
            return post.handle[len(prefix):]
    return post.handle


def _original(post) -> str:
    """The release's own wording, without the label this tool put in front."""
    text = post.text.split(": ", 1)[-1]
    if post.handle.startswith("Gazette "):
        text = text.split(" | ")[0]
    return text[:260]


def build_email(rows: list) -> tuple:
    rows = sorted(rows, key=lambda r: -r[1])
    lead = rows[0][2] or _original(rows[0][0])
    subject = lead if len(rows) == 1 else f"{len(rows)} national stories: {lead}"

    font = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"
    cards = []
    for post, value, headline, why in rows:
        colour, label = _badge(post)
        major = (f'<span style="background:#b91c1c;color:#fff;font:700 10px/1 {font};'
                 f'letter-spacing:.08em;padding:4px 7px;border-radius:3px;margin-left:6px;">'
                 f'MAJOR</span>') if value >= 9 else ""
        action = "Open the gazette (PDF)" if post.url.lower().endswith(".pdf") else "Read the release"
        cards.append(f"""
        <div style="border:1px solid #e5e7eb;border-radius:6px;padding:16px 18px;margin-bottom:14px;">
          <div style="margin-bottom:10px;">
            <span style="background:{colour};color:#fff;font:600 10px/1 {font};letter-spacing:.08em;
                         padding:4px 7px;border-radius:3px;">{label}</span>{major}
            <span style="color:#6b7280;font:400 12px/1 {font};margin-left:8px;">{html.escape(_issuer(post))}</span>
          </div>
          <div style="font:600 17px/1.35 {font};color:#111827;">{html.escape(headline or _original(post))}</div>
          <div style="font:400 13px/1.5 {font};color:#6b7280;margin-top:8px;">
            As issued: {html.escape(_original(post))}</div>
          <div style="margin-top:12px;">
            <a href="{html.escape(post.url)}" style="font:500 13px/1 {font};color:#1d4ed8;
               text-decoration:none;">{action} &rarr;</a>
          </div>
        </div>""")

    body = f"""<div style="max-width:640px;margin:0 auto;padding:24px 20px;background:#fff;">
      <div style="font:600 11px/1 {font};letter-spacing:.12em;color:#6b7280;
                  text-transform:uppercase;margin-bottom:16px;">National desk</div>
      {''.join(cards)}
      <div style="color:#9ca3af;font:400 12px/1.5 {font};margin-top:20px;
                  border-top:1px solid #e5e7eb;padding-top:12px;">
        Screened from PIB (every ministry), the Gazette of India (Extraordinary),
        the RBI and SEBI. Only items judged to have broad public news value are
        sent; routine releases are left out. Headlines are machine-written from
        the official text -- check the original before publishing.
      </div>
    </div>"""
    return subject[:150], body


# -------------------------------------------------------------------- run

def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def run(dry_run: bool) -> int:
    cfg = (yaml.safe_load(WATCHLIST.read_text()) or {}).get("national") or {}
    if not cfg:
        print("No 'national:' section in watchlist.yml -- nothing to do.")
        return 0
    if not dry_run and not os.environ.get(RECIPIENT_ENV, "").strip():
        print(f"No {RECIPIENT_ENV} secret set yet, so there is nobody to send to. "
              f"Skipping -- add the secret to switch this on.")
        return 0

    rubric = (cfg.get("newsworthy") or "").strip()
    threshold = float(cfg.get("threshold", 7))
    max_age = float(cfg.get("max_age_hours", 12))

    state = _load_state()
    memory = state.setdefault("national", {})
    seen_list = memory.setdefault("seen", [])
    seen = set(seen_list)
    scanned = set(memory.get("gazette_scanned") or [])
    first_ever = not seen_list

    print("Reading official sources for the national desk...")
    posts, failed = collect(cfg, scanned)

    fresh, settled, ids = [], set(), set()
    cutoff = dt.datetime.now(UTC) - dt.timedelta(hours=max_age)
    stale = 0
    for post in posts:
        if post.id in seen or post.id in ids:
            continue
        ids.add(post.id)
        if dt.datetime.fromisoformat(post.created_at) < cutoff:
            settled.add(post.id)       # older than anyone wants; never score it
            stale += 1
            continue
        fresh.append(post)
    if stale:
        print(f"  {stale} item(s) older than {max_age:g}h ignored")

    if first_ever and not dry_run:
        # Everything is "new" the first time. Note where things stand and
        # stay quiet, or the first email would be a day's worth of releases.
        print(f"First run: noting {len(ids)} existing item(s), sending nothing.")
        settled |= ids
        fresh = []
    elif first_ever:
        print("First run, dry: scoring what is listed now as a preview. "
              "A real first run sends nothing.")

    rows = []
    if fresh:
        # No folding by similar wording here, unlike the news tracker: official
        # documents share so much legal boilerplate that it merged dozens of
        # different gazettes into one. Each item already has its own unique
        # ID; true duplicates -- the same decision in a PIB release and a
        # gazette -- are left to the AI, which is shown both side by side.
        sent_urls = {e.get("url") for e in memory.get("alerted", []) if e.get("url")}
        repeats = {p.id for p in fresh if p.url and p.url in sent_urls}
        settled |= repeats
        fresh = [p for p in fresh if p.id not in repeats]
        print(f"{len(fresh)} new item(s) to judge")
        if fresh:
            read_gazettes(fresh)
            rows, judged = score(fresh, rubric, state, memory)
            settled |= judged

    picks = [r for r in rows if r[1] >= threshold]
    for post, value, headline, _ in sorted(rows, key=lambda r: -r[1]):
        mark = "SEND" if value >= threshold else "skip"
        print(f"  [{mark}] {value:.0f}/10 {_badge(post)[1]} {_issuer(post)[:30]}: "
              f"{headline or _original(post)[:80]}")

    if picks:
        subject, body = build_email(picks)
        if dry_run:
            print(f"\n(dry run) would have emailed: {subject}")
        else:
            try:
                email_out.send(subject, body, recipient_env=RECIPIENT_ENV,
                               sender_name=SENDER_NAME)
                print(f"\nEmailed: {subject}")
                judge.remember_alerted(memory, picks)
            except Exception as exc:                              # noqa: BLE001
                # Not remembered as seen, so they go out on the next run.
                print(f"\n! the email could not be sent ({exc}); retrying next run")
                settled -= {r[0].id for r in picks}
    else:
        print("\nNothing with broad news value this run.")

    if failed:
        print(f"sources unavailable this run (will retry next time): {', '.join(failed)}")

    if not dry_run:
        seen_list.extend(i for i in settled if i not in seen)
        memory["seen"] = seen_list[-MEMORY:]
        memory["gazette_scanned"] = sorted(scanned)[-2000:]
        memory["last_run"] = dt.datetime.now(UTC).isoformat()
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    # A government site or the AI being down is not a failure of this
    # program, and must not turn the run red and email anyone about it.
    return 0


def test_email() -> int:
    if not os.environ.get(RECIPIENT_ENV, "").strip():
        print(f"No {RECIPIENT_ENV} secret set yet -- no test email for the national desk.")
        return 0
    post = _blank(id="test", handle="PIB Cabinet",
                  text="Cabinet: This is a test release from the national desk",
                  url="https://www.pib.gov.in/", created_at=dt.datetime.now(UTC).isoformat())
    subject, body = build_email([(post, 8, "Test: the national desk can send email",
                                  "If this arrived, the settings for this prong are correct.")])
    email_out.send(subject, body, recipient_env=RECIPIENT_ENV, sender_name=SENDER_NAME)
    print("Sent. Check the inbox of every address on the list.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="score and print, send nothing, save nothing")
    parser.add_argument("--test-email", action="store_true",
                        help="send one test email to SOCIAL_MAIL_TO and stop")
    args = parser.parse_args()
    if args.test_email:
        return test_email()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
