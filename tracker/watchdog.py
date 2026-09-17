"""Notices when the 15-minute timer has stopped, and covers for it.

Every tracker run is started from outside, by cron-job.org, using a GitHub
token. If that token expires or cron-job.org switches the job off, nothing
runs -- and because nothing runs, nothing fails, so no email ever says so.
The tracker would simply go quiet.

This runs once an hour on GitHub's own scheduler, which does not depend on
cron-job.org. It looks at when the timer last started a run. If that was
too long ago it starts a run itself, so alerts keep flowing at least hourly,
and emails you what is wrong and how to fix it.

    python -m tracker.watchdog            (on GitHub; needs GITHUB_TOKEN)
"""

import datetime as dt
import os
import sys

import requests

from . import email_out

WORKFLOW = "check.yml"
API = "https://api.github.com"
# Runs every 15 minutes; a run can take a few minutes more. Beyond this,
# the timer has missed at least two starts in a row.
LATE_MINUTES = 45
# How often to repeat the email while it stays broken.
REMIND_EVERY_MINUTES = 6 * 60
# The watchdog itself runs roughly hourly, and GitHub's scheduler can be a
# little late, so each email "window" is slightly wider than an hour.
WINDOW_MINUTES = 65
# Runs this watchdog starts are made by GitHub's own bot account. They must
# not count as the timer being alive.
BOT = "github-actions[bot]"


def _get(path: str, token: str) -> dict:
    response = requests.get(f"{API}{path}", timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    response.raise_for_status()
    return response.json()


def assess(runs: list, now: dt.datetime) -> dict:
    """What to do, given the most recent runs of the tracker, newest first."""
    started = lambda run: dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    from_timer = [r for r in runs if (r.get("triggering_actor") or {}).get("login") != BOT]
    if not from_timer:
        return {"late": True, "gap": None, "dispatch": True, "email": True}

    gap = (now - started(from_timer[0])).total_seconds() / 60
    if gap <= LATE_MINUTES:
        return {"late": False, "gap": gap, "dispatch": False, "email": False}

    # Do not pile runs up: if one is already under way or was started very
    # recently (by this watchdog an hour ago it will have finished), wait.
    busy = any(r["status"] in ("queued", "in_progress", "waiting") or
               (now - started(r)).total_seconds() < 20 * 60 for r in runs[:5])
    first_notice = gap < LATE_MINUTES + WINDOW_MINUTES
    reminder = gap >= REMIND_EVERY_MINUTES and (gap % REMIND_EVERY_MINUTES) < WINDOW_MINUTES
    return {"late": True, "gap": gap, "dispatch": not busy, "email": first_notice or reminder}


def _email(gap) -> None:
    hours = "an unknown time" if gap is None else (
        f"{gap / 60:.1f} hours" if gap >= 90 else f"{gap:.0f} minutes")
    email_out.send(
        "News tracker: the 15-minute timer has stopped",
        f"""<div style="max-width:600px;margin:0 auto;padding:24px;
             font:400 15px/1.6 -apple-system,Segoe UI,sans-serif;color:#222;">
          <p><strong>The timer that starts your tracker every 15 minutes has not
          started a run for {hours}.</strong></p>
          <p>Nothing is lost. Until it is fixed, this watchdog starts a run itself
          once an hour, so alerts still arrive -- just up to an hour late instead
          of within 15 minutes.</p>
          <p><strong>The two usual causes, most likely first:</strong></p>
          <ol>
            <li><strong>The GitHub token has expired.</strong> Open
            github.com/settings/personal-access-tokens, check the token used by
            cron-job.org, and regenerate it. Paste the new token into the job on
            cron-job.org.</li>
            <li><strong>cron-job.org has switched the job off</strong> after it
            failed several times. Log in to cron-job.org, open the job, and turn it
            back on. Its execution history will say why it was failing.</li>
          </ol>
          <p style="color:#888;font-size:13px;">You will be reminded every six hours
          until the timer is running again.</p>
        </div>""",
    )


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        print("Needs GITHUB_TOKEN and GITHUB_REPOSITORY -- this runs on GitHub.")
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    runs = _get(f"/repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page=30", token)["workflow_runs"]
    verdict = assess(runs, now)

    if not verdict["late"]:
        print(f"Timer is healthy: last started a run {verdict['gap']:.0f} minutes ago.")
        return 0

    gap = verdict["gap"]
    print(f"TIMER LATE: last run it started was "
          f"{'never, in recent history' if gap is None else f'{gap:.0f} minutes ago'}.")

    if verdict["dispatch"]:
        branch = _get(f"/repos/{repo}", token).get("default_branch", "main")
        response = requests.post(
            f"{API}/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches", timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"ref": branch, "inputs": {"mode": "normal"}})
        print("Started a run to cover for it." if response.status_code == 204
              else f"Could not start a run ({response.status_code}): {response.text[:200]}")
    else:
        print("A run is already under way; not starting another.")

    if verdict["email"]:
        try:
            _email(gap)
            print("Emailed you about it.")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not email about it: {exc}")
    # Green either way: the watchdog did its job by noticing.
    return 0


if __name__ == "__main__":
    sys.exit(main())
