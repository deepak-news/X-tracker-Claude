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
import re
from dataclasses import dataclass
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup


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
    if label.startswith("pib "):
        return TIER_GOVERNMENT
    return TIER_NEWS if label.startswith("news:") else TIER_COMPANY


def describe(post) -> tuple[str, str]:
    """(what kind of source, which one) -- for the label in the email."""
    level = tier(post.handle)
    name = post.handle.split(" ", 1)[-1] if level in (TIER_FILING, TIER_GOVERNMENT) \
        else post.handle.split(":", 1)[-1].strip() if level == TIER_NEWS else post.handle
    if level == TIER_NEWS and post.origin:
        name = post.origin          # the outlet that actually ran it
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


def labels(cfg: dict) -> list[str]:
    """Every source label this config can produce, for pruning old state."""
    sources = cfg.get("sources") or {}
    names = [f"BSE {c['name']}" for c in (sources.get("bse") or [])]
    names += [f["name"] for f in (sources.get("rss") or [])]
    names += [f"News: {q['name']}" for q in (sources.get("google_news") or [])]
    names += [f"PIB {m['name']}" for m in (sources.get("pib") or [])]
    return names


def collect(cfg: dict):
    """Returns (posts, failed_source_names)."""
    sources = cfg.get("sources") or {}
    posts, failed = [], []

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
