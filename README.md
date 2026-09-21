# Email classifier (Jev)

Triage an inbox with [TypeSafe Jev](https://openrouter.ai/~typesafe/jev-latest) via [OpenRouter](https://openrouter.ai/docs/guides/community/typesafe-sdk): each message is scored as **needs attention**, **review**, or **not important**.

Gmail is connected with **OAuth + the Gmail API**. You sign in in the browser (2FA included). No Google password or App Password is stored.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1. Create an OpenRouter API key at [openrouter.ai/keys](https://openrouter.ai/keys) and set `OPENROUTER_API_KEY`.
2. Connect Gmail:
   - [Create a Google Cloud project](https://console.cloud.google.com/)
   - [Enable the Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)
   - OAuth consent screen: External is fine; add your Gmail as a test user
   - Credentials → Create OAuth client ID → **Desktop app** → download JSON as `credentials.json` in this folder
   - `python classify.py --login` (browser opens; complete 2FA as usual)

A `token.json` file is saved locally and refreshed automatically.

Optional: set `RECIPIENT_PRIORITIES` in `.env` so Jev knows what you personally care about.

## Run

```bash
# Sample emails, no inbox required
python classify.py --demo

# Sign in / confirm Gmail OAuth
python classify.py --login
python classify.py --check-gmail

# Last 30 unread Primary messages
python classify.py --unread --limit 30

# A specific Gmail tab (the 8k+ is Updates, not Inbox unread)
python classify.py --unread --folder updates --limit 30

# Star + label messages that need attention
python classify.py --unread --flag

# Local .eml files
python classify.py --eml-dir ./inbox
```

`--json` prints machine-readable output. `--output report.json` writes the same payload to disk.
