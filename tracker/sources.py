"""Reads news from primary sources instead of X.

WHY THIS EXISTS: X blocks every data-centre IP range, so a server can no
longer read it at all. These sources can be read from anywhere, forever,
without a login -- and for company and government news they are the
*original* announcement rather than a post about it, so they are usually
earlier than the tweet as well as more authoritative.

Three kinds of source, cheapest and most authoritative first:
  1. BSE filings   -- what an Indian listed company formally told the
                      exchange. This is the primary document.
  2. Newsroom RSS  -- what a global company published itself.
  3. Google News   -- everything else, including what individuals said,
                      once a publication has reported it.
"""

import datetime as dt
import hashlib
import html
import io
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests
from pypdf import PdfReader
from bs4 import BeautifulSoup

from . import krutidev, regulators


@dataclass
class Post:
    id: str
    handle: str
    text: str
    url: str
    created_at: str
    likes: int
    reposts: int
    is_reply: bool
    is_repost: bool
    origin: str = ""      # the outlet or body it actually came from


class XUnavailable(RuntimeError):
    """Every source failed -- the problem is upstream, not the config."""

TIMEOUT = 25
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

# Exchange filings that are legally required but never news. Dropping them
# here keeps them away from the AI, which has a small daily budget.
BSE_SKIP = {
    "book closure", "newspaper publication", "trading window",
    "compliance certificate", "share certificate", "loss of share certificates",
    "duplicate share certificate", "record date", "sub-division",
    "certificate under reg. 74 (5)", "statement of investor complaints",
    "shareholding pattern", "corporate governance report",
}

# Some feeds carry thousands of entries. Only the recent end can possibly
# be news, and the age filter would discard the rest anyway.
MAX_ITEMS = 60

# Syndicated market-research and SEO spam clears every other filter but is
# never a story. Cheaper to drop here than to spend AI budget on it.
NOISE = (
    "market size", "market share", "market report", "market research",
    "market outlook", "market analysis", "market forecast", "cagr",
    "forecast to 20", "market.us", "industry report", "research report",
    "market to reach", "market growth", "sample report", "openpr",
    "contest", "webinar", "sponsored", "press release distribution",
)


def _is_noise(text: str) -> bool:
    low = text.lower()
    return any(term in low for term in NOISE)


def _post_id(when: dt.datetime, key: str) -> str:
    """A sortable integer id, because progress is tracked as a high-water mark.

    Seconds alone would collide when a source publishes twice in the same
    second, and a collision means a dropped story -- so the item's own
    identity contributes the last three digits.
    """
    tail = int(hashlib.sha1(key.encode()).hexdigest()[:6], 16) % 1000
    return str(int(when.timestamp()) * 1000 + tail)


def _make(when: dt.datetime, label: str, text: str, url: str, key: str,
          origin: str = "") -> Post:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return Post(
        id=_post_id(when, key),
        handle=label,
        text=" ".join(text.split())[:1200],
        url=url,
        created_at=when.isoformat(),
        likes=0,
        reposts=0,
        is_reply=False,
        is_repost=False,
        origin=origin,
    )


# How far to trust a source, and therefore what order things are read in.
# A filing is the document itself; a ministry release is the government
# speaking; a newsroom post is the company speaking; a news article is
# somebody reporting on one of those three.
TIER_FILING, TIER_GOVERNMENT, TIER_COMPANY, TIER_NEWS = 0, 1, 2, 3

TIER_NAMES = {
    TIER_FILING: "Exchange filings",
    TIER_GOVERNMENT: "Government releases",
    TIER_COMPANY: "Company announcements",
    TIER_NEWS: "Reported in the news",
}

KIND_NAMES = {
    TIER_FILING: "BSE FILING",
    TIER_GOVERNMENT: "PIB RELEASE",
    TIER_COMPANY: "COMPANY NEWSROOM",
    TIER_NEWS: "NEWS",
}


def tier(handle: str) -> int:
    label = handle.lower()
    if label.startswith("bse "):
        return TIER_FILING
    if label.startswith(("pib ", "reg ")):
        return TIER_GOVERNMENT
    return TIER_NEWS if label.startswith("news:") else TIER_COMPANY


def describe(post) -> tuple[str, str]:
    """(what kind of source, which one) -- for the label in the email."""
    level = tier(post.handle)
    name = post.handle.split(" ", 1)[-1] if level in (TIER_FILING, TIER_GOVERNMENT) \
        else post.handle.split(":", 1)[-1].strip() if level == TIER_NEWS else post.handle
    if level == TIER_NEWS and post.origin:
        name = post.origin          # the outlet that actually ran it
    if post.handle.lower().startswith("reg "):
        return "REGULATOR", name    # CCI, CERT-In, DGFT -- not PIB
    return KIND_NAMES[level], name


def _bse(companies: list[dict], days: int) -> list[Post]:
    today = dt.datetime.now(dt.timezone.utc)
    start = (today - dt.timedelta(days=days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    posts = []
    for company in companies:
        code, name = str(company["code"]), company["name"]
        params = {
            "pageno": 1, "strCat": -1, "strPrevDate": start, "strScrip": code,
            "strSearch": "P", "strToDate": end, "strType": "C", "subcategory": -1,
        }
        response = requests.get(
            BSE_API, params=params, timeout=TIMEOUT,
            headers={"User-Agent": UA, "Referer": "https://www.bseindia.com/"},
        )
        response.raise_for_status()
        for row in response.json().get("Table", []):
            subcat = (row.get("SUBCATNAME") or "").strip().lower()
            if subcat in BSE_SKIP:
                continue
            when = row.get("NEWS_DT") or row.get("DT_TM") or ""
            try:
                stamp = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                continue
            # The headline is often just "Enclosed" -- the real subject line
            # lives in NEWSSUB, so prefer whichever actually says something.
            headline = (row.get("HEADLINE") or "").strip()
            subject = (row.get("NEWSSUB") or "").strip()
            detail = subject if len(subject) > len(headline) else headline
            if not detail:
                continue
            category = " / ".join(x for x in [row.get("CATEGORYNAME"), row.get("SUBCATNAME")] if x)
            attachment = (row.get("ATTACHMENTNAME") or "").strip()
            url = (BSE_ATTACH + attachment) if attachment else \
                  f"https://www.bseindia.com/stock-share-price/x/x/{code}/corp-announcements/"
            posts.append(_make(
                stamp, f"BSE {name}",
                f"{name} filed with the exchange: {detail} [{category}]",
                url, row.get("NEWSID") or detail,
            ))
    return posts


def _plain(markup: str) -> str:
    """Feed summaries often arrive as HTML; the AI only needs the words."""
    return html.unescape(re.sub(r"<[^>]+>", " ", markup)).strip()


PIB_URL = "https://www.pib.gov.in/allRel.aspx?reg=3&lang=1"


def _pib(ministries: list[dict]) -> list[Post]:
    """Government releases, straight from the source rather than via a paper.

    PIB publishes no English feed -- their RSS is Hindi whatever the language
    parameter says, and the English pages render their list through an
    ASP.NET dropdown. So this drives that dropdown: fetch the page, keep its
    hidden form fields, and post them back with the ministry selected.

    The listing carries no per-item time, so an item is dated from when we
    first see it. Checking every fifteen minutes keeps that honest, and the
    release id -- which only ever climbs -- is what tracks progress.
    """
    session = requests.Session()
    session.headers["User-Agent"] = UA
    now = dt.datetime.now(dt.timezone.utc)

    page = session.get(PIB_URL, timeout=TIMEOUT)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")

    posts = []
    for entry in ministries:
        fields = {i.get("name"): i.get("value", "")
                  for i in soup.select("input[type=hidden]") if i.get("name")}
        fields.update({
            "ctl00$ContentPlaceHolder1$ddlMinistry": str(entry["ministry"]),
            "ctl00$ContentPlaceHolder1$ddlday": "0",
            "ctl00$ContentPlaceHolder1$ddlMonth": str(now.month),
            "ctl00$ContentPlaceHolder1$ddlYear": str(now.year),
            "__EVENTTARGET": "ctl00$ContentPlaceHolder1$ddlMinistry",
            "__EVENTARGUMENT": "",
        })
        response = session.post(PIB_URL, data=fields, timeout=TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.select('a[href*="PressReleaseDetail.aspx"]'):
            title = " ".join(link.get_text(" ", strip=True).split())
            href = link.get("href", "")
            if not title or "PRID=" not in href:
                continue
            prid = href.split("PRID=")[-1].split("&")[0]
            if not prid.isdigit():
                continue
            posts.append(Post(
                id=prid,                      # climbs with every release
                handle=f"PIB {entry['name']}",
                text=f"{entry['name']} press release: {title}",
                url="https://www.pib.gov.in/" + href.lstrip("/"),
                created_at=now.isoformat(),
                likes=0, reposts=0, is_reply=False, is_repost=False,
            ))
    return posts


# A filing's headline is frequently just "Enclosed" -- the announcement
# itself is in the attached PDF. Reading it is the difference between
# judging a story and judging a filing reference number.
FILING_CHARS = 2500
FILING_PAGES = 5
FILING_MAX_BYTES = 12_000_000


def _filing_text(url: str) -> str:
    """The readable part of an attached filing, or "" if there isn't one."""
    if not url.lower().endswith(".pdf"):
        return ""
    try:
        response = requests.get(url, timeout=TIMEOUT,
                                headers={"User-Agent": UA, "Referer": "https://www.bseindia.com/"})
        response.raise_for_status()
        if len(response.content) > FILING_MAX_BYTES:
            return ""
        reader = PdfReader(io.BytesIO(response.content))
        text = " ".join((page.extract_text() or "") for page in reader.pages[:FILING_PAGES])
    except Exception:  # noqa: BLE001 - a filing we cannot read is not a crash
        return ""

    body = " ".join(text.split())
    # Everything before the subject line is letterhead: address, CIN, the
    # exchange's own postal address. Start where the company starts talking.
    marker = re.search(r"\b(Sub(?:ject)?\s*[:\-])", body, re.I)
    if marker:
        body = body[marker.start():]
    return body[:FILING_CHARS]


def read_filings(posts: list) -> int:
    """Fetch the attachment behind each filing and fold it into the text.

    Only called for the handful of posts that are about to be scored, so
    this costs a few seconds rather than re-downloading the whole window.
    """
    read = 0
    for post in posts:
        if tier(post.handle) != TIER_FILING:
            continue
        body = _filing_text(post.url)
        if len(body) > 120:                  # ignore scans with no text layer
            post.text = f"{post.text} FULL FILING: {body}"
            read += 1
    if read:
        print(f"  read {read} filing attachment(s) in full")
    return read


def _rss(url: str, label: str) -> list[Post]:
    """Real-world feeds are full of stray entities and broken markup, so this
    goes through feedparser rather than a strict XML parser."""
    response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
    response.raise_for_status()
    parsed = feedparser.parse(response.content)

    posts = []
    for entry in parsed.entries[:MAX_ITEMS]:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        when = entry.get("published_parsed") or entry.get("updated_parsed")
        if not when:
            continue
        stamp = dt.datetime(*when[:6], tzinfo=dt.timezone.utc)
        summary = _plain(entry.get("summary") or "")
        squash = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
        if squash(title)[:60] and squash(title)[:60] in squash(summary):
            summary = ""   # Google News repeats the headline as the summary
        link = entry.get("link") or ""
        if _is_noise(title):
            continue
        body = f"{title}. {summary}".strip().rstrip(".") if summary else title
        posts.append(_make(stamp, label, body[:900], link, link or title))
    return posts


# ------------------------------------------------------ company web pages

# Most company newsrooms, blogs and research pages publish no feed at all.
# For those, the page itself is read: every article link on it is
# remembered with the moment it was first seen, and a link that was not
# there last time is a new post.
#
# That first-seen moment is also the post's id. Progress is tracked as a
# high-water mark per source, so an id must never change for the same
# link -- otherwise every article still sitting on the page would look new
# on every run. The first time a site is read, everything on it is simply
# recorded, exactly as for any other new source.
PAGE_MEMORY = 1500          # links remembered per site; Airtel lists 600
PAGE_HEADERS = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def _same_site(a: str, b: str) -> bool:
    """Same organisation. A subdomain counts -- Tamil Nadu lists releases on
    www.tn.gov.in but serves the files from cms.tn.gov.in."""
    host = lambda u: urlparse(u).netloc.lower().split(":")[0].removeprefix("www.")
    x, y = host(a), host(b)
    return x == y or x.endswith("." + y) or y.endswith("." + x)


# Link text that names the action, not the document. Government sites are
# full of it: "Download (239.7 KB)", "Click here", "View", "डाउनलोड".
_EMPTY_LABEL = re.compile(
    r"^\W*(download|view|click here|read more|learn more|know more|view more|more|"
    r"here|pdf|details?|open|डाउनलोड|देखें|यहाँ क्लिक करें)\b|\(\s*[\d.]+\s*[kmg]b\s*\)$|^[\d.]+\s*[kmg]b$",
    re.I)


def _link_title(anchor, url: str) -> str:
    text = " ".join(anchor.get_text(" ", strip=True).split())
    if len(text) < 12 or _EMPTY_LABEL.search(text):
        # The title usually sits in the same table row or list item.
        row = anchor.find_parent(["tr", "li"]) or anchor.parent
        around = " ".join(row.get_text(" ", strip=True).split()) if row else ""
        around = re.sub(r"\b(download|view|click here)\b|\(\s*[\d.]+\s*[kmg]b\s*\)|डाउनलोड",
                        " ", around, flags=re.I)
        # Tables number their rows: "39 5277 31 Aug 2026 PRESS RELEASE...".
        around = re.sub(r"^(\d+\s+){1,2}(?=\S)", "", " ".join(around.split()))
        if len(around) >= 12:
            text = around
        else:
            slug = [part for part in urlparse(url).path.split("/") if part][-1]
            text = re.sub(r"\.\w+$|^\d{4}-\d{2}-\d{2}-", "", slug).replace("-", " ").replace("_", " ")
    return text[:300]


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def date_in_text(text: str):
    """The first date written in a listing's own text, as the end of that
    day in Indian time -- "11 Sep 2026", "16/09/2026", "September 17 ,2026".
    None if there is no date. Government tables print the release date on
    every row; it is the only honest date a page with no feed offers."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    day = month = year = None
    match = re.search(r"\b(\d{1,2})\s*[-/. ]?\s*([A-Za-z]{3})[a-z]*\.?\s*[-/., ]\s*(20\d\d)\b", text)
    if match and match.group(2).lower() in _MONTHS:
        day, month, year = int(match.group(1)), _MONTHS[match.group(2).lower()], int(match.group(3))
    if day is None:
        match = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})\s*,?\s*(20\d\d)\b", text)
        if match and match.group(1).lower() in _MONTHS:
            day, month, year = int(match.group(2)), _MONTHS[match.group(1).lower()], int(match.group(3))
    if day is None:
        match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d\d)\b", text)
        if match:
            day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if day is None:
        return None
    try:
        return dt.datetime(year, month, day, 23, 59, tzinfo=ist)
    except ValueError:
        return None


def _page(entry: dict, memory: dict) -> list[Post]:
    """New article links on a company page that has no feed."""
    response = requests.get(entry["url"], timeout=TIMEOUT, headers=PAGE_HEADERS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    # Which links are articles. Without a pattern, anything below the page's
    # own address counts: /news/some-story under /news.
    pattern = entry.get("match")
    if not pattern:
        base = urlparse(response.url).path.rstrip("/")
        base = re.sub(r"\.\w+$", "", base)
        pattern = re.escape(base) + r"/[^?#]+"
    wanted = re.compile(pattern)
    # Listings to leave out by their title -- e.g. IMD posts every bulletin
    # twice, in Hindi and English, and only one copy is wanted.
    unwanted = re.compile(entry["exclude"]) if entry.get("exclude") else None

    found = {}
    for anchor in soup.find_all("a", href=True):
        url = urljoin(response.url, anchor["href"]).split("#")[0].split("?")[0].rstrip("/")
        if not _same_site(url, response.url) or url == response.url.rstrip("/"):
            continue
        if not wanted.search(urlparse(url).path):
            continue
        title = _link_title(anchor, url)
        if unwanted and unwanted.search(title):
            continue
        if url not in found or len(title) > len(found[url]):
            found[url] = title
    if not found:
        raise RuntimeError("no article links found -- the page may have changed, "
                           "or now loads its articles by script")

    label = entry["name"]
    known = memory.setdefault(label, {})
    now = dt.datetime.now(dt.timezone.utc)
    for url in found:
        known.setdefault(url, now.isoformat())
    if len(known) > PAGE_MEMORY:
        for url in sorted(known, key=known.get)[:len(known) - PAGE_MEMORY]:
            if url not in found:
                del known[url]

    posts = []
    for url, title in found.items():
        post = _make(dt.datetime.fromisoformat(known[url]), label,
                     f"{label} published: {title}", url, url)
        post.origin = "page"          # its real date is checked when it is read
        posts.append(post)
    return posts


# A company post's news is usually in the body -- a survey figure, a customer,
# a number of jobs -- not in its headline. So before one is judged, the
# article itself is opened and its opening read.
ARTICLE_CHARS = 2500
ARTICLE_BUDGET_SECONDS = 120
# A link can surface on a page long after it was written -- a redesign, a
# "most read" box. Anything whose own date is older than this is not news.
PAGE_MAX_AGE_DAYS = 3
_DATE_META = ("article:published_time", "og:published_time", "datepublished",
              "publish-date", "publish_date", "pubdate", "date", "dc.date")


def _published(soup: BeautifulSoup, url: str):
    """When the article says it was published, if it says at all."""
    raw = None
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        if key in _DATE_META and meta.get("content"):
            raw = meta["content"]
            break
    if not raw:
        for script in soup.find_all("script", type="application/ld+json"):
            match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string or "")
            if match:
                raw = match.group(1)
                break
    if not raw:
        stamp = soup.find("time", attrs={"datetime": True})
        raw = stamp["datetime"] if stamp else None
    if not raw:
        match = re.search(r"/(20\d\d)[-/](\d\d)[-/](\d\d)", url)
        raw = "-".join(match.groups()) if match else None
    if not raw:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)?"
                      r"(?:Z|[+-]\d{2}:?\d{2})?", raw)
    if not match:
        return None
    try:
        when = dt.datetime.fromisoformat(match.group().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def _article(url: str) -> tuple[str, object]:
    """(opening text, published date or None) for one company post."""
    response = requests.get(url, timeout=TIMEOUT, headers=PAGE_HEADERS)
    if response.status_code in (403, 429):
        # Some sites' bot protection turns away about half of all visits at
        # random (OpenAI's does); a second knock a moment later often works.
        time.sleep(3)
        response = requests.get(url, timeout=TIMEOUT, headers=PAGE_HEADERS)
    response.raise_for_status()
    if url.lower().endswith(".pdf") or "pdf" in response.headers.get("Content-Type", ""):
        reader = PdfReader(io.BytesIO(response.content))
        text = " ".join((page.extract_text() or "") for page in reader.pages[:6])
        text = " ".join(text.split())
        # Hindi typed in the old Kruti Dev font reads back as gibberish
        # ("eaf=ifj"kn" for मंत्रिपरिषद) until it is converted.
        if krutidev.looks_like_krutidev(text):
            text = krutidev.to_unicode(text)
        return text[:ARTICLE_CHARS], None

    soup = BeautifulSoup(response.text, "html.parser")
    when = _published(soup, url)
    for clutter in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        clutter.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    parts = [block.get_text(" ", strip=True) for block in root.find_all(["p", "li", "h2", "h3"])]
    text = " ".join(part for part in parts if len(part) > 40)
    return " ".join(text.split())[:ARTICLE_CHARS], when


def read_articles(posts: list) -> list:
    """Read company posts in full before they are judged. Returns the posts
    still worth judging: a page link whose own date shows it is old is
    dropped here. A post that cannot be read is kept, on its headline."""
    started, read, kept, stale = time.monotonic(), 0, [], 0
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=PAGE_MAX_AGE_DAYS)
    for post in posts:
        if tier(post.handle) != TIER_COMPANY or not post.url:
            kept.append(post)
            continue
        if time.monotonic() - started > ARTICLE_BUDGET_SECONDS:
            kept.append(post)
            continue
        try:
            body, when = _article(post.url)
        except Exception:  # noqa: BLE001 - blocked or broken: judge the headline
            kept.append(post)
            continue
        if post.origin == "page" and when and when < cutoff:
            stale += 1
            continue
        if len(body) > 150:
            post.text = f"{post.text} ARTICLE: {body}"
            read += 1
        kept.append(post)
    if read:
        print(f"  read {read} company post(s) in full")
    if stale:
        print(f"  dropped {stale} old article(s) that had only just appeared on a page")
    return kept


# Google News names some outlets by masthead and others by bare domain.
# A byline in your inbox should read the way you would write it in copy.
OUTLETS = {
    "economictimes": "The Economic Times", "timesofindia": "Times of India",
    "livemint": "Mint", "moneycontrol": "Moneycontrol", "inc42": "Inc42",
    "entrackr": "Entrackr", "techcircle": "TechCircle", "cnbctv18": "CNBC-TV18",
    "ndtvprofit": "NDTV Profit", "thehindubusinessline": "BusinessLine",
    "financialexpress": "Financial Express", "business-standard": "Business Standard",
    "hindustantimes": "Hindustan Times", "indianexpress": "The Indian Express",
    "reuters": "Reuters", "bloomberg": "Bloomberg", "thehindu": "The Hindu",
    "medianama": "MediaNama", "yourstory": "YourStory", "digit": "Digit",
}


def _tidy_outlet(name: str) -> str:
    key = name.lower().split(".")[0].strip()
    if key in OUTLETS:
        return OUTLETS[key]
    return name if " " in name else name[:1].upper() + name[1:]


def _google_news(query: str, label: str) -> list[Post]:
    url = ("https://news.google.com/rss/search?q=" + quote_plus(f"{query} when:1d")
           + "&hl=en-IN&gl=IN&ceid=IN:en")
    posts = _rss(url, label)
    for post in posts:
        # Titles arrive as "Headline - Outlet"; the outlet belongs in the
        # source label, not in the middle of the story text.
        if " - " in post.text:
            head, _, tail = post.text.rpartition(" - ")
            outlet = tail.split(".")[0].strip()
            if 0 < len(outlet) <= 40:
                post.origin = _tidy_outlet(outlet)
                post.text = head.strip()
    return posts


def _regulator(which: str) -> list[Post]:
    """CCI, CERT-In or DGFT, as posts for the AI -- see regulators.py.

    These documents carry a date but no time, and CCI often uploads an
    order days after the date on it. So the post is stamped with the moment
    it was first seen, which is what lets it past the freshness check; its
    identifier comes from the document's own date and key, so it is the
    same on every run and is judged only once.
    """
    now = dt.datetime.now(dt.timezone.utc)
    posts = []
    for row in regulators.read(which):
        dated = dt.datetime.combine(row["date"], dt.time(), tzinfo=dt.timezone.utc)
        post = _make(dated, f"REG {regulators.NAMES[which]}",
                     regulators.describe(row), row.get("url", ""), row["key"])
        post.created_at = now.isoformat()
        posts.append(post)
    return posts


def labels(cfg: dict) -> list[str]:
    """Every source label this config can produce, for pruning old state."""
    sources = cfg.get("sources") or {}
    names = [f"BSE {c['name']}" for c in (sources.get("bse") or [])]
    names += [f["name"] for f in (sources.get("rss") or [])]
    names += [f"News: {q['name']}" for q in (sources.get("google_news") or [])]
    names += [f"PIB {m['name']}" for m in (sources.get("pib") or [])]
    names += [p["name"] for p in (sources.get("pages") or [])]
    names += [f"REG {regulators.NAMES[r]}" for r in (sources.get("regulators") or [])]
    return names


def collect(cfg: dict, page_memory: dict | None = None):
    """Returns (posts, failed_source_names).

    page_memory is where company pages without a feed remember the links
    they have seen; the caller keeps it in state.json.
    """
    sources = cfg.get("sources") or {}
    posts, failed = [], []
    page_memory = {} if page_memory is None else page_memory

    companies = sources.get("bse") or []
    if companies:
        try:
            found = _bse(companies, int(cfg.get("bse_days", 2)))
            posts.extend(found)
            print(f"  BSE filings: {len(found)} from {len(companies)} companies")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! BSE failed: {exc}")
            failed.append("BSE")

    ministries = sources.get("pib") or []
    if ministries:
        try:
            found = _pib(ministries)
            posts.extend(found)
            print(f"  PIB: {len(found)} releases from {len(ministries)} ministries")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! PIB failed: {exc}")
            failed.append("PIB")

    for feed in sources.get("rss") or []:
        try:
            found = _rss(feed["url"], feed["name"])
            posts.extend(found)
            print(f"  {feed['name']}: {len(found)} items")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {feed['name']} failed: {exc}")
            failed.append(feed["name"])

    for entry in sources.get("pages") or []:
        try:
            found = _page(entry, page_memory)
            posts.extend(found)
            print(f"  {entry['name']}: {len(found)} article links")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {entry['name']} failed: {str(exc)[:120]}")
            failed.append(entry["name"])

    for which in sources.get("regulators") or []:
        label = f"REG {regulators.NAMES[which]}"
        try:
            found = _regulator(which)
            posts.extend(found)
            print(f"  {regulators.NAMES[which]}: {len(found)} documents in the last 30 days")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {regulators.NAMES[which]} failed: {str(exc)[:120]}")
            failed.append(label)

    for topic in sources.get("google_news") or []:
        label = f"News: {topic['name']}"
        try:
            found = _google_news(topic["q"], label)
            posts.extend(found)
            print(f"  {label}: {len(found)} items")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {label} failed: {exc}")
            failed.append(label)

    if not posts:
        raise XUnavailable(
            f"Every news source came back empty ({len(failed)} failed). "
            "Either this machine has no internet, or every source is down at once."
        )
    return posts, failed
