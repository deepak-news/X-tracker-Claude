"""The robot. Wakes up, checks X, decides, emails, goes back to sleep."""

import argparse
import asyncio
import datetime as dt
import json
import pathlib
import sys

import yaml

from . import email_out, judge
from .fetch import XUnavailable, collect

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


def report_breakage(state: dict, reason: str) -> None:
    """X access is broken. Nag once, not every ten minutes."""
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    count = state["consecutive_failures"]
    print(f"FAILURE {count}: {reason}")

    if count == FAILURES_BEFORE_ALARM:
        try:
            email_out.send(
                "X Tracker has stopped working",
                f"""<div style="max-width:600px;margin:0 auto;padding:24px;
                     font:400 15px/1.6 -apple-system,Segoe UI,sans-serif;color:#222;">
                  <p><strong>Your X tracker can no longer read X.</strong></p>
                  <p>{reason}</p>
                  <p>This nearly always means the burner account's saved session has
                  expired or been blocked. The fix is to log into the burner account
                  again and replace the <code>X_COOKIES</code> secret on GitHub with
                  a fresh value.</p>
                  <p style="color:#888;font-size:13px;">You won't be emailed about
                  this again until it recovers and breaks a second time.</p>
                </div>""",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not send the breakage email either: {exc}")


async def run(dry_run: bool) -> int:
    cfg = yaml.safe_load(WATCHLIST.read_text())
    handles = [str(h).strip().lstrip("@") for h in cfg.get("accounts", []) if h]
    rubric = (cfg.get("newsworthy") or "").strip()
    priority = [str(h).strip().lstrip("@") for h in (cfg.get("priority") or []) if h]
    threshold = float(cfg.get("threshold", 7))

    if not handles or not rubric:
        print("watchlist.yml is missing accounts or the newsworthy description.")
        return 1

    state = load_state()
    seen: dict = state.setdefault("seen", {})
    id_cache: dict = state.setdefault("user_ids", {})
    offset = int(state.get("sweep_offset", 0))

    print(f"Checking {len(handles)} accounts...")
    try:
        posts, failed, next_offset = await collect(handles, cfg, id_cache, offset, priority)
    except XUnavailable as exc:
        report_breakage(state, str(exc))
        save_state(state)
        return 2

    state["sweep_offset"] = next_offset
    if failed:
        print(f"  ! could not resolve: {', '.join('@' + h for h in failed)}")

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
    print(f"{len(fresh)} new posts, {len(candidates)} survived the cheap filters")

    # Whether the AI was actually asked to do its job this run, and whether
    # it came through. A run with nothing to score proves nothing either
    # way, so it must not be allowed to quietly disarm the alarm below.
    scoring_attempted = bool(candidates)

    newsworthy, unscreened = [], []
    if candidates:
        try:
            judged, unscreened, newly_dead = judge.score(candidates, rubric, dead_models(state))
        except judge.AllModelsExhausted as exc:
            # Even a total failure teaches us which models are dead today --
            # keep that lesson so the next run (ten minutes from now) does
            # not waste a request re-probing them from scratch.
            record_dead_models(state, exc.newly_dead)
            report_breakage(state, f"The AI scoring step failed: {exc}")
            save_state(state)
            return 2
        except Exception as exc:  # noqa: BLE001
            # Records the failure but does NOT advance what we have seen, so
            # these posts get another chance on the next run.
            report_breakage(state, f"The AI scoring step failed: {exc}")
            save_state(state)
            return 2
        record_dead_models(state, newly_dead)
        newsworthy = sorted((r for r in judged if r[1] >= threshold), key=lambda r: -r[1])
        for post, value, headline, _ in judged:
            mark = "SEND" if value >= threshold else "skip"
            print(f"  [{mark}] {value:.0f}/10 @{post.handle}: {headline}")

    if newsworthy or unscreened:
        subject, body = email_out.build_digest(newsworthy, unscreened)
        if dry_run:
            print(f"\n(dry run) would have emailed: {subject}")
        else:
            try:
                email_out.send(subject, body)
            except Exception as exc:  # noqa: BLE001
                report_breakage(state, f"Sending the alert email failed: {exc}")
                save_state(state)
                return 2
            print(f"\nEmailed: {subject}")
    else:
        print("\nNothing worth emailing.")

    if not dry_run:
        seen.update(new_marks)
        # Forget accounts that are no longer watched, so state.json stays a
        # readable picture of the current watchlist instead of a junk drawer.
        watched = {h.lower() for h in handles}
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
            "X Tracker test email",
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
