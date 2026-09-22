"""SQLite cache of Jev classifications so later runs skip work already done."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_CREATE = """
CREATE TABLE IF NOT EXISTS classifications (
    id TEXT PRIMARY KEY,
    gmail_id TEXT,
    thread_id TEXT,
    sender TEXT NOT NULL DEFAULT '',
    to_addr TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    kind_confidence REAL NOT NULL,
    kind_probabilities TEXT NOT NULL,
    urgency REAL NOT NULL,
    urgency_confidence REAL NOT NULL,
    expects_reply REAL NOT NULL,
    action_required REAL NOT NULL,
    from_real_person REAL NOT NULL,
    time_sensitive REAL NOT NULL,
    is_marketing REAL NOT NULL,
    is_spam_or_phishing REAL NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    classified_at TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT ''
)
"""


def db_path() -> Path:
    raw = os.environ.get("CLASSIFY_DB", "classifications.sqlite").strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(_CREATE)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(classifications)")}
    if "thread_id" not in columns:
        conn.execute("ALTER TABLE classifications ADD COLUMN thread_id TEXT")
    if "folder" not in columns:
        conn.execute(
            "ALTER TABLE classifications ADD COLUMN folder TEXT NOT NULL DEFAULT ''"
        )
    return conn


def _row_to_classification(row: sqlite3.Row, *, threshold: float):
    from classify import Classification, Email, decide

    kind_probabilities = json.loads(row["kind_probabilities"])
    decision, score, reason = decide(
        kind=row["kind"],
        kind_confidence=float(row["kind_confidence"]),
        kind_probabilities=kind_probabilities,
        urgency=float(row["urgency"]),
        expects_reply=float(row["expects_reply"]),
        action_required=float(row["action_required"]),
        from_real_person=float(row["from_real_person"]),
        time_sensitive=float(row["time_sensitive"]),
        is_marketing=float(row["is_marketing"]),
        is_spam_or_phishing=float(row["is_spam_or_phishing"]),
        threshold=threshold,
    )
    email = Email(
        id=row["id"],
        sender=row["sender"],
        to=row["to_addr"],
        date=row["date"],
        subject=row["subject"],
        body=row["body"],
        gmail_id=row["gmail_id"],
        thread_id=row["thread_id"] if "thread_id" in row.keys() else None,
    )
    return Classification(
        email=email,
        decision=decision,
        score=score,
        reason=reason,
        kind=row["kind"],
        kind_confidence=float(row["kind_confidence"]),
        kind_probabilities=kind_probabilities,
        urgency=float(row["urgency"]),
        urgency_confidence=float(row["urgency_confidence"]),
        expects_reply=float(row["expects_reply"]),
        action_required=float(row["action_required"]),
        from_real_person=float(row["from_real_person"]),
        time_sensitive=float(row["time_sensitive"]),
        is_marketing=float(row["is_marketing"]),
        is_spam_or_phishing=float(row["is_spam_or_phishing"]),
        model=row["model"],
        input_tokens=int(row["input_tokens"] or 0),
        cached=True,
    )


def get_many(ids: Iterable[str], *, threshold: float) -> dict[str, Any]:
    wanted = [item for item in ids if item]
    if not wanted:
        return {}
    found: dict[str, Any] = {}
    with connect() as conn:
        for start in range(0, len(wanted), 400):
            chunk = wanted[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM classifications WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                found[row["id"]] = _row_to_classification(row, threshold=threshold)
    return found


def save(item: Any, *, folder: str | None = None) -> None:
    if item.error or item.cached:
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO classifications (
                id, gmail_id, thread_id, sender, to_addr, date, subject, body,
                kind, kind_confidence, kind_probabilities, urgency, urgency_confidence,
                expects_reply, action_required, from_real_person, time_sensitive,
                is_marketing, is_spam_or_phishing, model, input_tokens, classified_at,
                folder
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?
            )
            ON CONFLICT(id) DO UPDATE SET
                gmail_id=excluded.gmail_id,
                thread_id=excluded.thread_id,
                sender=excluded.sender,
                to_addr=excluded.to_addr,
                date=excluded.date,
                subject=excluded.subject,
                body=excluded.body,
                kind=excluded.kind,
                kind_confidence=excluded.kind_confidence,
                kind_probabilities=excluded.kind_probabilities,
                urgency=excluded.urgency,
                urgency_confidence=excluded.urgency_confidence,
                expects_reply=excluded.expects_reply,
                action_required=excluded.action_required,
                from_real_person=excluded.from_real_person,
                time_sensitive=excluded.time_sensitive,
                is_marketing=excluded.is_marketing,
                is_spam_or_phishing=excluded.is_spam_or_phishing,
                model=excluded.model,
                input_tokens=excluded.input_tokens,
                classified_at=excluded.classified_at,
                folder=excluded.folder
            """,
            (
                item.email.id,
                item.email.gmail_id,
                item.email.thread_id,
                item.email.sender,
                item.email.to,
                item.email.date,
                item.email.subject,
                item.email.body,
                item.kind,
                item.kind_confidence,
                json.dumps(item.kind_probabilities),
                item.urgency,
                item.urgency_confidence,
                item.expects_reply,
                item.action_required,
                item.from_real_person,
                item.time_sensitive,
                item.is_marketing,
                item.is_spam_or_phishing,
                item.model,
                item.input_tokens,
                datetime.now(timezone.utc).isoformat(),
                (folder or "").strip(),
            ),
        )


def list_recent(*, threshold: float, limit: int = 500) -> list[Any]:
    query = "SELECT * FROM classifications ORDER BY classified_at DESC"
    params: tuple[Any, ...] = ()
    if limit > 0:
        query += " LIMIT ?"
        params = (limit,)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_classification(row, threshold=threshold) for row in rows]


def count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM classifications").fetchone()
    return int(row["n"] if row else 0)


def count_by_folder() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT folder, COUNT(*) AS n FROM classifications "
            "WHERE folder IS NOT NULL AND folder != '' "
            "GROUP BY folder"
        ).fetchall()
    return {str(row["folder"]): int(row["n"]) for row in rows}
