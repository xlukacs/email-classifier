#!/usr/bin/env python3
"""Triage email with TypeSafe Jev: needs attention vs not important.

Jev is a System One model: you send state + typed questions, it returns
calibrated probabilities — not prose. This script asks several small
questions about each message, then decides in code.

Examples:
  python classify.py --demo
  python classify.py --unread --limit 30
  python classify.py --eml-dir ./inbox --json
"""

from __future__ import annotations

import argparse
import asyncio
import imaplib
import json
import os
import re
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, Sequence

from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    Score,
    TypeSafeError,
)

import gmail_api

Decision = Literal["attention", "review", "skip"]

BODY_CHAR_LIMIT = 8_000
DEFAULT_CONCURRENCY = 8
DEFAULT_THRESHOLD = 0.40
REVIEW_BAND = 0.08
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
DEFAULT_MODEL = "jev-latest"

NOISE_KINDS = {"newsletter", "promo", "notification", "transactional"}
ATTENTION_KINDS = {"direct_person", "work", "calendar", "account"}

QUESTIONS = {
    "from_real_person": Noul(
        instructions=(
            "Is `from` a real person writing a direct message, rather than a "
            "noreply address, mailing list, brand, or automated system?"
        ),
        criteria={
            "true": "A specific human sent this as a personal or professional message.",
            "false": "Automated, marketing, noreply, notifications@, or bulk sender.",
        },
    ),
    "expects_reply": Noul(
        instructions=(
            "Does `body` or `subject` ask the recipient a question, request a "
            "decision, or otherwise expect a reply?"
        ),
        criteria={
            "true": "The sender is waiting on a response.",
            "false": "No reply is expected (FYI, receipt, newsletter, notification).",
        },
    ),
    "action_required": Noul(
        instructions=(
            "Does this email require the recipient to do something besides "
            "reading it — sign, pay, schedule, approve, fix, confirm, or decide?"
        ),
    ),
    "time_sensitive": Noul(
        instructions=(
            "Does `body` or `subject` contain a deadline, meeting time, "
            "expiring obligation, or other time pressure that would cost the "
            "recipient if ignored? Marketing countdown timers do not count."
        ),
    ),
    "is_marketing": Noul(
        instructions=(
            "Is this promotional, a newsletter, a digest, or a sales pitch "
            "rather than a one-to-one message?"
        ),
    ),
    "is_spam_or_phishing": Noul(
        instructions="Is this unsolicited spam, a scam, or a phishing attempt?"
    ),
    "kind": Choice(
        instructions="What kind of email is this?",
        criteria={
            "direct_person": "A human wrote this to the recipient personally.",
            "work": "Professional, client, coworker, or business matter.",
            "calendar": "Meeting invite, scheduling, or calendar update.",
            "account": "Security, billing, legal, or account change that may need action.",
            "transactional": "Order, receipt, shipping, or booking with no problem stated.",
            "notification": "Automated product, social, CI, or system notification.",
            "newsletter": "Newsletter, digest, or subscribed content.",
            "promo": "Marketing, coupon, or sales campaign.",
            "other": "None of the other labels fit well.",
        },
    ),
    "urgency": Score(
        instructions=(
            "How soon should the recipient act on this email, given "
            "`recipient_priorities` if that field is present?"
        ),
        criteria=[
            "Safe to ignore or archive; no action.",
            "Low priority; handle when convenient.",
            "Should handle within a few days.",
            "Should handle today.",
            "Critical; delaying has real cost.",
        ],
    ),
}

URGENCY_LEVELS = QUESTIONS["urgency"].criteria
URGENCY_MAX = len(URGENCY_LEVELS) - 1

SAMPLE_EMAILS = [
    {
        "id": "demo-1",
        "from": "Priya Shah <priya@acme.io>",
        "to": "you@example.com",
        "date": "Mon, 21 Sep 2026 09:14:00 -0700",
        "subject": "Need your sign-off on the vendor contract today",
        "body": (
            "Hey — legal sent back the Acme vendor agreement. They want the "
            "liability cap changed to 12 months of fees. Can you confirm we "
            "are okay with that so I can send it out before 5pm?"
        ),
    },
    {
        "id": "demo-2",
        "from": "Mom <mom@family.example>",
        "to": "you@example.com",
        "date": "Mon, 21 Sep 2026 18:02:00 +0200",
        "subject": "Dinner Sunday?",
        "body": (
            "Are you free this Sunday around 6? I was thinking we could do "
            "pasta if you are in town. Let me know either way."
        ),
    },
    {
        "id": "demo-3",
        "from": "The Batch <batch@deeplearning.ai>",
        "to": "you@example.com",
        "date": "Sat, 19 Sep 2026 08:00:00 -0700",
        "subject": "The Batch: New models, same arguments",
        "body": (
            "This week in AI: a recap of launches, papers, and industry news. "
            "Read the full issue on our site. Unsubscribe at any time."
        ),
    },
    {
        "id": "demo-4",
        "from": "Stripe <receipts@stripe.com>",
        "to": "you@example.com",
        "date": "Sun, 20 Sep 2026 12:11:00 +0000",
        "subject": "Your receipt from TypeSafe AI",
        "body": (
            "You paid $12.00 USD to TypeSafe AI. Payment method: Visa 4242. "
            "This is a receipt for your records. No action needed."
        ),
    },
    {
        "id": "demo-5",
        "from": "GitHub <notifications@github.com>",
        "to": "you@example.com",
        "date": "Mon, 21 Sep 2026 16:40:00 +0000",
        "subject": "[acme/api] A dependency was updated (Dependabot)",
        "body": (
            "Dependabot opened a pull request to bump lodash from 4.17.20 to "
            "4.17.21. View it on GitHub. You are receiving this because you "
            "are watching the repository."
        ),
    },
    {
        "id": "demo-6",
        "from": "Alex Rivera <alex@client.com>",
        "to": "you@example.com",
        "date": "Mon, 21 Sep 2026 14:03:00 -0400",
        "subject": "Production checkout is failing",
        "body": (
            "Customers cannot complete checkout as of ~1:40pm ET. Error is "
            "502 from /api/pay. This is blocking launch tomorrow — can you "
            "look now and reply when you have a read?"
        ),
    },
    {
        "id": "demo-7",
        "from": "NordVPN Deals <deals@marketing.nordvpn.example>",
        "to": "you@example.com",
        "date": "Mon, 21 Sep 2026 07:00:00 +0000",
        "subject": "70% OFF — offer ends tonight",
        "body": (
            "Last chance: 70% off 2-year plans plus 3 extra months free. "
            "Click here to claim your discount. This offer expires at midnight."
        ),
    },
    {
        "id": "demo-8",
        "from": "IT Security <it-security@yourcompany.com>",
        "to": "you@example.com",
        "date": "Mon, 21 Sep 2026 11:22:00 +0000",
        "subject": "Action required: confirm a new sign-in",
        "body": (
            "We detected a new sign-in to your work account from Frankfurt. "
            "If this was you, confirm here. If not, reset your password "
            "immediately. This link expires in 2 hours."
        ),
    },
    {
        "id": "demo-9",
        "from": "PayPal <service@paypal.com.secure-login.example>",
        "to": "you@example.com",
        "date": "Mon, 21 Sep 2026 03:14:00 +0000",
        "subject": "Your account has been limited",
        "body": (
            "We have limited your account. Click http://paypal.com.secure-login.example "
            "/verify immediately or your funds will be frozen. Please enter "
            "your password and 2FA code on the next page."
        ),
    },
]


@dataclass
class Email:
    id: str
    sender: str
    to: str
    date: str
    subject: str
    body: str
    imap_uid: str | None = None
    gmail_id: str | None = None


@dataclass
class Classification:
    email: Email
    decision: Decision
    score: float
    reason: str
    kind: str
    kind_confidence: float
    kind_probabilities: dict[str, float]
    urgency: float
    urgency_confidence: float
    expects_reply: float
    action_required: float
    from_real_person: float
    time_sensitive: float
    is_marketing: float
    is_spam_or_phishing: float
    model: str
    input_tokens: int = 0
    error: str | None = None


@dataclass
class RunStats:
    model: str = ""
    input_tokens: int = 0
    results: list[Classification] = field(default_factory=list)


def load_dotenv_files() -> None:
    for path in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            os.environ.setdefault(key, value)


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for data, charset in decode_header(value):
        if isinstance(data, bytes):
            parts.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(data)
    return "".join(parts).strip()


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


def decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def extract_body(msg: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain":
                plain_parts.append(decode_part(part))
            elif ctype == "text/html":
                html_parts.append(decode_part(part))
    else:
        ctype = msg.get_content_type()
        text = decode_part(msg)
        if ctype == "text/html":
            html_parts.append(text)
        else:
            plain_parts.append(text)
    body = "\n".join(p for p in plain_parts if p.strip()) or html_to_text(
        "\n".join(html_parts)
    )
    body = body.strip()
    if len(body) > BODY_CHAR_LIMIT:
        body = body[:BODY_CHAR_LIMIT] + "\n…[truncated]"
    return body


def message_to_email(raw: bytes, email_id: str, imap_uid: str | None = None) -> Email:
    msg = message_from_bytes(raw)
    to_header = decode_mime(msg.get("To"))
    return Email(
        id=email_id,
        sender=decode_mime(msg.get("From")),
        to=to_header,
        date=decode_mime(msg.get("Date")),
        subject=decode_mime(msg.get("Subject")) or "(no subject)",
        body=extract_body(msg),
        imap_uid=imap_uid,
    )


def sample_emails() -> list[Email]:
    return [
        Email(
            id=str(item["id"]),
            sender=str(item["from"]),
            to=str(item["to"]),
            date=str(item["date"]),
            subject=str(item["subject"]),
            body=str(item["body"]),
        )
        for item in SAMPLE_EMAILS
    ]


def load_eml_dir(directory: Path) -> list[Email]:
    files = sorted(directory.glob("*.eml"))
    if not files:
        raise SystemExit(f"No .eml files in {directory}")
    emails: list[Email] = []
    for path in files:
        emails.append(message_to_email(path.read_bytes(), path.name))
    return emails


def imap_search_criteria(*, unread: bool, since_days: int | None) -> str:
    terms: list[str] = ["UNSEEN"] if unread else ["ALL"]
    if since_days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=since_days)
        terms.append(f'SINCE {since.strftime("%d-%b-%Y")}')
    if len(terms) == 1:
        return terms[0]
    return "(" + " ".join(terms) + ")"


@dataclass(frozen=True)
class Mailbox:
    host: str
    port: int
    user: str
    password: str
    folder: str

    @property
    def is_gmail(self) -> bool:
        return "gmail.com" in self.host.lower() or self.user.lower().endswith("@gmail.com")


def mailbox_from_env(*, folder: str) -> Mailbox:
    user = (
        os.environ.get("GMAIL_ADDRESS") or os.environ.get("IMAP_USER") or ""
    ).strip()
    password = (
        os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("IMAP_PASSWORD") or ""
    ).strip().replace(" ", "")
    host = os.environ.get("IMAP_HOST", "imap.gmail.com").strip() or "imap.gmail.com"
    port = int(os.environ.get("IMAP_PORT", "993") or "993")
    if not user or not password:
        raise SystemExit(
            "Gmail is not connected. In .env set GMAIL_ADDRESS and GMAIL_APP_PASSWORD.\n"
            "1. Enable IMAP: https://mail.google.com/mail/#settings/fwdandpop\n"
            "2. Create an App Password: https://myaccount.google.com/apppasswords\n"
            "   (2-Step Verification must be on; this is not your normal Gmail password.)"
        )
    return Mailbox(host=host, port=port, user=user, password=password, folder=folder)


def connect_mailbox(mailbox: Mailbox, *, readonly: bool) -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(
        mailbox.host, mailbox.port, ssl_context=ssl.create_default_context()
    )
    try:
        client.login(mailbox.user, mailbox.password)
    except imaplib.IMAP4.error as exc:
        client.logout()
        raise SystemExit(
            f"Gmail IMAP login failed for {mailbox.user}: {exc}\n"
            "Use an App Password, not your Google account password: "
            "https://myaccount.google.com/apppasswords"
        ) from exc
    status, _ = client.select(mailbox.folder, readonly=readonly)
    if status != "OK":
        client.logout()
        raise SystemExit(f"Could not open Gmail folder {mailbox.folder!r}")
    return client


def check_gmail_imap(folder: str) -> None:
    mailbox = mailbox_from_env(folder=folder)
    client = connect_mailbox(mailbox, readonly=True)
    try:
        status, data = client.uid("search", None, "UNSEEN")
        unread = len(data[0].split()) if status == "OK" and data and data[0] else 0
        print(
            f"Connected to Gmail as {mailbox.user} via {mailbox.host}:{mailbox.port}\n"
            f"Folder {mailbox.folder} has {unread} unread message(s)."
        )
    finally:
        client.logout()


def fetch_imap(
    *,
    unread: bool,
    limit: int,
    since_days: int | None,
    folder: str,
) -> tuple[imaplib.IMAP4_SSL, list[Email]]:
    mailbox = mailbox_from_env(folder=folder)
    client = connect_mailbox(mailbox, readonly=True)
    criteria = imap_search_criteria(unread=unread, since_days=since_days)
    status, data = client.uid("search", None, criteria)
    if status != "OK":
        client.logout()
        raise SystemExit(f"Gmail search failed for {criteria}")

    uids = data[0].split() if data and data[0] else []
    uids = uids[-limit:]
    emails: list[Email] = []
    for uid in uids:
        status, fetched = client.uid("fetch", uid, "(BODY.PEEK[])")
        raw = _raw_from_fetch(fetched if status == "OK" else None)
        if raw is None:
            continue
        uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
        emails.append(message_to_email(raw, uid_str, imap_uid=uid_str))
    return client, emails


def _raw_from_fetch(fetched: list[Any] | None) -> bytes | None:
    if not fetched:
        return None
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def reopen_imap_for_flags(folder: str) -> imaplib.IMAP4_SSL:
    return connect_mailbox(mailbox_from_env(folder=folder), readonly=False)


def apply_flags(
    client: imaplib.IMAP4_SSL,
    results: Sequence[Classification],
    *,
    flag: bool,
    folder: str,
) -> None:
    if not flag:
        return
    mailbox = mailbox_from_env(folder=folder)
    label = os.environ.get("GMAIL_LABEL", "Jev/Attention").strip()
    for item in results:
        uid = item.email.imap_uid
        if not uid or item.decision != "attention":
            continue
        client.uid("store", uid, "+FLAGS", "(\\Flagged)")
        if mailbox.is_gmail and label:
            quoted = '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"'
            client.uid("store", uid, "+X-GM-LABELS", f"({quoted})")


def email_state(message: Email) -> dict[str, str]:
    state = {
        "from": message.sender,
        "to": message.to,
        "date": message.date,
        "subject": message.subject,
        "body": message.body or "(empty body)",
    }
    priorities = os.environ.get("RECIPIENT_PRIORITIES", "").strip()
    if priorities:
        state["recipient_priorities"] = priorities
    return state


def decide(
    *,
    kind: str,
    kind_confidence: float,
    kind_probabilities: dict[str, float],
    urgency: float,
    expects_reply: float,
    action_required: float,
    from_real_person: float,
    time_sensitive: float,
    is_marketing: float,
    is_spam_or_phishing: float,
    threshold: float,
) -> tuple[Decision, float, str]:
    urgency_norm = urgency / URGENCY_MAX if URGENCY_MAX else 0.0
    score = (
        0.30 * expects_reply
        + 0.22 * action_required
        + 0.20 * urgency_norm
        + 0.16 * from_real_person
        + 0.12 * time_sensitive
        - 0.45 * is_marketing
        - 0.90 * is_spam_or_phishing
    )

    if kind in ATTENTION_KINDS:
        score += 0.12 * max(kind_confidence, kind_probabilities.get(kind, 0.0))
    elif kind in NOISE_KINDS:
        score -= 0.18 * max(kind_confidence, kind_probabilities.get(kind, 0.0))

    reasons: list[str] = []
    if is_spam_or_phishing >= 0.75:
        return "skip", score, "spam/phishing"
    if (
        is_marketing >= 0.82
        and expects_reply < 0.35
        and action_required < 0.35
        and time_sensitive < 0.4
        and kind in NOISE_KINDS
    ):
        return "skip", score, f"{kind}; marketing"

    if expects_reply >= 0.6:
        reasons.append("expects a reply")
    if action_required >= 0.6:
        reasons.append("action required")
    if time_sensitive >= 0.6:
        reasons.append("time-sensitive")
    if kind in ATTENTION_KINDS and kind_confidence >= 0.5:
        reasons.append(kind.replace("_", " "))
    if urgency >= 3.0:
        reasons.append("urgent")
    if not reasons:
        if score >= threshold:
            reasons.append("composite score")
        else:
            reasons.append(kind.replace("_", " ") or "low signal")

    reason = ", ".join(reasons)
    if score >= threshold + REVIEW_BAND:
        return "attention", score, reason
    if score < threshold - REVIEW_BAND:
        return "skip", score, reason
    return "review", score, f"uncertain · {reason}"


async def classify_one(
    client: AsyncTypeSafeClient,
    message: Email,
    *,
    threshold: float,
    semaphore: asyncio.Semaphore,
) -> Classification:
    async with semaphore:
        try:
            response = await client.system_one(
                state=email_state(message),
                questions=QUESTIONS,
            )
        except TypeSafeError as exc:
            return Classification(
                email=message,
                decision="review",
                score=0.0,
                reason="api error",
                kind="unknown",
                kind_confidence=0.0,
                kind_probabilities={},
                urgency=0.0,
                urgency_confidence=0.0,
                expects_reply=0.0,
                action_required=0.0,
                from_real_person=0.0,
                time_sensitive=0.0,
                is_marketing=0.0,
                is_spam_or_phishing=0.0,
                model="",
                error=str(exc),
            )

    kind_answer = response.choices["kind"]
    urgency_answer = response.scores["urgency"]
    decision, score, reason = decide(
        kind=kind_answer.choice,
        kind_confidence=kind_answer.confidence,
        kind_probabilities=dict(kind_answer.probabilities),
        urgency=float(urgency_answer.score),
        expects_reply=float(response.nouls["expects_reply"].noul),
        action_required=float(response.nouls["action_required"].noul),
        from_real_person=float(response.nouls["from_real_person"].noul),
        time_sensitive=float(response.nouls["time_sensitive"].noul),
        is_marketing=float(response.nouls["is_marketing"].noul),
        is_spam_or_phishing=float(response.nouls["is_spam_or_phishing"].noul),
        threshold=threshold,
    )
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    return Classification(
        email=message,
        decision=decision,
        score=score,
        reason=reason,
        kind=kind_answer.choice,
        kind_confidence=float(kind_answer.confidence),
        kind_probabilities={k: float(v) for k, v in kind_answer.probabilities.items()},
        urgency=float(urgency_answer.score),
        urgency_confidence=float(urgency_answer.confidence),
        expects_reply=float(response.nouls["expects_reply"].noul),
        action_required=float(response.nouls["action_required"].noul),
        from_real_person=float(response.nouls["from_real_person"].noul),
        time_sensitive=float(response.nouls["time_sensitive"].noul),
        is_marketing=float(response.nouls["is_marketing"].noul),
        is_spam_or_phishing=float(response.nouls["is_spam_or_phishing"].noul),
        model=str(getattr(response, "model", "") or ""),
        input_tokens=input_tokens,
    )


def resolve_openrouter_config() -> tuple[str, str]:
    api_key = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("TYPESAFE_API_KEY")
        or ""
    ).strip()
    base_url = (
        os.environ.get("OPENROUTER_BASE_URL")
        or os.environ.get("TYPESAFE_BASE_URL")
        or OPENROUTER_BASE_URL
    ).strip()
    return api_key, base_url or OPENROUTER_BASE_URL


async def classify_all(
    emails: Sequence[Email],
    *,
    model: str,
    threshold: float,
    concurrency: int,
) -> RunStats:
    stats = RunStats()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    api_key, base_url = resolve_openrouter_config()
    try:
        async with AsyncTypeSafeClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            headers={
                "HTTP-Referer": "https://github.com/email-classifier",
                "X-OpenRouter-Title": "email-classifier",
            },
        ) as client:
            results = await asyncio.gather(
                *(
                    classify_one(
                        client, message, threshold=threshold, semaphore=semaphore
                    )
                    for message in emails
                )
            )
    except TypeSafeError as exc:
        raise SystemExit(f"OpenRouter / Jev client error: {exc}") from exc
    stats.results = list(results)
    if stats.results:
        stats.model = next((item.model for item in stats.results if item.model), model)
        stats.input_tokens = sum(item.input_tokens for item in stats.results)
    return stats


def _color(enabled: bool, code: str, text: str) -> str:
    if not enabled:
        return text
    return f"\033[{code}m{text}\033[0m"


def render_text(stats: RunStats, *, color: bool) -> str:
    groups: dict[Decision, list[Classification]] = {
        "attention": [],
        "review": [],
        "skip": [],
    }
    for item in stats.results:
        groups[item.decision].append(item)

    titles = {
        "attention": "NEEDS ATTENTION",
        "review": "REVIEW (uncertain)",
        "skip": "NOT IMPORTANT",
    }
    title_colors = {"attention": "1;32", "review": "1;33", "skip": "1;90"}
    lines: list[str] = []
    for key in ("attention", "review", "skip"):
        bucket = groups[key]
        if not bucket:
            continue
        heading = f"{titles[key]} · {len(bucket)}"
        lines.append(_color(color, title_colors[key], heading))
        lines.append("")
        for item in sorted(bucket, key=lambda row: row.score, reverse=True):
            subject = item.email.subject
            sender = item.email.sender
            lines.append(f"  {subject}")
            lines.append(f"    from  {sender}")
            detail = (
                f"    {item.kind} · score {item.score:+.2f} · "
                f"urgency {item.urgency:.1f}/{URGENCY_MAX} · {item.reason}"
            )
            if item.error:
                detail += f" · error: {item.error}"
            lines.append(_color(color, "2", detail))
            lines.append("")
    summary = (
        f"{len(groups['attention'])} need attention, "
        f"{len(groups['review'])} to review, "
        f"{len(groups['skip'])} not important"
    )
    if stats.model:
        summary += f" · {stats.model}"
    if stats.input_tokens:
        summary += f" · {stats.input_tokens} input tokens"
    lines.append(_color(color, "1", summary))
    return "\n".join(lines).rstrip() + "\n"


def classification_dict(item: Classification) -> dict[str, Any]:
    payload = asdict(item)
    payload["email"] = asdict(item.email)
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify mail with TypeSafe Jev into needs-attention vs not-important.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Setup:\n"
            "  1. Get an OpenRouter API key: https://openrouter.ai/keys\n"
            "  2. Copy .env.example to .env and set OPENROUTER_API_KEY\n"
            "  3. Download Google Desktop OAuth credentials as credentials.json\n"
            "  4. python classify.py --login   (browser sign-in, 2FA included)\n"
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--demo",
        action="store_true",
        help="Classify built-in sample emails (no IMAP).",
    )
    source.add_argument(
        "--eml-dir",
        type=Path,
        help="Classify every .eml file in this directory.",
    )
    parser.add_argument("--unread", action="store_true", help="Only unread inbox messages.")
    parser.add_argument("--limit", type=int, default=25, help="Max messages to classify.")
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Only mail from the last N days.",
    )
    parser.add_argument(
        "--folder",
        default=os.environ.get("GMAIL_FOLDER", os.environ.get("IMAP_FOLDER", "primary")),
        help="Gmail tab: primary, inbox, social, updates, promotions, forums (default: primary).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="Composite score cutoff for attention (default: 0.40).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Parallel Jev calls (default: 8).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get(
            "OPENROUTER_MODEL",
            os.environ.get("TYPESAFE_DEFAULT_MODEL", DEFAULT_MODEL),
        ),
        help="Jev model id via OpenRouter (default: jev-latest → ~typesafe/jev-latest).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of a human-readable report.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the JSON report to this file as well.",
    )
    parser.add_argument(
        "--flag",
        action="store_true",
        help="Star messages that need attention and add GMAIL_LABEL.",
    )
    parser.add_argument(
        "--poll",
        type=int,
        metavar="SECONDS",
        help="Re-run on new unread mail every SECONDS.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors.",
    )
    parser.add_argument(
        "--check-gmail",
        action="store_true",
        help="Verify Gmail OAuth and print unread count (no Jev call).",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open a browser to sign in to Gmail with Google OAuth (2FA ok).",
    )
    parser.add_argument(
        "--imap",
        action="store_true",
        help="Fall back to IMAP + App Password instead of the Gmail API.",
    )
    return parser.parse_args(argv)


def load_emails(args: argparse.Namespace) -> tuple[list[Email], imaplib.IMAP4_SSL | None]:
    if args.demo:
        return sample_emails()[: args.limit], None
    if args.eml_dir:
        return load_eml_dir(args.eml_dir)[: args.limit], None
    if args.imap:
        client, emails = fetch_imap(
            unread=args.unread,
            limit=args.limit,
            since_days=args.since_days,
            folder=args.folder,
        )
        return emails, client
    messages = gmail_api.fetch_inbox(
        unread=args.unread,
        limit=args.limit,
        since_days=args.since_days,
        folder=args.folder,
        body_limit=BODY_CHAR_LIMIT,
    )
    emails = [
        Email(
            id=item["id"],
            sender=item["from"],
            to=item["to"],
            date=item["date"],
            subject=item["subject"],
            body=item["body"],
            gmail_id=item["id"],
        )
        for item in messages
    ]
    return emails, None


def report_payload(stats: RunStats, threshold: float) -> dict[str, Any]:
    return {
        "model": stats.model,
        "input_tokens": stats.input_tokens,
        "threshold": threshold,
        "emails": [classification_dict(item) for item in stats.results],
    }


def print_report(stats: RunStats, args: argparse.Namespace) -> None:
    payload = report_payload(stats, args.threshold)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.json:
        print(text, end="")
    else:
        color = sys.stdout.isatty() and not args.no_color
        print(render_text(stats, color=color), end="")
    if args.output:
        args.output.write_text(text, encoding="utf-8")


def run_once(args: argparse.Namespace, *, seen: set[str] | None = None) -> set[str]:
    emails, imap_client = load_emails(args)
    processed = set(seen or ())
    try:
        if seen is not None:
            emails = [item for item in emails if item.id not in processed]
        if not emails:
            if not args.json and seen is None:
                print("No emails to classify.")
            return processed
        stats = asyncio.run(
            classify_all(
                emails,
                model=args.model,
                threshold=args.threshold,
                concurrency=args.concurrency,
            )
        )
        print_report(stats, args)
        if args.flag:
            gmail_ids = [
                item.email.gmail_id
                for item in stats.results
                if item.email.gmail_id and item.decision == "attention"
            ]
            if gmail_ids:
                gmail_api.apply_attention_labels(
                    gmail_ids,
                    label=os.environ.get("GMAIL_LABEL", "Jev/Attention").strip(),
                )
            elif any(item.email.imap_uid for item in stats.results):
                if imap_client is not None:
                    try:
                        imap_client.logout()
                    except Exception:
                        pass
                    imap_client = None
                flag_client = reopen_imap_for_flags(args.folder)
                try:
                    apply_flags(
                        flag_client, stats.results, flag=True, folder=args.folder
                    )
                finally:
                    flag_client.logout()
        processed.update(item.email.id for item in stats.results)
        return processed
    finally:
        if imap_client is not None:
            try:
                imap_client.logout()
            except Exception:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv_files()
    args = parse_args(argv)
    if args.login:
        gmail_api.login()
        return 0
    if args.check_gmail:
        if args.imap:
            check_gmail_imap(args.folder)
        else:
            gmail_api.check_connection()
        return 0
    api_key, _ = resolve_openrouter_config()
    if not api_key:
        raise SystemExit(
            "OPENROUTER_API_KEY is not set. Get a key at "
            "https://openrouter.ai/keys and put it in .env"
        )
    if args.poll and (args.demo or args.eml_dir):
        raise SystemExit("--poll only works with a live Gmail inbox.")
    if args.poll:
        args.unread = True
        seen: set[str] = set()
        while True:
            seen = run_once(args, seen=seen)
            time.sleep(args.poll)
    run_once(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
