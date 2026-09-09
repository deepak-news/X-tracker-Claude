"""Sends the alert email through Gmail."""

import html
import os

from . import sources
import smtplib
from email.message import EmailMessage

SENDER_ENV = "GMAIL_USER"
PASSWORD_ENV = "GMAIL_APP_PASSWORD"
RECIPIENT_ENV = "MAIL_TO"


def _credentials():
    sender = os.environ.get(SENDER_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "").strip()
    recipient = os.environ.get(RECIPIENT_ENV, "").strip()
    # MAIL_TO may list several addresses, separated by commas or newlines.
    recipients = [a.strip() for a in recipient.replace("\n", ",").split(",") if a.strip()]

    missing = [
        name
        for name, value in (
            (SENDER_ENV, sender),
            (PASSWORD_ENV, password),
            (RECIPIENT_ENV, recipients),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing email settings: {', '.join(missing)}")
    # Gmail app passwords are shown with spaces; they must be sent without.
    return sender, password.replace(" ", ""), recipients


def send(subject: str, body_html: str) -> None:
    sender, password, recipients = _credentials()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"News Tracker <{sender}>"
    message["To"] = ", ".join(recipients)
    message.set_content("This email needs an HTML-capable reader.")
    message.add_alternative(body_html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(sender, password)
        server.send_message(message)
    print(f"  sent to {len(recipients)} recipient(s): {', '.join(recipients)}")


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
          <div style="font:400 13px/1.5 -apple-system,Segoe UI,sans-serif;color:#666;margin-top:10px;">
            {html.escape(why)}
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
        News Tracker
      </div>
      {''.join(blocks)}
      <div style="font:400 12px -apple-system,Segoe UI,sans-serif;color:#999;margin-top:8px;">
        Too much or too little? Change <code>threshold</code> or <code>newsworthy</code> in watchlist.yml.
      </div>
    </div>"""
    return subject, body
