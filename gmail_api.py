"""Gmail API + OAuth 2.0 — no account password, 2FA happens in the browser."""

from __future__ import annotations

import base64
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

_thread_local = threading.local()
DEFAULT_FETCH_WORKERS = 4
MAX_FETCH_WORKERS = 8
# messages.get is 20 units; new projects allow 6,000 units/user/minute.
COST_MESSAGES_GET = 20
COST_MESSAGES_LIST = 5
COST_MESSAGES_MODIFY = 5
COST_LABELS_LIST = 1
COST_LABELS_GET = 1
COST_LABELS_CREATE = 5
COST_GET_PROFILE = 1

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


def _units_per_second() -> float:
    raw = os.environ.get("GMAIL_UNITS_PER_SEC", "80").strip() or "80"
    try:
        value = float(raw)
    except ValueError:
        value = 80.0
    return max(10.0, min(value, 90.0))


class _QuotaLimiter:
    """Stay under Gmail's per-user unit caps (6,000/min, 250/s burst)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens = 0.0
        self._last = time.monotonic()

    def acquire(self, units: int) -> None:
        rate = _units_per_second()
        need = max(1, int(units))
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._last)
                self._tokens = min(rate, self._tokens + elapsed * rate)
                self._last = now
                if self._tokens >= need:
                    self._tokens -= need
                    return
                wait = (need - self._tokens) / rate
            time.sleep(wait)

    def pause(self, seconds: float) -> None:
        delay = max(0.0, seconds)
        with self._lock:
            self._tokens = 0.0
            self._last = time.monotonic() + delay
        if delay:
            time.sleep(delay)


_limiter = _QuotaLimiter()


def _is_rate_limit(exc: HttpError) -> bool:
    if int(getattr(exc.resp, "status", 0) or 0) not in {403, 429}:
        return False
    try:
        payload = exc.content.decode("utf-8", errors="replace").lower()
    except Exception:
        payload = str(exc).lower()
    markers = (
        "ratelimitexceeded",
        "userratelimitexceeded",
        "quotaexceeded",
        "quota exceeded",
        "rate limit",
        "total query cost",
    )
    return any(marker in payload for marker in markers)


def _retry_after_seconds(exc: HttpError, attempt: int) -> float:
    header = ""
    try:
        header = str(exc.resp.get("retry-after") or "").strip()
    except Exception:
        header = ""
    if header:
        try:
            return min(90.0, max(1.0, float(header)))
        except ValueError:
            pass
    return min(90.0, (2**attempt) * 8 + random.uniform(0.0, 4.0))


def _execute(request: Any, *, units: int, attempts: int = 8) -> Any:
    last: HttpError | None = None
    for attempt in range(attempts):
        _limiter.acquire(units)
        try:
            return request.execute()
        except HttpError as exc:
            last = exc
            if not _is_rate_limit(exc) or attempt >= attempts - 1:
                raise
            _limiter.pause(_retry_after_seconds(exc, attempt))
    assert last is not None
    raise last


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


def _thread_service() -> Resource:
    service = getattr(_thread_local, "gmail", None)
    if service is None:
        service = gmail_service(interactive=False)
        _thread_local.gmail = service
    return service


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


def iter_message_id_pages(
    service: Resource,
    *,
    unread: bool,
    limit: int,
    since_days: int | None,
    folder: str,
):
    """Yield (id_page, result_size_estimate) as Gmail list pages arrive."""
    query = _gmail_query(unread=unread, since_days=since_days, folder=folder)
    yielded = 0
    page_token: str | None = None
    uncapped = limit <= 0
    while True:
        remaining = None if uncapped else max(0, limit - yielded)
        if remaining == 0:
            return
        page_size = 100 if remaining is None else min(100, remaining)
        response = _execute(
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=page_size,
                pageToken=page_token,
            ),
            units=COST_MESSAGES_LIST,
        )
        page: list[str] = []
        for item in response.get("messages") or []:
            page.append(item["id"])
            yielded += 1
            if not uncapped and yielded >= limit:
                break
        estimate = response.get("resultSizeEstimate")
        if page:
            yield page, int(estimate) if estimate is not None else None
        if not uncapped and yielded >= limit:
            return
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def list_message_ids(
    service: Resource,
    *,
    unread: bool,
    limit: int,
    since_days: int | None,
    folder: str,
) -> list[str]:
    ids: list[str] = []
    for page, _estimate in iter_message_id_pages(
        service, unread=unread, limit=limit, since_days=since_days, folder=folder
    ):
        ids.extend(page)
    return ids


def get_message(service: Resource, message_id: str, *, body_limit: int) -> dict[str, str]:
    raw = _execute(
        service.users().messages().get(userId="me", id=message_id, format="full"),
        units=COST_MESSAGES_GET,
    )
    payload = raw.get("payload") or {}
    return {
        "thread_id": str(raw.get("threadId") or ""),
        "id": str(raw.get("id") or message_id),
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "date": _header(payload, "Date"),
        "subject": _header(payload, "Subject") or "(no subject)",
        "body": _body_from_payload(payload, limit=body_limit),
    }


def fetch_messages(
    ids: list[str],
    *,
    body_limit: int,
    workers: int = DEFAULT_FETCH_WORKERS,
) -> list[dict[str, str]]:
    if not ids:
        return []
    worker_count = max(1, min(int(workers), len(ids), MAX_FETCH_WORKERS))
    if worker_count == 1:
        service = gmail_service(interactive=True)
        return [get_message(service, message_id, body_limit=body_limit) for message_id in ids]

    def one(message_id: str) -> dict[str, str]:
        return get_message(_thread_service(), message_id, body_limit=body_limit)

    by_id: dict[str, dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(one, message_id) for message_id in ids]
        for future in as_completed(futures):
            message = future.result()
            by_id[message["id"]] = message
    return [by_id[message_id] for message_id in ids if message_id in by_id]


def fetch_inbox(
    *,
    unread: bool,
    limit: int,
    since_days: int | None,
    folder: str,
    body_limit: int,
    workers: int = DEFAULT_FETCH_WORKERS,
) -> list[dict[str, str]]:
    service = gmail_service(interactive=True)
    ids = list_message_ids(
        service, unread=unread, limit=limit, since_days=since_days, folder=folder
    )
    return fetch_messages(ids, body_limit=body_limit, workers=workers)


def mailbox_snapshot() -> dict[str, Any]:
    service = gmail_service(interactive=False)
    profile = _execute(service.users().getProfile(userId="me"), units=COST_GET_PROFILE)
    labels = _label_counts(service)
    tabs: list[dict[str, Any]] = []
    extras: list[dict[str, Any]] = []
    shown: set[str] = set()
    for label_id, title in SIDEBAR_LABELS:
        raw = labels.get(label_id)
        if not raw:
            continue
        shown.add(label_id)
        tabs.append(
            {
                "id": label_id,
                "name": title,
                "unread": int(raw.get("messagesUnread") or 0),
                "total": int(raw.get("messagesTotal") or 0),
            }
        )
    for label_id, raw in labels.items():
        if label_id in shown or raw.get("type") == "system":
            continue
        extras.append(
            {
                "id": label_id,
                "name": str(raw.get("name") or label_id),
                "unread": int(raw.get("messagesUnread") or 0),
                "total": int(raw.get("messagesTotal") or 0),
            }
        )
    extras.sort(key=lambda item: item["unread"], reverse=True)
    return {
        "address": profile.get("emailAddress", "(unknown)"),
        "tabs": tabs,
        "extras": extras[:12],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def check_connection() -> None:
    snap = mailbox_snapshot()
    address = snap.get("address", "(unknown)")
    lines = [
        f"Connected to Gmail as {address} via OAuth (Gmail API).",
        "",
        f"{'':22} {'unread':>10}  {'total':>10}",
    ]
    for tab in snap.get("tabs") or []:
        lines.append(
            f"{str(tab['name']):22} {_format_count(tab.get('unread')):>10}  "
            f"{_format_count(tab.get('total')):>10}"
        )
    for tab in snap.get("extras") or []:
        if int(tab.get("unread") or 0) <= 0:
            continue
        lines.append(
            f"{str(tab['name']):22} {_format_count(tab.get('unread')):>10}  "
            f"{_format_count(tab.get('total')):>10}"
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
    listed = (
        _execute(service.users().labels().list(userId="me"), units=COST_LABELS_LIST).get(
            "labels"
        )
        or []
    )
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
            by_id[label_id] = _execute(
                service.users().labels().get(userId="me", id=label_id),
                units=COST_LABELS_GET,
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
    existing = (
        _execute(service.users().labels().list(userId="me"), units=COST_LABELS_LIST).get(
            "labels"
        )
        or []
    )
    for label in existing:
        if label.get("name") == name:
            return str(label["id"])
    created = _execute(
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ),
        units=COST_LABELS_CREATE,
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
        _execute(
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": add},
            ),
            units=COST_MESSAGES_MODIFY,
        )
