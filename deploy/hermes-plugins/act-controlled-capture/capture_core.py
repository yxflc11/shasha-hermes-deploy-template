"""Deterministic, append-only capture journal used by the Hermes plugin.

This module deliberately has no Hermes imports so its safety properties can be
tested locally. It never reads or writes the ACT Vault.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


SCHEMA_VERSION = 1
MAX_TEXT_LENGTH = 12_000
CAPTURE_RE = re.compile(r"^记一下(?:[：:][ \t]*|[ \t]+)(?P<body>[\s\S]+)$")
EMPTY_CAPTURE_RE = re.compile(r"^记一下[：:]?[ \t]*$")
CANCEL_RE = re.compile(r"^撤销上一条[ \t]*$")


@dataclass(frozen=True)
class Decision:
    action: str
    capture_id: Optional[str] = None
    message: str = ""


def _compact_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _read_events(handle) -> list[dict[str, Any]]:
    handle.seek(0)
    events: list[dict[str, Any]] = []
    for raw_line in handle:
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _active_captures(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    captures: dict[str, dict[str, Any]] = {}
    cancelled: set[str] = set()
    for event in events:
        event_type = event.get("event")
        capture_id = event.get("capture_id")
        if not isinstance(capture_id, str):
            continue
        if event_type == "capture":
            captures.setdefault(capture_id, event)
        elif event_type == "cancel":
            cancelled.add(capture_id)
    return {
        capture_id: event
        for capture_id, event in captures.items()
        if capture_id not in cancelled
    }


class CaptureJournal:
    """A locked JSONL journal with idempotent capture and append-only cancel."""

    def __init__(self, journal_path: Path):
        self.path = Path(journal_path)

    def _open(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return os.fdopen(fd, "r+", encoding="utf-8")

    def _append_locked(self, handle, event: dict[str, Any]) -> None:
        handle.seek(0, os.SEEK_END)
        handle.write(_compact_json(event).decode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())

    def capture(
        self,
        *,
        raw_text: str,
        message_id: str,
        sender_id: str,
        channel: str = "weixin_home",
        captured_at: Optional[str] = None,
    ) -> Decision:
        if not isinstance(raw_text, str) or "\x00" in raw_text:
            return Decision("reject", message="只支持普通文本捕获。")
        if len(raw_text) > MAX_TEXT_LENGTH:
            return Decision(
                "reject",
                message=f"这条内容超过 {MAX_TEXT_LENGTH} 字，请拆成两条再记。",
            )
        if EMPTY_CAPTURE_RE.fullmatch(raw_text):
            return Decision("empty", message="请在“记一下”后面写下要保存的内容。")
        match = CAPTURE_RE.fullmatch(raw_text)
        if not match:
            return Decision("allow")
        if not match.group("body").strip():
            return Decision("empty", message="请在“记一下”后面写下要保存的内容。")

        timestamp = captured_at or datetime.now(timezone.utc).isoformat()
        sender_fingerprint = hashlib.sha256(sender_id.encode("utf-8")).hexdigest()[:16]
        identity = "\x00".join((channel, message_id, sender_fingerprint, raw_text))
        capture_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        raw_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        event = {
            "schema_version": SCHEMA_VERSION,
            "event": "capture",
            "capture_id": capture_id,
            "captured_at": timestamp,
            "channel": channel,
            "message_id": message_id,
            "sender_fingerprint": sender_fingerprint,
            "raw_sha256": raw_sha256,
            "raw_text": raw_text,
        }

        with self._open() as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            events = _read_events(handle)
            if any(
                existing.get("event") == "capture"
                and existing.get("capture_id") == capture_id
                for existing in events
            ):
                return Decision(
                    "duplicate",
                    capture_id=capture_id,
                    message=f"这条已经暂存过了（#{capture_id}）。",
                )
            self._append_locked(handle, event)
        return Decision(
            "captured",
            capture_id=capture_id,
            message=(
                f"已原文暂存（#{capture_id}）。"
                "需要取消时回复“撤销上一条”。"
            ),
        )

    def cancel_latest(self, *, sender_id: str, cancelled_at: Optional[str] = None) -> Decision:
        sender_fingerprint = hashlib.sha256(sender_id.encode("utf-8")).hexdigest()[:16]
        timestamp = cancelled_at or datetime.now(timezone.utc).isoformat()
        with self._open() as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            events = _read_events(handle)
            active = _active_captures(events)
            latest: Optional[dict[str, Any]] = None
            for event in events:
                capture_id = event.get("capture_id")
                if (
                    event.get("event") == "capture"
                    and capture_id in active
                    and event.get("sender_fingerprint") == sender_fingerprint
                ):
                    latest = event
            if latest is None:
                return Decision("nothing_to_cancel", message="没有可撤销的待捕获内容。")
            capture_id = str(latest["capture_id"])
            self._append_locked(
                handle,
                {
                    "schema_version": SCHEMA_VERSION,
                    "event": "cancel",
                    "capture_id": capture_id,
                    "cancelled_at": timestamp,
                    "sender_fingerprint": sender_fingerprint,
                },
            )
        return Decision(
            "cancelled",
            capture_id=capture_id,
            message=(
                f"已标记撤销（#{capture_id}）。"
                "尚未导入的内容会被跳过；已导入内容仍需在 ACT 中确认处理。"
            ),
        )


def decide_message(
    journal: CaptureJournal,
    *,
    raw_text: str,
    message_id: str,
    sender_id: str,
    channel: str = "weixin_home",
) -> Decision:
    if CANCEL_RE.fullmatch(raw_text):
        return journal.cancel_latest(sender_id=sender_id)
    return journal.capture(
        raw_text=raw_text,
        message_id=message_id,
        sender_id=sender_id,
        channel=channel,
    )
