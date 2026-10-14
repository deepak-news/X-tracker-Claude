"""X alerts, on trial: one email, to Deepak only.

Posts come from the Google Sheet that reads X (xfeed.py): every account
@DeepakatPTI follows with the bell on, so following a new account is all it
takes to add it. Each post is judged the way a desk would judge it if X were
added there later:
  - every post            -- by the Tweets desk's rubric and threshold
  - the "accounts:" list  -- ALSO by the Tech Desk's, so a story that matters
                             to the beat but not to the general public still
                             gets through
A post is sent if it clears either bar. Neither desk's own emails, memory or
score log is touched; everything goes to X_TEST_MAIL_TO, and nobody else.

One announcement is usually posted by several accounts -- the PMO, the
minister, PIB and the ministry, often over an hour or so. So before the AI
sees anything, a post telling the same story as another one in this run, or
as anything already emailed in the last 48 hours (by this trial or by the
Tech Desk), is dropped. The AI is then shown those headlines too and asked to
mark repeats; what is left is one alert per story.

    python -m tracker.xdesk              check, score, email
    python -m tracker.xdesk --dry-run    score and print, send nothing, save nothing

Does nothing until both X_FEED_URL and X_TEST_MAIL_TO exist.
"""

import argparse
import datetime as dt
import html
import os
import pathlib
import sys

import yaml

from . import email_out, judge, national, xfeed
from .main import dead_models, load_state, record_dead_models, save_state

WATCHLIST = pathlib.Path(__file__).resolve().parent.parent / "watchlist.yml"

RECIPIENT = "X_TEST_MAIL_TO"
SENDER_NAME = "X Alerts (test)"
MEMORY = 3000       # post ids remembered: several days at the current pace
SENT_HOURS = 48     # how long a story counts as already sent, as on the desks
MIN_LENGTH = 25


def _tag(post):
    """A post labelled by its account, under this trial's identifier scheme."""
    post.id, post.handle = f"x:{post.id}", f"{post.origin} on X"
    return post


def _words(post) -> set:
    """What the post says, without the label put in front of it."""
    return judge._fingerprint(post.text.split(" on X: ", 1)[-1][:300])


def _alike(a: set, b: set) -> bool:
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= judge.SAME_STORY


def _sent_before(state: dict, memory: dict) -> list:
    """Every story emailed in the last two days: this trial's and the Tech Desk's."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=SENT_HOURS)).isoformat()
    entries = (state.get("alerted") or []) + (memory.get("alerted") or [])
    return [e for e in entries if e.get("at", "") >= cutoff]


def fold(posts: list, sent: list) -> tuple:
    """(posts to judge, posts dropped as a story already in hand)."""
    earlier = [set(e.get("words") or []) for e in sent]
    urls = {e.get("url") for e in sent if e.get("url")}
    kept, dropped, kept_words = [], [], []
    for post in sorted(posts, key=lambda p: p.created_at):      # the first to post it wins
        words = _words(post)
        if post.url in urls or any(_alike(words, w) for w in earlier + kept_words):
            dropped.append(post)
            continue
        kept.append(post)
        kept_words.append(words)
    return kept, dropped


def build_email(rows: list) -> tuple:
    """rows: (post, score, headline, desk), best first."""
    font = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"
    cards = []
    for post, value, headline, desk in rows:
        original = post.text.split(" on X: ", 1)[-1]
        cards.append(f"""
        <div style="border:1px solid #e5e7eb;border-radius:6px;padding:16px 18px;margin-bottom:14px;">
          <div style="color:#6b7280;font:600 11px/1 {font};letter-spacing:.06em;margin-bottom:10px;">
            {html.escape(post.origin)} &middot; {desk.upper()} &middot; {value:g}/10</div>
          <div style="font:600 17px/1.35 {font};color:#111827;">{html.escape(headline or original[:160])}</div>
          <div style="font:400 13px/1.5 {font};color:#6b7280;margin-top:8px;">
            As posted: {html.escape(original[:400])}</div>
          <div style="margin-top:12px;"><a href="{html.escape(post.url)}"
             style="font:500 13px/1 {font};color:#1d4ed8;text-decoration:none;">Open on X &rarr;</a></div>
        </div>""")
    lead = rows[0][2] or rows[0][0].origin
    subject = lead if len(rows) == 1 else f"{len(rows)} X posts: {lead}"
    body = f"""<div style="max-width:640px;margin:0 auto;padding:24px 20px;background:#fff;">
      <div style="font:600 11px/1 {font};letter-spacing:.12em;color:#6b7280;
                  text-transform:uppercase;margin-bottom:16px;">X alerts &middot; on trial, to you only</div>
      {''.join(cards)}
    </div>"""
    return f"[X test] {subject}"[:150], body


def run(dry_run: bool) -> int:
    if not xfeed.enabled():
        print("No X_FEED_URL secret yet -- nothing to read.")
        return 0
    if not dry_run and not os.environ.get(RECIPIENT, "").strip():
        print(f"No {RECIPIENT} secret yet, so there is nobody to send to.")
        return 0

    whole = yaml.safe_load(WATCHLIST.read_text()) or {}
    tweets_cfg = whole.get("national") or {}
    tech_accounts = {str(h).lower() for h in whole.get("accounts") or []}

    state = load_state()
    memory = state.setdefault("x_test", {})
    seen_list = memory.setdefault("seen", [])
    seen = set(seen_list)

    print("Reading X posts from the Google Sheet...")
    try:
        everything = [_tag(p) for p in xfeed.posts()]
    except Exception as exc:                                    # noqa: BLE001
        # The sheet itself raises the alarm by name: usually an expired login.
        print(f"  ! {exc}")
        return 1
    print(f"  {len(everything)} post(s) from {len({p.origin for p in everything})} accounts "
          f"in the last {xfeed.HOURS}h")

    if not seen_list:
        print("  first run: noting what is there now, sending nothing")
        if not dry_run:
            memory["seen"] = [p.id for p in everything][-MEMORY:]
            save_state(state)
        return 0

    new = [p for p in everything if p.id not in seen]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=float(tweets_cfg.get("max_age_hours", 12)))
    current = [p for p in new if len(p.text.split(" on X: ", 1)[-1]) >= MIN_LENGTH
               and dt.datetime.fromisoformat(p.created_at) >= cutoff]
    sent = _sent_before(state, memory)
    candidates, repeats = fold(current, sent)
    print(f"  new: {len(new)}; worth judging: {len(current)}; "
          f"same story as another post or an earlier alert: {len(repeats)}")
    for post in repeats:
        print(f"    repeat: {post.origin}: {post.text.split(' on X: ', 1)[-1][:70]}")

    # Whatever the cheap checks dropped is done with now; what goes to the AI
    # is done with once each bar it is judged by has seen it.
    done = {p.id for p in new} - {p.id for p in candidates}
    headlines = [e.get("headline", "") for e in sent if e.get("headline")]
    best: dict = {}                       # post id -> (post, score, headline, desk)

    def consider(post, value, headline, desk, bar):
        if value >= bar and value > best.get(post.id, (None, -1))[1]:
            best[post.id] = (post, value, headline, desk)

    tweets_done = set()
    if candidates:
        judged, tweets_done = national.score(candidates, (tweets_cfg.get("newsworthy") or "").strip(),
                                             state, {"alerted": sent}, log="x_test")
        for post, value, headline, _ in judged:
            consider(post, value, headline, "tweets", float(tweets_cfg.get("threshold", 7)))

    tech_age = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=float(whole.get("max_age_hours", 6)))
    tech = [p for p in candidates if p.origin[1:].lower() in tech_accounts
            and dt.datetime.fromisoformat(p.created_at) >= tech_age]
    tech_done = {p.id for p in candidates if p not in tech}
    if tech:
        try:
            judged, missed, newly_dead = judge.score(tech, (whole.get("newsworthy") or "").strip(),
                                                     dead_models(state), already_sent=headlines)
            record_dead_models(state, newly_dead)
            for post, value, headline, _why in judged:
                consider(post, value, headline, "tech", float(whole.get("threshold", 7)))
            tech_done |= {p.id for p in tech} - {p.id for p in missed}
        except judge.AllModelsExhausted as exc:
            record_dead_models(state, exc.newly_dead)
            print(f"  ! no AI model is available: {exc}. Tried again next run.")
    done |= tweets_done & tech_done

    # The AI judged each post on its own; two accounts telling one story in
    # this run still fold to the higher-scored one.
    rows = []
    for row in sorted(best.values(), key=lambda r: -r[1]):
        if not any(_alike(_words(row[0]), _words(kept[0])) for kept in rows):
            rows.append(row)
    for post, value, headline, desk in rows:
        print(f"  SEND {value:g} [{desk}] {post.origin}: {headline}")

    if rows and not dry_run:
        subject, body = build_email(rows)
        try:
            email_out.send(subject, body, recipient_env=RECIPIENT, sender_name=SENDER_NAME)
        except Exception as exc:                                # noqa: BLE001
            print(f"  ! email failed ({exc}); these are tried again next run")
            done -= {r[0].id for r in rows}
        else:
            done |= {r[0].id for r in rows}
            now = dt.datetime.now(dt.timezone.utc)
            cutoff = (now - dt.timedelta(hours=SENT_HOURS)).isoformat()
            memory["alerted"] = [e for e in memory.get("alerted") or [] if e.get("at", "") >= cutoff]
            memory["alerted"] += [{"at": now.isoformat(), "headline": r[2], "url": r[0].url,
                                   "words": sorted(_words(r[0]))} for r in rows]

    if not dry_run:
        memory["seen"] = (seen_list + sorted(done))[-MEMORY:]
        memory["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save_state(state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="score and print, send nothing, save nothing")
    return run(parser.parse_args().dry_run)


if __name__ == "__main__":
    sys.exit(main())
