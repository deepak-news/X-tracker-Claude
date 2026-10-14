"""X alerts, on trial: one email, to Deepak only.

Posts come from the Google Sheet that reads X (xfeed.py). Two kinds of
account, each judged the way its desk would judge it if X were added there
later:
  - the "accounts:" list   -- by the Tech Desk's rubric and threshold
  - "x_test: official:"    -- by the Tweets desk's rubric and threshold
Neither desk's own emails, memory or score log is touched. Everything that
clears its bar goes in one email to X_TEST_MAIL_TO, and nobody else.

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


def _tag(post, desk: str):
    """A post labelled by its account, and which desk's bar it is judged by."""
    post.id, post.handle = f"x:{post.id}", f"{post.origin} on X"
    post.desk = desk
    return post


def _recent(posts: list, hours: float) -> list:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return [p for p in posts if dt.datetime.fromisoformat(p.created_at) >= cutoff]


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
    official = (whole.get("x_test") or {}).get("official") or []

    state = load_state()
    memory = state.setdefault("x_test", {})
    seen_list = memory.setdefault("seen", [])
    seen = set(seen_list)

    print("Reading X posts from the Google Sheet...")
    try:
        # An account on both lists (PIB, the PMO) is judged by the Tweets desk's
        # broader bar only, so the same post is never scored or sent twice.
        listed = {str(h).lower() for h in official}
        tech_accounts = [h for h in whole.get("accounts") or [] if str(h).lower() not in listed]
        tech = [_tag(p, "tech") for p in xfeed.posts(tech_accounts)]
        gov = [_tag(p, "tweets") for p in xfeed.posts(official)]
    except Exception as exc:                                    # noqa: BLE001
        # The sheet itself raises the alarm by name: usually an expired login.
        print(f"  ! {exc}")
        return 1
    print(f"  {len(tech)} tech post(s), {len(gov)} official post(s) in the last {xfeed.HOURS}h")

    everything = tech + gov
    if not seen_list:
        print("  first run: noting what is there now, sending nothing")
        if not dry_run:
            memory["seen"] = [p.id for p in everything][-MEMORY:]
            save_state(state)
        return 0

    new = [p for p in everything if p.id not in seen]
    tech = judge.prefilter([p for p in new if p.desk == "tech"], whole)
    gov = _recent([p for p in new if p.desk == "tweets"], float(tweets_cfg.get("max_age_hours", 12)))
    print(f"  new: {len(tech)} tech, {len(gov)} official")

    # Whatever the cheap filters dropped (too old, too short, a duplicate) is
    # done with now; what goes to the AI is done with once the AI has seen it.
    waiting = {p.id for p in tech + gov}
    rows, settled = [], {p.id for p in new} - waiting
    if tech:
        try:
            judged, missed, newly_dead = judge.score(tech, (whole.get("newsworthy") or "").strip(),
                                                     dead_models(state))
            record_dead_models(state, newly_dead)
            bar = float(whole.get("threshold", 7))
            for post, value, headline, _why in judged:
                if value >= bar:
                    rows.append((post, value, headline, "tech"))
            settled |= {p.id for p in tech} - {p.id for p in missed}
        except judge.AllModelsExhausted as exc:
            record_dead_models(state, exc.newly_dead)
            print(f"  ! no AI model is available: {exc}. Tried again next run.")
    if gov:
        judged, done = national.score(gov, (tweets_cfg.get("newsworthy") or "").strip(),
                                      state, memory, log="x_test")
        settled |= done
        bar = float(tweets_cfg.get("threshold", 7))
        rows += [(post, value, headline, "tweets") for post, value, headline, _ in judged if value >= bar]

    for post, value, headline, desk in rows:
        print(f"  SEND {value:g} [{desk}] {post.origin}: {headline}")
    if rows and not dry_run:
        rows.sort(key=lambda r: -r[1])
        subject, body = build_email(rows)
        try:
            email_out.send(subject, body, recipient_env=RECIPIENT, sender_name=SENDER_NAME)
        except Exception as exc:                                # noqa: BLE001
            print(f"  ! email failed ({exc}); these are tried again next run")
            settled -= {r[0].id for r in rows}
        else:
            alerted = memory.setdefault("alerted", [])
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            alerted += [{"at": now, "headline": r[2]} for r in rows]
            memory["alerted"] = alerted[-400:]

    if not dry_run:
        # Everything settled, plus whatever has aged out of the window, is done with.
        memory["seen"] = (seen_list + sorted(settled))[-MEMORY:]
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
