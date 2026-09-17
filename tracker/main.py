"""The robot. Wakes up, checks X, decides, emails, goes back to sleep."""

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import pathlib
import sys

import yaml

from . import email_out, judge, sources
from .sources import XUnavailable

ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "watchlist.yml"
STATE = ROOT / "state.json"

# How many runs in a row must fail before we email about it. Stops one blip
# from bothering you, while a genuinely dead session still gets through.
FAILURES_BEFORE_ALARM = 3


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# How long to treat a model as dead after it says it is out of quota.
# Google resets the daily quota once every 24h, but not at UTC midnight --
# it lands somewhere around midday IST, at a time Google does not publish.
# Comparing calendar dates would sometimes call a model "fresh" again just
# hours after it died, and other times leave it marked dead for up to 18
# extra hours after Google already reset it. A rolling cooldown avoids
# both: worst case is one wasted probe a little early, never a half-day of
# needlessly skipping a model that is actually available again.
DEAD_MODEL_COOLDOWN_HOURS = 20


def dead_models(state: dict) -> set:
    """Model names still likely out of quota, based on when they last failed."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=DEAD_MODEL_COOLDOWN_HOURS)
    live = {}
    for name, marked_at in state.get("dead_models", {}).items():
        try:
            when = dt.datetime.fromisoformat(marked_at)
        except ValueError:
            continue
        if when >= cutoff:
            live[name] = marked_at
    return set(live)


def record_dead_models(state: dict, newly_dead: set) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    graveyard = state.setdefault("dead_models", {})
    for name in newly_dead:
        graveyard[name] = now
    # Drop anything old enough that dead_models() would ignore it anyway,
    # so this does not grow forever.
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=DEAD_MODEL_COOLDOWN_HOURS)
    for name in list(graveyard):
        try:
            if dt.datetime.fromisoformat(graveyard[name]) < cutoff:
                del graveyard[name]
        except ValueError:
            del graveyard[name]


# Google News items wait and go out together, once an hour, just after the
# hour in Indian time -- so reporters know when to look. Primary sources
# never wait. An hour with nothing waiting sends nothing.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _queued_post(entry: dict):
    return sources.Post(**entry["post"])


def _is_news(post) -> bool:
    return sources.tier(post.handle) == sources.TIER_NEWS


def hold_news(state: dict, rows: list, unscreened: list) -> None:
    """Park news-report stories until the next digest."""
    queue = state.setdefault("news_queue", [])
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    waiting = {e["post"].get("url") for e in queue}
    for post, value, headline, why in rows:
        if post.url not in waiting:
            queue.append({"post": dataclasses.asdict(post), "score": value,
                          "headline": headline, "why": why, "queued_at": now})
    for post in unscreened:
        if post.url not in waiting:
            queue.append({"post": dataclasses.asdict(post), "score": None,
                          "headline": "", "why": "", "queued_at": now})


def release_superseded(state: dict, primary_rows: list) -> None:
    """A filing or release just went out about a story that was waiting.

    The primary source IS the story; the news report of it would only arrive
    an hour later as a repeat. So it leaves the queue.
    """
    queue = state.get("news_queue") or []
    if not queue or not primary_rows:
        return
    kept = [e for e in queue
            if not any(judge._same_story(_queued_post(e), row[0]) for row in primary_rows)]
    if len(kept) < len(queue):
        print(f"  dropped {len(queue) - len(kept)} waiting news report(s): "
              f"the original source was just sent instead")
    state["news_queue"] = kept


def send_digest(state: dict, dry_run: bool, label: str = "") -> bool:
    """Email everything waiting as one message. Returns whether it went."""
    sent = {e.get("url") for e in state.get("alerted", []) if e.get("url")}
    rows, raw = [], []
    for entry in state.get("news_queue") or []:
        post = _queued_post(entry)
        if post.url in sent:
            continue
        if entry.get("score") is None:
            raw.append(post)
        else:
            rows.append((post, float(entry["score"]), entry.get("headline") or post.text[:100],
                         entry.get("why") or ""))

    # Several outlets may have queued the same story during the hour.
    rows.sort(key=lambda r: -r[1])
    unique = []
    for row in rows:
        if not any(judge._same_story(row[0], kept[0]) for kept in unique):
            unique.append(row)

    if not unique and not raw:
        state["news_queue"] = []
        return False
    subject, body = email_out.build_digest(unique, raw)
    subject = f"{label or 'Hourly'} news digest: {subject}"
    if dry_run:
        print(f"(dry run) the hourly digest would go now: {subject}")
        return False
    email_out.send(subject, body, sender_name=email_out.mail_name("tech_desk"))
    print(f"Emailed the hourly digest: {subject}")
    judge.remember_alerted(state, unique + [(p, 0, p.text[:110], "") for p in raw])
    state["news_queue"] = []
    return True


def hourly_digest(state: dict, dry_run: bool) -> None:
    """Send the news digest if this hour's has not gone yet.

    The first run after the hour turns is the digest run: whatever waited
    through the previous hour goes then, including anything that run itself
    just found. Once an hour's slot is used -- sent, or found empty -- later
    runs in that hour leave the queue alone, so a report found at 3:05 goes
    at 4:00, never at 3:20. A digest that fails to send keeps its slot open
    and is tried again on the next run.
    """
    now = dt.datetime.now(IST)
    slot = now.strftime("%Y-%m-%dT%H")
    if not state.get("last_digest_hour") and not dry_run:
        # The very first run: start the clock rather than sending a digest
        # at whatever odd minute this happens to be. The first one goes on
        # the next hour.
        state["last_digest_hour"] = slot
    if state.get("last_digest_hour") == slot:
        waiting = len(state.get("news_queue") or [])
        if waiting:
            print(f"{waiting} news report(s) waiting for the {(now + dt.timedelta(hours=1)).strftime('%-I %p')} digest")
        return
    if state.get("news_queue"):
        label = now.strftime("%-I %p")
        if not dry_run and email_out.pressure() != "normal":
            # Left unsent and the slot left open: it goes once there is room.
            print(f"  email budget is {email_out.pressure()}: the {label} digest waits")
            return
        try:
            send_digest(state, dry_run, label)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! the {label} news digest could not be sent ({exc}); trying again next run")
            return
    if not dry_run:
        state["last_digest_hour"] = slot


def report_breakage(state: dict, reason: str) -> None:
    """Something is broken. Nag once, not every run."""
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    count = state["consecutive_failures"]
    print(f"FAILURE {count}: {reason}")

    if count == FAILURES_BEFORE_ALARM:
        try:
            email_out.send(
                "News tracker has stopped working",
                f"""<div style="max-width:600px;margin:0 auto;padding:24px;
                     font:400 15px/1.6 -apple-system,Segoe UI,sans-serif;color:#222;">
                  <p><strong>Your news tracker has failed three runs in a row.</strong></p>
                  <p>{reason}</p>
                  <p>Check the Actions tab on GitHub and open the most recent red run --
                  the log names whichever source or step is failing. A single source
                  going down does not cause this; it takes all of them, or the AI
                  scoring step, to fail repeatedly.</p>
                  <p style="color:#888;font-size:13px;">You won't be emailed about
                  this again until it recovers and breaks a second time.</p>
                </div>""",
                recipient_env=email_out.ADMIN_ENV,
                sender_name=email_out.mail_name("maintenance"),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not send the breakage email either: {exc}")


async def run(dry_run: bool) -> int:
    cfg = yaml.safe_load(WATCHLIST.read_text())
    rubric = (cfg.get("newsworthy") or "").strip()
    threshold = float(cfg.get("threshold", 7))
    # Whether Google News stories wait for the hourly digest.
    hourly = bool(cfg.get("news_digest_hourly", True))
    feeds = sources.labels(cfg)

    if not feeds or not rubric:
        print("watchlist.yml is missing sources or the newsworthy description.")
        return 1

    state = load_state()
    seen: dict = state.setdefault("seen", {})

    print(f"Checking {len(feeds)} sources...")
    try:
        posts, failed = await asyncio.to_thread(
            sources.collect, cfg, state.setdefault("page_links", {}))
    except XUnavailable as exc:
        report_breakage(state, str(exc))
        if hourly:
            hourly_digest(state, dry_run)
        save_state(state)
        return 2

    if failed:
        print(f"  ! sources that failed this run: {', '.join(failed)}")

    # Work out what is genuinely new, per account. The new high-water marks
    # are held back until the whole run succeeds -- if scoring or email fails
    # we must NOT record these posts as seen, or they would be lost for good.
    by_handle: dict[str, list] = {}
    for post in posts:
        by_handle.setdefault(post.handle.lower(), []).append(post)

    new_marks: dict[str, int] = {}
    fresh = []
    for handle, group in by_handle.items():
        newest = max(int(p.id) for p in group)
        if handle not in seen:
            # Never seen this account before. Note where it is and stay quiet,
            # otherwise its backlog would arrive as one huge email.
            new_marks[handle] = newest
            continue
        since = int(seen[handle])
        new_marks[handle] = max(newest, since)
        fresh.extend([p for p in group if int(p.id) > since])

    candidates = judge.prefilter(fresh, cfg)
    # Stories already emailed do not need scoring or sending a second time.
    candidates = judge.drop_already_alerted(candidates, state)
    # A news report of a story already waiting for the digest adds nothing.
    # Only reports are checked: a filing about it must still go straight out.
    waiting = [_queued_post(e) for e in state.get("news_queue") or []]
    if hourly and waiting:
        before = len(candidates)
        candidates = [p for p in candidates
                      if not (_is_news(p) and any(judge._same_story(p, w) for w in waiting))]
        if before > len(candidates):
            print(f"  skipped {before - len(candidates)} report(s) of stories already waiting for the digest")
    print(f"{len(fresh)} new posts, {len(candidates)} survived the cheap filters")

    # Read the actual filings before judging them. A headline of "Enclosed"
    # says nothing; the PDF behind it is the announcement.
    if candidates and cfg.get("read_filings", True):
        await asyncio.to_thread(sources.read_filings, candidates)
    # Blogs, reports and newsroom posts: the news is in the body.
    if candidates and cfg.get("read_articles", True):
        candidates = await asyncio.to_thread(sources.read_articles, candidates)

    # Whether the AI was actually asked to do its job this run, and whether
    # it came through. A run with nothing to score proves nothing either
    # way, so it must not be allowed to quietly disarm the alarm below.
    scoring_attempted = bool(candidates)

    newsworthy, unscreened = [], []
    if candidates:
        try:
            judged, unscreened, newly_dead = judge.score(
                candidates, rubric, dead_models(state), judge.recent_headlines(state))
        except judge.AllModelsExhausted as exc:
            # Even a total failure teaches us which models are dead today --
            # keep that lesson so the next run (ten minutes from now) does
            # not waste a request re-probing them from scratch.
            record_dead_models(state, exc.newly_dead)
            report_breakage(state, f"The AI scoring step failed: {exc}")
            if hourly:
                hourly_digest(state, dry_run)
            save_state(state)
            return 2
        except Exception as exc:  # noqa: BLE001
            # Records the failure but does NOT advance what we have seen, so
            # these posts get another chance on the next run.
            report_breakage(state, f"The AI scoring step failed: {exc}")
            if hourly:
                hourly_digest(state, dry_run)
            save_state(state)
            return 2
        record_dead_models(state, newly_dead)
        # Primary sources first, best score within each.
        newsworthy = sorted((r for r in judged if r[1] >= threshold),
                            key=lambda r: (sources.tier(r[0].handle), -r[1]))
        for post, value, headline, _ in judged:
            mark = "SEND" if value >= threshold else "skip"
            print(f"  [{mark}] {value:.0f}/10 {post.handle}: {headline}")

    # Original sources go out now. Google News reports wait for the digest.
    held, held_raw = [], []
    if hourly:
        held = [r for r in newsworthy if _is_news(r[0])]
        held_raw = [p for p in unscreened if _is_news(p)]
        newsworthy = [r for r in newsworthy if not _is_news(r[0])]
        unscreened = [p for p in unscreened if not _is_news(p)]

    # Close to Gmail's daily limit, only MAJOR stories go out.
    if (newsworthy or unscreened) and not dry_run and email_out.pressure() == "tight":
        before = len(newsworthy) + len(unscreened)
        newsworthy = [r for r in newsworthy if r[1] >= 9]
        unscreened = []
        print(f"  email budget is tight: sending only MAJOR stories, "
              f"{before - len(newsworthy)} lesser one(s) left out")

    if newsworthy or unscreened:
        subject, body = email_out.build_digest(newsworthy, unscreened)
        if dry_run:
            print(f"\n(dry run) would have emailed: {subject}")
        else:
            try:
                email_out.send(subject, body, sender_name=email_out.mail_name("tech_desk"))
            except Exception as exc:  # noqa: BLE001
                report_breakage(state, f"Sending the alert email failed: {exc}")
                if hourly:
                    hourly_digest(state, dry_run)
                save_state(state)
                return 2
            print(f"\nEmailed: {subject}")
            judge.remember_alerted(state, newsworthy)
            release_superseded(state, newsworthy)
    else:
        print("\nNo original-source alert this run.")

    if held or held_raw:
        hold_news(state, held, held_raw)
        print(f"Holding {len(held) + len(held_raw)} news report(s) for the hourly digest "
              f"({len(state['news_queue'])} waiting in all)")


    if hourly:
        hourly_digest(state, dry_run)

    if not dry_run:
        seen.update(new_marks)
        # Forget accounts that are no longer watched, so state.json stays a
        # readable picture of the current watchlist instead of a junk drawer.
        watched = {f.lower() for f in feeds}
        for gone in [h for h in seen if h not in watched]:
            del seen[gone]
        state["last_success"] = dt.datetime.now(dt.timezone.utc).isoformat()
        # Only clear the alarm when the AI was actually asked to do its job
        # and came through. A run with nothing new to score proves nothing
        # either way -- if it were allowed to reset the counter, a single
        # quiet ten-minute gap after a real failure would silently disarm
        # the alarm for the rest of a day-long quota outage.
        if scoring_attempted:
            state["consecutive_failures"] = 0
        save_state(state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except send email or save progress")
    parser.add_argument("--test-email", action="store_true",
                        help="send one test email and stop")
    args = parser.parse_args()

    if args.test_email:
        email_out.send(
            f"{email_out.mail_name('tech_desk')}: test email",
            """<div style="max-width:600px;margin:0 auto;padding:24px;
                 font:400 15px/1.6 -apple-system,Segoe UI,sans-serif;">
              <p>If you are reading this, email is working correctly.</p>
            </div>""",
        )
        print("Sent. Check the inbox, and the spam folder.")
        return 0

    return asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
