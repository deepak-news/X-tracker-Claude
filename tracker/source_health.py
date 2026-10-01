"""A source that has been failing for a day, reported once a day.

One source going down never stops a run: the others carry on, and the log
names it. That is right for a blip, but it also let BSE refuse every request
for nine days (23 Sep - 1 Oct 2026) without anyone hearing about it. So each
desk notes when every source started failing, and once any has been down for
a day, the maintenance list is emailed -- at most once a day per desk,
naming every source still down.
"""

import datetime as dt
import html

from . import email_out

DOWN_HOURS = 24      # how long a source must have been failing to be reported
REMIND_HOURS = 24    # and how often the reminder repeats while it stays down

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _hours_since(stamp: str, now: dt.datetime) -> float:
    try:
        return (now - dt.datetime.fromisoformat(stamp)).total_seconds() / 3600
    except (TypeError, ValueError):
        return 0.0


def check(record: dict, desk: str, failed, worked=None, where: str = "",
          dry_run: bool = False) -> None:
    """Update one desk's record of failing sources, and warn if it is time.

    record  -- this desk's own corner of state.json, changed in place
    failed  -- names of the sources that failed this run
    worked  -- names read successfully this run; None means every source
               not in "failed" (for desks that read everything every run)
    where   -- where to look for the error, said in the email
    """
    now = dt.datetime.now(dt.timezone.utc)
    down = record.setdefault("down", {})
    failed = set(failed)
    worked = [n for n in down if n not in failed] if worked is None else worked
    for name in worked:
        down.pop(name, None)
    for name in failed:
        down.setdefault(name, now.isoformat())

    long_down = {n: s for n, s in down.items() if _hours_since(s, now) >= DOWN_HOURS}
    if not long_down:
        record.pop("warned", None)      # all recovered: the next outage is news
        return
    if record.get("warned") and _hours_since(record["warned"], now) < REMIND_HOURS:
        return

    names = sorted(long_down, key=lambda n: long_down[n])
    print(f"  sources down for a day or more: {', '.join(names)}")
    if dry_run:
        print("  (dry run) would have emailed the daily warning")
        return

    rows = "".join(
        f"<tr><td style='padding:4px 16px 4px 0;'><strong>{html.escape(n)}</strong></td>"
        f"<td style='padding:4px 0;color:#555;'>failing since "
        f"{dt.datetime.fromisoformat(long_down[n]).astimezone(IST):%d %b, %I:%M %p} "
        f"({_hours_since(long_down[n], now) / 24:.0f} day(s))</td></tr>"
        for n in names)
    try:
        email_out.send(
            f"{desk}: {len(names)} source(s) down for a day -- {', '.join(names)}",
            f"""<div style="max-width:600px;margin:0 auto;padding:24px;
                 font:400 15px/1.6 -apple-system,Segoe UI,sans-serif;color:#222;">
              <p><strong>These sources for the {html.escape(desk)} have failed on
              every run for at least a day.</strong> Nothing from them is reaching
              the alerts until they recover.</p>
              <table style="border-collapse:collapse;">{rows}</table>
              <p>{html.escape(where)}</p>
              <p style="color:#888;font-size:13px;">The other sources are carrying
              on as normal. This reminder repeats once a day while any of these
              stays down, and stops by itself once they recover.</p>
            </div>""",
            recipient_env=email_out.ADMIN_ENV,
            sender_name=email_out.mail_name("maintenance"),
        )
        record["warned"] = now.isoformat()
        print("  emailed the daily warning about sources that are down")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not send the warning about sources that are down: {exc}")
