"""The evening email: every parliamentary committee meeting in the week ahead.

Once a day, on the first run after 7 pm India time, both Houses' committee
meeting lists are read and everything called for the next seven days
(tomorrow onwards) is sent as one email, grouped by day, with the time,
the committee, what it is taking up, the room, and the notice.

Meetings that were not in the previous evening's email are marked NEW, so
a quick look says what has been called since yesterday.

It goes to the addresses in the PARLIAMENT_WEEK_TO secret, and does nothing
until that secret exists. It is sent every evening, including when nothing
is listed, so a missing email always means something is wrong.

If Parliament's website is down at 7 pm, the next run tries again, up to
midnight. The meetings are read from the same lists the Parliament watch
uses (sansad.py); this only adds the looking-ahead.

    python -m tracker.parliament_week --dry-run    print it, send nothing
    python -m tracker.parliament_week --now        send now, whatever the time
"""

import datetime as dt
import html
import json
import pathlib
import sys

from . import email_out, sansad

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "state.json"
RECIPIENT_ENV = "PARLIAMENT_WEEK_TO"
SEND_FROM_HOUR = 19          # 7 pm, India time
DAYS_AHEAD = 7
AGENDA_CHARS = 450           # enough for the subject; the notice has the rest

# The Lok Sabha list mixes dates, so the week ahead can sit fifty rows down.
LS_ROWS = 200
RS_ROWS = 100


def _clock(text: str) -> str:
    """"1100 hrs", "1030 hrs. onwards", "11:00 AM" -> a time to sort by."""
    digits = "".join(ch for ch in text if ch.isdigit())[:4]
    if "PM" in text.upper() and len(digits) >= 3:
        hour = int(digits[:-2]) % 12 + 12
        return f"{hour:02d}{digits[-2:]}"
    return digits.zfill(4) if digits else "9999"


def _meetings(today: dt.date) -> list:
    """Every meeting from tomorrow to DAYS_AHEAD days out, both Houses."""
    rows = sansad._ls_meetings(LS_ROWS) + sansad._rs_meetings(RS_ROWS)
    last = today + dt.timedelta(days=DAYS_AHEAD)
    out = []
    for row in rows:
        if not (today < row["date"] <= last):
            continue
        parts = [p.strip() for p in row["detail"].split("|")]
        if row["key"].startswith("meeting:RS"):
            # "Rajya Sabha | 11:00 AM | Committee Room - 3, ..."
            house, time, venue = "Rajya Sabha", parts[1:2], parts[2:]
            committee = row["title"].rsplit(" meets on ", 1)[0]
        else:
            # "Financial Committees | 1500 hrs | Committee Room C, ..."
            house, time, venue = "Lok Sabha", parts[1:2], parts[2:]
            committee = row["title"].rsplit(" meets on ", 1)[0]
            if "committee" not in committee.lower():
                committee = f"Committee on {committee}"
        out.append({
            "key": row["key"], "date": row["date"], "house": house,
            "committee": committee,
            "time": time[0] if time else "",
            "venue": " | ".join(venue),
            "agenda": " ".join(row["lines"]),
            "url": row["url"],
        })
    out.sort(key=lambda m: (m["date"], _clock(m["time"]), m["house"], m["committee"]))
    return out


def _short(text: str) -> str:
    if len(text) <= AGENDA_CHARS:
        return text
    return text[:AGENDA_CHARS].rsplit(" ", 1)[0] + " ..."


def build_email(meetings: list, today: dt.date, before: set) -> tuple:
    new = sum(1 for m in meetings if m["key"] not in before)
    last = today + dt.timedelta(days=DAYS_AHEAD)
    span = f"{(today + dt.timedelta(days=1)).strftime('%d %b')} to {last.strftime('%d %b')}"
    if meetings:
        subject = (f"Parliament committees, week ahead: {len(meetings)} meeting"
                   f"{'s' if len(meetings) != 1 else ''} ({span})"
                   + (f", {new} new" if before and new else ""))
    else:
        subject = f"Parliament committees, week ahead: none listed ({span})"

    font = "-apple-system,Segoe UI,sans-serif"
    days = []
    for day in sorted({m["date"] for m in meetings}):
        rows = []
        for m in (m for m in meetings if m["date"] == day):
            badge = ""
            if before and m["key"] not in before:
                badge = ('<span style="background:#15803d;color:#fff;font:600 10px/1 '
                         f'{font};letter-spacing:.08em;padding:3px 6px;border-radius:3px;'
                         'margin-left:8px;">NEW</span>')
            house_colour = "#7c2d12" if m["house"] == "Rajya Sabha" else "#14532d"
            rows.append(f"""
            <div style="padding:12px 0;border-top:1px solid #e5e7eb;">
              <div style="font:600 12px/1.4 {font};color:#6b7280;">
                {html.escape(m["time"] or "time not given")}
                &nbsp;&middot;&nbsp;<span style="color:{house_colour};">{m["house"]}</span>{badge}</div>
              <div style="font:600 15px/1.4 {font};color:#111827;margin-top:3px;">
                {html.escape(m["committee"])}</div>
              {f'<div style="font:400 14px/1.55 {font};color:#374151;margin-top:4px;">{html.escape(_short(m["agenda"]))}</div>' if m["agenda"] else ''}
              <div style="font:400 12px/1.5 {font};color:#6b7280;margin-top:4px;">
                {html.escape(m["venue"])}{' &nbsp;&middot;&nbsp; ' if m["venue"] else ''}<a href="{html.escape(m["url"].replace(" ", "%20"))}"
                style="color:#1d4ed8;text-decoration:none;">Notice &rarr;</a></div>
            </div>""")
        days.append(f"""
        <div style="margin-bottom:22px;">
          <div style="font:700 13px/1 {font};letter-spacing:.06em;text-transform:uppercase;
                      color:#111827;margin-bottom:6px;">{day.strftime("%A, %d %B")}</div>
          {''.join(rows)}
        </div>""")

    if not meetings:
        days.append(f'<div style="font:400 15px/1.55 {font};color:#374151;">'
                    'No committee meetings are listed on sansad.in for the next seven days.</div>')

    body = f"""<div style="max-width:640px;margin:0 auto;padding:24px 20px;background:#fff;">
      <div style="font:600 11px/1 {font};letter-spacing:.12em;color:#6b7280;
                  text-transform:uppercase;margin-bottom:6px;">Parliament &middot; week ahead</div>
      <div style="font:600 18px/1.35 {font};color:#111827;margin-bottom:20px;">
        Committee meetings, {span}</div>
      {''.join(days)}
      <div style="color:#9ca3af;font:400 12px/1.5 {font};margin-top:20px;
                  border-top:1px solid #e5e7eb;padding-top:12px;">
        Both Houses, from the committee meeting lists on sansad.in. Sent every
        evening at 7 pm.{' NEW means it was not in yesterday&#39;s email.' if before and new else ''}
        Committees sometimes add or move a meeting at short notice.
      </div>
    </div>"""
    return subject, body


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def run(dry_run: bool, now_anyway: bool) -> int:
    if not email_out.recipient_setting(RECIPIENT_ENV) and not dry_run:
        print(f"{RECIPIENT_ENV} is not set; the week-ahead email is switched off.")
        return 0

    now = dt.datetime.now(sansad.IST)
    today = now.date()
    state = _load_state()
    mine = state.get("parliament_week") or {}
    if not now_anyway and not dry_run:
        if now.hour < SEND_FROM_HOUR:
            return 0
        if mine.get("sent_on") == today.isoformat():
            return 0

    try:
        meetings = _meetings(today)
    except Exception as exc:                                      # noqa: BLE001
        # Tried again on the next run; nothing is marked as sent.
        print(f"Could not read the committee meeting lists: {exc}")
        return 0

    before = set(mine.get("keys") or [])
    subject, body = build_email(meetings, today, before)
    print(f"{subject}")
    if dry_run:
        for m in meetings:
            print(f"  {m['date']} {m['time']:>12} {m['house']:<11} {m['committee']}")
        return 0

    try:
        email_out.send(subject, body, recipient_env=RECIPIENT_ENV,
                       sender_name="Parliament Week Ahead")
    except Exception as exc:                                      # noqa: BLE001
        print(f"Could not send the week-ahead email: {exc}")
        return 0

    # Loaded again just before writing, so nothing another part of the
    # tracker wrote in the meantime is lost.
    state = _load_state()
    state["parliament_week"] = {"sent_on": today.isoformat(),
                                "keys": sorted(m["key"] for m in meetings)}
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run(dry_run="--dry-run" in sys.argv, now_anyway="--now" in sys.argv))
