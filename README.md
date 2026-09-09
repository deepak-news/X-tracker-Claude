# News Tracker

Watches primary news sources around the clock and emails you when something
newsworthy appears. Free to run, forever.

## Why it no longer reads X

It used to. In September 2026 X began refusing every request from data-centre
IP addresses, which is where any free server lives. A logged-in session works
fine from a home connection and not at all from GitHub, and every free
workaround was tested and rejected: guest access serves year-old snapshots for
most accounts, the embed endpoint serves frozen caches, the Nitter mirrors are
gone, and X's own API has had no free tier since February 2026.

So it reads the sources X posts were usually *about* instead — which for
company and government news arrive earlier and carry more authority.

## Where the news comes from

1. **BSE filings** — what an Indian listed company formally told the exchange.
   The primary document, usually ahead of the company's own tweet.
2. **Company newsrooms** — NVIDIA, OpenAI, Google, Meta, AMD, Samsung.
3. **Google News** — the catch-all, including what individuals said, once a
   publication has reported it.

## How a run works

1. `sources.py` pulls every feed.
2. Anything already seen, too old, or a duplicate of another outlet's copy of
   the same story is dropped — before anything is paid for.
3. `judge.py` asks Gemini to score what is left out of 10 against the brief in
   `watchlist.yml`.
4. Anything at or above `threshold` is emailed by `email_out.py`.
5. `state.json` records how far each source has been read.

## The files

| File | What it does |
|---|---|
| `watchlist.yml` | The sources, the brief, and the threshold. The only file to edit normally. |
| `tracker/sources.py` | Reads the feeds. |
| `tracker/judge.py` | Cheap filters, deduplication, then the AI scoring. |
| `tracker/email_out.py` | Builds and sends the email. |
| `tracker/main.py` | Runs the sequence and tracks progress. |
| `tracker/fetch.py` | The old X reader. Unused, kept in case X access returns. |
| `state.json` | How far each source has been read. Written by the robot. |

## Settings

Six secrets, in Settings -> Secrets and variables -> Actions:
`GEMINI_API_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `MAIL_TO`.
(`X_COOKIES` and `X_LIST_ID` are no longer used and can be deleted.)

Timing comes from cron-job.org, which POSTs to the workflow every 15 minutes.
GitHub's own schedule is a fallback only — it drops most scheduled runs.

## When something breaks

Three failed runs in a row trigger an email. One source failing does not:
the run continues on the others, and the log names whichever one failed.
