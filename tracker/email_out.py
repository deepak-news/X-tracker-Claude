"""Sends the alert email through Gmail."""

import html
import os
import smtplib
from email.message import EmailMessage

SENDER_ENV = "GMAIL_USER"
PASSWORD_ENV = "GMAIL_APP_PASSWORD"
RECIPIENT_ENV = "MAIL_TO"


def _credentials():
    sender = os.environ.get(SENDER_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "").strip()
    recipient = os.environ.get(RECIPIENT_ENV, "").strip()
    missing = [
        name
        for name, value in (
            (SENDER_ENV, sender),
            (PASSWORD_ENV, password),
            (RECIPIENT_ENV, recipient),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing email settings: {', '.join(missing)}")
    # Gmail app passwords are shown with spaces; they must be sent without.
    return sender, password.replace(" ", ""), recipient


def send(subject: str, body_html: str) -> None:
    sender, password, recipient = _credentials()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"X Tracker <{sender}>"
    message["To"] = recipient
    message.set_content("This email needs an HTML-capable reader.")
    message.add_alternative(body_html, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(sender, password)
        server.send_message(message)


def build_digest(items, unscreened=None) -> tuple[str, str]:
    """items = [(post, score, headline, why), ...] already sorted, best first.

    unscreened = posts the AI could not judge. They are listed raw at the
    bottom so a failure never turns into a story you never heard about.
    """
    unscreened = unscreened or []
    if items:
        top = items[0]
        subject = f"[{int(top[1])}/10] {top[2]}" if len(items) == 1 else \
                  f"{len(items)} newsworthy posts — top: {top[2]}"
    else:
        n = len(unscreened)
        subject = f"{n} post{'' if n == 1 else 's'} need{'s' if n == 1 else ''} a manual look"

    blocks = []
    for post, score_value, headline, why in items:
        blocks.append(f"""
        <div style="margin:0 0 28px;padding:18px 20px;border:1px solid #e3e3e3;border-radius:10px;">
          <div style="font:600 12px/1.4 -apple-system,Segoe UI,sans-serif;color:#666;">
            @{html.escape(post.handle)} &middot; scored {score_value:.0f}/10
          </div>
          <div style="font:700 17px/1.35 -apple-system,Segoe UI,sans-serif;color:#111;margin:6px 0 10px;">
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
                    color:#1d6ef5;text-decoration:none;">Open on X &rarr;</a>
        </div>""")

    if unscreened:
        rows = "".join(
            f'<li style="margin-bottom:6px;"><a href="{html.escape(p.url)}" '
            f'style="color:#1d6ef5;text-decoration:none;">@{html.escape(p.handle)}</a>'
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
        X Tracker
      </div>
      {''.join(blocks)}
      <div style="font:400 12px -apple-system,Segoe UI,sans-serif;color:#999;margin-top:8px;">
        Too much or too little? Change <code>threshold</code> or <code>newsworthy</code> in watchlist.yml.
      </div>
    </div>"""
    return subject, body
