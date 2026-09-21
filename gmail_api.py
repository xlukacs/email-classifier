"""Gmail API + OAuth 2.0 — no account password, 2FA happens in the browser."""

from __future__ import annotations

import base64
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

CATEGORY_QUERIES = {
    "inbox": "in:inbox",
    "primary": "category:primary",
    "social": "category:social",
    "promotions": "category:promotions",
    "updates": "category:updates",
    "forums": "category:forums",
    "purchases": "category:purchases",
    "starred": "is:starred",
    "important": "is:important",
    "unread": "is:unread",
}

# Gmail UI left-nav order, plus the system UNREAD label.
SIDEBAR_LABELS = (
    ("INBOX", "Inbox"),
    ("CATEGORY_PERSONAL", "Primary"),
    ("CATEGORY_SOCIAL", "Social"),
    ("CATEGORY_UPDATES", "Updates"),
    ("CATEGORY_PROMOTIONS", "Promotions"),
    ("CATEGORY_FORUMS", "Forums"),
    ("STARRED", "Starred"),
    ("IMPORTANT", "Important"),
    ("DRAFT", "Drafts"),
    ("UNREAD", "Unread (all mail)"),
)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

SETUP_HELP = """
Gmail is connected with OAuth, not a password.

1. Open https://console.cloud.google.com/ and create (or pick) a project
2. Enable the Gmail API:
   https://console.cloud.google.com/apis/library/gmail.googleapis.com
3. Configure the OAuth consent screen (External is fine for a personal account).
   Add your own Gmail address as a test user.
4. Create credentials → OAuth client ID → Application type: Desktop app
5. Download the JSON and save it as credentials.json in this folder
6. Run: python classify.py --login

Google will open a browser. Sign in with 2FA as usual. A token.json file is
stored locally and refreshed automatically — your password is never saved.
""".strip()


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in {"p", "br", "div", "tr", "li", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        collapsed = re.sub(r"[ \t]+", " ", "".join(self._parts))
        return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def _project_path(env_name: str, default: str) -> Path:
    raw = os.environ.get(env_name, default).strip() or default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def credentials_path() -> Path:
    return _project_path("GMAIL_CREDENTIALS", "credentials.json")


def token_path() -> Path:
    return _project_path("GMAIL_TOKEN", "token.json")


def _save_token(creds: Credentials) -> None:
    token_path().write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(token_path(), 0o600)
    except OSError:
        pass


def load_credentials(*, interactive: bool = True) -> Credentials:
    creds_file = credentials_path()
    token_file = token_path()
    creds: Credentials | None = None

    if token_file.is_file():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except RefreshError:
            creds = None

    if not interactive:
        raise SystemExit(
            "Gmail OAuth token is missing or expired. Run: python classify.py --login"
        )

    if not creds_file.is_file():
        raise SystemExit(
            f"Missing {creds_file.name}. {SETUP_HELP}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    _save_token(creds)
    return creds


def gmail_service(*, interactive: bool = True) -> Resource:
    creds = load_credentials(interactive=interactive)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(payload: dict[str, Any], name: str) -> str:
    target = name.lower()
    for item in payload.get("headers") or []:
        if str(item.get("name", "")).lower() == target:
            return str(item.get("value") or "")
    return ""


def _decode_body(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _walk_parts(part: dict[str, Any]) -> tuple[str, str]:
    plain = ""
    html = ""
    mime = str(part.get("mimeType") or "")
    filename = str(part.get("filename") or "")
    body = part.get("body") or {}
    if filename:
        pass
    elif mime == "text/plain":
        plain += _decode_body(body.get("data"))
    elif mime == "text/html":
        html += _decode_body(body.get("data"))
    for child in part.get("parts") or []:
        child_plain, child_html = _walk_parts(child)
        plain += child_plain
        html += child_html
    return plain, html


def _body_from_payload(payload: dict[str, Any], *, limit: int) -> str:
    plain, html = _walk_parts(payload)
    text = plain.strip() or html_to_text(html)
    if len(text) > limit:
        text = text[:limit] + "\n…[truncated]"
    return text


def _gmail_query(*, unread: bool, since_days: int | None, folder: str) -> str:
    terms: list[str] = []
    folder_key = (folder or "primary").strip()
    mapped = CATEGORY_QUERIES.get(folder_key.lower())
    if mapped:
        terms.append(mapped)
    elif folder_key.upper() == "INBOX":
        terms.append("in:inbox")
    else:
        terms.append(f"label:{folder_key}")
    if unread and folder_key.lower() not in {"unread"}:
        terms.append("is:unread")
    if since_days is not None:
        terms.append(f"newer_than:{since_days}d")
    return " ".join(terms)


def list_message_ids(
    service: Resource,
    *,
    unread: bool,
    limit: int,
    since_days: int | None,
    folder: str,
) -> list[str]:
    query = _gmail_query(unread=unread, since_days=since_days, folder=folder)
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < limit:
        request = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(100, limit - len(ids)),
                pageToken=page_token,
            )
        )
        response = request.execute()
        for item in response.get("messages") or []:
            ids.append(item["id"])
            if len(ids) >= limit:
                break
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return ids


def get_message(service: Resource, message_id: str, *, body_limit: int) -> dict[str, str]:
    raw = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    payload = raw.get("payload") or {}
    return {
        "id": str(raw.get("id") or message_id),
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "date": _header(payload, "Date"),
        "subject": _header(payload, "Subject") or "(no subject)",
        "body": _body_from_payload(payload, limit=body_limit),
    }


def fetch_inbox(
    *,
    unread: bool,
    limit: int,
    since_days: int | None,
    folder: str,
    body_limit: int,
) -> list[dict[str, str]]:
    service = gmail_service(interactive=True)
    ids = list_message_ids(
        service, unread=unread, limit=limit, since_days=since_days, folder=folder
    )
    return [get_message(service, message_id, body_limit=body_limit) for message_id in ids]


def check_connection() -> None:
    service = gmail_service(interactive=True)
    profile = service.users().getProfile(userId="me").execute()
    address = profile.get("emailAddress", "(unknown)")
    labels = _label_counts(service)
    shown_ids: set[str] = set()
    lines = [
        f"Connected to Gmail as {address} via OAuth (Gmail API).",
        "",
        f"{'':22} {'unread':>10}  {'total':>10}",
    ]
    for label_id, title in SIDEBAR_LABELS:
        raw = labels.get(label_id)
        if not raw:
            continue
        shown_ids.add(label_id)
        lines.append(
            f"{title:22} {_format_count(raw.get('messagesUnread')):>10}  "
            f"{_format_count(raw.get('messagesTotal')):>10}"
        )

    extras: list[tuple[int, str, dict[str, Any]]] = []
    for label_id, raw in labels.items():
        if label_id in shown_ids or raw.get("type") == "system":
            continue
        unread = int(raw.get("messagesUnread") or 0)
        if unread <= 0:
            continue
        extras.append((unread, str(raw.get("name") or label_id), raw))
    extras.sort(reverse=True)
    for _, name, raw in extras[:12]:
        lines.append(
            f"{name:22} {_format_count(raw.get('messagesUnread')):>10}  "
            f"{_format_count(raw.get('messagesTotal')):>10}"
        )

    lines.extend(
        [
            "",
            "Those unread numbers are the same counts Gmail shows in the sidebar.",
            "Classification defaults to Primary (`--folder primary`). Other tabs:",
            "  --folder inbox | social | updates | promotions | forums",
            f"Token stored at {token_path()} — password was not saved.",
        ]
    )
    print("\n".join(lines))


def _label_counts(service: Resource) -> dict[str, dict[str, Any]]:
    listed = service.users().labels().list(userId="me").execute().get("labels") or []
    by_id: dict[str, dict[str, Any]] = {}
    extra_names = {
        "purchases",
        "vásárlások",
        "vasarlasok",
        "travel",
        "utazás",
        "utazas",
    }
    wanted = {label_id for label_id, _ in SIDEBAR_LABELS}
    for label in listed:
        label_id = str(label.get("id") or "")
        name = str(label.get("name") or "").casefold()
        if label_id in wanted or name in extra_names:
            wanted.add(label_id)
    for label_id in wanted:
        try:
            by_id[label_id] = (
                service.users().labels().get(userId="me", id=label_id).execute()
            )
        except Exception:
            continue
    return by_id


def _format_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def login() -> None:
    load_credentials(interactive=True)
    check_connection()


def _label_id(service: Resource, name: str) -> str:
    existing = service.users().labels().list(userId="me").execute().get("labels") or []
    for label in existing:
        if label.get("name") == name:
            return str(label["id"])
    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    return str(created["id"])


def apply_attention_labels(message_ids: list[str], *, label: str) -> None:
    if not message_ids:
        return
    service = gmail_service(interactive=False)
    add = ["STARRED"]
    if label:
        add.append(_label_id(service, label))
    for message_id in message_ids:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": add},
        ).execute()
