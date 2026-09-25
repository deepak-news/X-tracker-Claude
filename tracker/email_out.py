"""Sends the alert email through Gmail."""

import html
import json
import os
import pathlib
import time

import yaml

from . import sources
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

SENDER_ENV = "GMAIL_USER"
PASSWORD_ENV = "GMAIL_APP_PASSWORD"
RECIPIENT_ENV = "MAIL_TO"
# Maintenance mail -- "the tracker has stopped", the watchdog -- is for
# whoever looks after the tracker, not for everyone on the alert lists.
# Until this secret exists it falls back to MAIL_TO.
ADMIN_ENV = "ADMIN_MAIL_TO"

WATCHLIST = pathlib.Path(__file__).resolve().parent.parent / "watchlist.yml"
DEFAULT_NAMES = {
    "tech_desk": "For Tech Desk",
    "government_watch": "DoPT, MHA & ED Watch",
    "national_desk": "Alerts for Tweets",
    "maintenance": "Tracker Maintenance",
}


def mail_name(kind: str) -> str:
    """The name recipients see for one kind of email, from watchlist.yml."""
    try:
        names = (yaml.safe_load(WATCHLIST.read_text()) or {}).get("mail_names") or {}
    except (OSError, yaml.YAMLError):
        names = {}
    return str(names.get(kind) or DEFAULT_NAMES.get(kind) or "Alerts")


# Reporters' own gazette lists all live in ONE secret, a line per desk:
#     law: someone@pti.in, other@gmail.com
# and a watch points at a line as "GAZETTE_DESKS:law". Adding a reporter
# then means editing that secret and watchlist.yml, never the workflow.
DESKS_ENV = "GAZETTE_DESKS"


def recipient_setting(name: str) -> str:
    """A recipient list by name, or "" if it is not set."""
    if ":" not in name:
        return os.environ.get(name, "").strip()
    secret, desk = name.split(":", 1)
    for line in os.environ.get(secret, "").splitlines():
        label, _, addresses = line.partition(":")
        if label.strip().lower() == desk.strip().lower():
            return addresses.strip()
    return ""


def _credentials(recipient_env: str = RECIPIENT_ENV):
    """The sending account, plus whichever recipient list was asked for.

    One mailbox sends everything, but different kinds of alert can go to
    different people -- the news digest to MAIL_TO, the government watch to
    WATCH_MAIL_TO -- so the recipient list is chosen by the caller.
    """
    sender = os.environ.get(SENDER_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "").strip()
    recipient = recipient_setting(recipient_env)
    if not recipient and recipient_env == ADMIN_ENV:
        recipient = os.environ.get(RECIPIENT_ENV, "").strip()
    # The list may hold several addresses, separated by commas or newlines.
    recipients = [a.strip() for a in recipient.replace("\n", ",").split(",") if a.strip()]

    missing = [
        name
        for name, value in (
            (SENDER_ENV, sender),
            (PASSWORD_ENV, password),
            (recipient_env, recipients),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing email settings: {', '.join(missing)}")
    # Gmail app passwords are shown with spaces; they must be sent without.
    return sender, password.replace(" ", ""), recipients


# ------------------------------------------------------------ sending budget
#
# Gmail lets one personal account reach about 500 recipients in any rolling
# 24 hours. Every email counts once per person on its list. Past that, Gmail
# refuses to send anything from the account for up to a day -- every alert
# type at once, and the warning about it too.
#
# So every email sent is written down here, and before sending, the last 24
# hours are added up. It lives in its own file rather than state.json: each
# part of the tracker loads state.json when it starts and writes it back when
# it ends, which would quietly wipe out anything written in between.

MAIL_LOG = WATCHLIST.parent / "mail_log.json"
DAY = 24 * 60 * 60
# Which list an email went to decides what it is, for the counter's purposes.
LIST_NAMES = {RECIPIENT_ENV: "Tech Desk", "WATCH_MAIL_TO": "DoPT, MHA & ED Watch",
              "GAZETTE_MAIL_TO": "Gazette Alerts", "SOCIAL_MAIL_TO": "Tweets",
              "GAZETTE_DESKS:law": "Law Ministry Gazettes",
              "GAZETTE_DESKS:parliament": "Parliament Watch",
              "PARLIAMENT_WEEK_TO": "Parliament Week Ahead",
              "MHA_FINANCE_MAIL_TO": "MHA & Finance Gazettes",
              ADMIN_ENV: "Maintenance"}


class BudgetFull(RuntimeError):
    """Sending this would run the account into Gmail's daily limit."""


def budget_settings() -> dict:
    try:
        cfg = (yaml.safe_load(WATCHLIST.read_text()) or {}).get("mail_budget") or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    return {"limit": int(cfg.get("daily_limit", 500)),
            "warn": int(cfg.get("warn_at", 400)),
            "keep_free": int(cfg.get("keep_free", 30))}


def _load_log() -> dict:
    try:
        log = json.loads(MAIL_LOG.read_text())
    except (OSError, ValueError):
        log = {}
    cutoff = time.time() - DAY
    log["sent"] = [e for e in log.get("sent") or [] if e.get("at", 0) >= cutoff]
    return log


def _save_log(log: dict) -> None:
    MAIL_LOG.write_text(json.dumps(log, indent=1, sort_keys=True) + "\n")


def used_today() -> int:
    """Recipients reached in the last 24 hours, from this machine's log."""
    return sum(int(e.get("n", 0)) for e in _load_log()["sent"])


def pressure(recipient_env: str = RECIPIENT_ENV) -> str:
    """How freely an email to this list can go now.

    "normal" -- plenty of room.
    "tight"  -- past the warning line: alerts that go to big lists should
                carry only their MAJOR items (9 or 10 out of 10).
    "full"   -- sending would eat into the part kept free: hold everything
                except maintenance mail, and try again later.
    """
    try:
        planned = len(_credentials(recipient_env)[2])
    except RuntimeError:
        planned = 1
    settings, used = budget_settings(), used_today()
    if used + planned > settings["limit"] - settings["keep_free"]:
        return "full"
    if used + planned > settings["warn"]:
        return "tight"
    return "normal"


def _record(recipient_env: str, count: int) -> None:
    log = _load_log()
    log["sent"].append({"at": int(time.time()), "n": count, "list": recipient_env})
    _save_log(log)
    used = sum(int(e["n"]) for e in log["sent"])
    settings = budget_settings()
    if used >= settings["warn"] and log.get("warned", 0) < time.time() - DAY \
            and recipient_env != ADMIN_ENV:
        log["warned"] = int(time.time())
        _save_log(log)
        _warn(log["sent"], used, settings)


def _warn(sent: list, used: int, settings: dict) -> None:
    by_list: dict = {}
    for entry in sent:
        name = LIST_NAMES.get(entry.get("list"), entry.get("list"))
        emails, people = by_list.get(name, (0, 0))
        by_list[name] = (emails + 1, people + int(entry["n"]))
    rows = "".join(f"<tr><td style='padding:4px 12px 4px 0;'>{html.escape(name)}</td>"
                   f"<td style='padding:4px 12px;text-align:right;'>{emails}</td>"
                   f"<td style='padding:4px 0;text-align:right;'>{people}</td></tr>"
                   for name, (emails, people) in sorted(by_list.items(), key=lambda kv: -kv[1][1]))
    try:
        send(f"Email budget: {used} of {settings['limit']} used in the last 24 hours",
             f"""<div style="max-width:600px;margin:0 auto;padding:24px;
                  font:400 15px/1.6 -apple-system,Segoe UI,sans-serif;color:#222;">
               <p><strong>The tracker has reached {used} of Gmail's {settings['limit']}
               recipients for the last 24 hours.</strong></p>
               <p>From now until the count falls, the Tech Desk and Tweets emails carry
               only their MAJOR items (9 or 10 out of 10), and the hourly digest waits.
               The DoPT, MHA &amp; ED watch and Gazette Alerts carry on as normal. At
               {settings['limit'] - settings['keep_free']}, everything except maintenance
               mail is held and retried later.</p>
               <table style="border-collapse:collapse;font-size:14px;margin:12px 0;">
                 <tr style="color:#888;"><td style="padding:4px 12px 4px 0;">List</td>
                 <td style="padding:4px 12px;text-align:right;">Emails</td>
                 <td style="padding:4px 0;text-align:right;">Recipients</td></tr>{rows}
               </table>
               <p style="color:#888;font-size:13px;">The count only covers emails sent
               from GitHub; the DoPT check on the Mac is covered by the
               {settings['keep_free']} kept free. To stay further from the limit, move
               the biggest list onto a Google Group, which counts as one recipient.</p>
             </div>""",
             recipient_env=ADMIN_ENV, sender_name=mail_name("maintenance"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not send the email-budget warning: {exc}")


def send(subject: str, body_html: str, recipient_env: str = RECIPIENT_ENV,
         sender_name: str = "") -> None:
    sender, password, recipients = _credentials(recipient_env)
    if recipient_env != ADMIN_ENV and pressure(recipient_env) == "full":
        raise BudgetFull(f"{used_today()} recipients already reached in the last "
                         f"24 hours; holding this email to stay clear of Gmail's limit")

    message = EmailMessage()
    message["Subject"] = subject
    # formataddr quotes the name when it needs it: "DoPT, MHA & ED Watch" has
    # a comma, which an unquoted header would read as two separate senders.
    shown_as = formataddr((sender_name or mail_name("tech_desk"), sender))
    message["From"] = shown_as
    # Everyone on a list is blind-copied, so no recipient sees the others'
    # addresses. The visible "To" is the sending account itself.
    message["To"] = shown_as
    message["Bcc"] = ", ".join(recipients)
    message.set_content("This email needs an HTML-capable reader.")
    message.add_alternative(body_html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(sender, password)
        # Delivered to the list only. Without to_addrs the visible "To" --
        # the sending account -- would get a copy of every email too, and
        # count against Gmail's daily limit.
        server.send_message(message, to_addrs=recipients)
    print(f"  sent to {len(recipients)} recipient(s): {', '.join(recipients)}")
    _record(recipient_env, len(recipients))


def build_digest(items, unscreened=None) -> tuple[str, str]:
    """items = [(post, score, headline, why), ...], best first.

    Primary sources are shown before news reports: a filing or a ministry
    release IS the event, while an article is somebody's account of it.
    """
    unscreened = unscreened or []
    if items:
        # The subject carries the single most newsworthy thing, whatever its
        # source, breaking ties towards the more authoritative one.
        top = sorted(items, key=lambda r: (-r[1], sources.tier(r[0].handle)))[0]
        subject = f"[{int(top[1])}/10] {top[2]}" if len(items) == 1 else \
                  f"{len(items)} newsworthy — top: {top[2]}"
    else:
        n = len(unscreened)
        subject = f"{n} item{'' if n == 1 else 's'} need{'s' if n == 1 else ''} a manual look"

    tint = {
        sources.TIER_FILING: ("#0b6b3a", "#e7f5ec"),
        sources.TIER_GOVERNMENT: ("#8a4b00", "#fdf0e0"),
        sources.TIER_COMPANY: ("#1d4ed8", "#e8efff"),
        sources.TIER_NEWS: ("#555", "#eee"),
    }

    grouped: dict[int, list] = {}
    for row in items:
        grouped.setdefault(sources.tier(row[0].handle), []).append(row)

    blocks = []
    for level in sorted(grouped):
        ink, wash = tint[level]
        blocks.append(f"""
        <div style="font:700 11px -apple-system,Segoe UI,sans-serif;color:#888;
                    letter-spacing:.1em;text-transform:uppercase;margin:26px 0 12px;">
          {html.escape(sources.TIER_NAMES[level])}
        </div>""")
        for post, score_value, headline, why in sorted(grouped[level], key=lambda r: -r[1]):
            kind, where = sources.describe(post)
            blocks.append(f"""
        <div style="margin:0 0 14px;padding:18px 20px;border:1px solid #e3e3e3;border-radius:10px;">
          <div style="margin-bottom:10px;">
            <span style="display:inline-block;background:{wash};color:{ink};
                         font:700 10px -apple-system,Segoe UI,sans-serif;letter-spacing:.08em;
                         padding:4px 8px;border-radius:4px;">{html.escape(kind)}</span>
            <span style="font:600 12px -apple-system,Segoe UI,sans-serif;color:#333;
                         margin-left:8px;">{html.escape(where)}</span>
            <span style="font:400 12px -apple-system,Segoe UI,sans-serif;color:#888;
                         margin-left:8px;">scored {score_value:.0f}/10</span>
          </div>
          <div style="font:700 17px/1.35 -apple-system,Segoe UI,sans-serif;color:#111;margin:0 0 10px;">
            {html.escape(headline)}
          </div>
          <div style="font:400 15px/1.55 -apple-system,Segoe UI,sans-serif;color:#222;white-space:pre-wrap;">
            {html.escape(post.text)}
          </div>
          <a href="{html.escape(post.url)}"
             style="display:inline-block;margin-top:12px;font:600 13px -apple-system,Segoe UI,sans-serif;
                    color:#1d6ef5;text-decoration:none;">Read it in full &rarr;</a>
        </div>""")

    if unscreened:
        rows = "".join(
            f'<li style="margin-bottom:6px;"><a href="{html.escape(p.url)}" '
            f'style="color:#1d6ef5;text-decoration:none;">{html.escape(p.handle)}</a>'
            f' &mdash; {html.escape(p.text[:110])}...</li>'
            for p in unscreened
        )
        blocks.append(f"""
        <div style="margin:0 0 28px;padding:18px 20px;border:1px solid #e6c200;
                    border-radius:10px;background:#fffdf2;">
          <div style="font:700 14px -apple-system,Segoe UI,sans-serif;color:#8a6d00;margin-bottom:10px;">
            {len(unscreened)} post{'' if len(unscreened) == 1 else 's'} could not be screened &mdash; check {'this' if len(unscreened) == 1 else 'these'} yourself
          </div>
          <ul style="margin:0;padding-left:18px;font:400 13px/1.5 -apple-system,Segoe UI,sans-serif;color:#333;">
            {rows}
          </ul>
        </div>""")

    body = f"""<div style="max-width:640px;margin:0 auto;padding:24px 16px;background:#fff;">
      <div style="font:600 13px -apple-system,Segoe UI,sans-serif;color:#888;
                  letter-spacing:.06em;text-transform:uppercase;margin-bottom:18px;">
        {html.escape(mail_name("tech_desk"))}
      </div>
      {''.join(blocks)}
      <div style="font:400 12px -apple-system,Segoe UI,sans-serif;color:#999;margin-top:8px;">
        Machine-screened from company filings, newsrooms and official releases. Check the original before filing.
      </div>
    </div>"""
    return subject, body
