# X Tracker

Watches ~96 X accounts on the Indian IT / AI / semiconductor beat and emails
when one of them posts something that would need a wire alert.

Runs itself on GitHub Actions every 10 minutes. Costs nothing.

## Editing it

`watchlist.yml` is the only file you need. It holds:

- `newsworthy` - plain-English description of what deserves an email
- `threshold` - 0-10 strictness dial (raise it if the inbox is noisy)
- `priority` - accounts checked directly on every run
- `accounts` - everything being watched

Edit it on GitHub, commit, done. The next run picks it up.

## How it reads X

X charges $200/month for API access, so this uses a burner account's browser
session instead. Two sources, merged and de-duplicated:

1. **An X list** (`X_LIST_ID`) containing every watched account. One cheap
   request covers all of them. A list does not require *following* the
   accounts, which matters because new accounts hit follow limits fast.
2. **A direct sweep** - re-checks the `priority` accounts every run and
   rotates through the rest. Works with no list at all, just slower for the
   long tail.

If `X_LIST_ID` is unset, source 2 carries the whole load and the tracker
still works.

## Secrets it needs

Set under Settings -> Secrets and variables -> Actions.

| Secret | What it is |
|---|---|
| `X_COOKIES` | `auth_token=...; ct0=...` from the burner's browser session |
| `X_LIST_ID` | Number from the burner's X list URL (optional) |
| `GEMINI_API_KEY` | Free key from aistudio.google.com/apikey |
| `GMAIL_USER` | Gmail address that sends the alerts |
| `GMAIL_APP_PASSWORD` | 16-character Google app password |
| `MAIL_TO` | Where alerts are delivered |

## When it breaks

It will, eventually - X actively fights this. You get ONE email saying so
after three consecutive failures, not one every ten minutes.

Almost always the fix is a stale session: log into the burner again, grab
fresh `auth_token` and `ct0` cookies, update the `X_COOKIES` secret.

If that is not it, `tracker/fetch.py` is the only file that knows how X
works. Nothing else needs to change.

## Running it by hand

Actions tab -> "X Tracker" -> "Run workflow". The log shows every post it
considered and the score it gave.
