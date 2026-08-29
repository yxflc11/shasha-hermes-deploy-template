#!/usr/bin/env python3
"""Import uncancelled Hermes captures into ACT's top-level x/ inbox.

The remote journal is read over the existing private SSH path. Import is
create-only, validates the raw-text hash, and never merges or pulls Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_SSH_TARGET = ""
REMOTE_JOURNAL = os.environ.get(
    "ACT_CAPTURE_REMOTE_JOURNAL",
    "/var/lib/shasha-hermes/act-capture/journal.jsonl",
)
CAPTURE_ID_RE = re.compile(r"^捕获 ID：([a-f0-9]{16})$", re.MULTILINE)
CHANNEL_LABELS = {
    "weixin_home": "微信 Home Channel",
    "telegram_dm": "Telegram 私聊",
}


class ImportFailure(RuntimeError):
    pass


def read_remote_journal(target: str) -> str:
    result = subprocess.run(
        [
            "/usr/bin/ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            target,
            "cat",
            REMOTE_JOURNAL,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        if "No such file" in result.stderr:
            return ""
        raise ImportFailure("无法读取远端捕获日志；未修改 ACT。")
    return result.stdout


def parse_journal(text: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
    captures: dict[str, dict[str, Any]] = {}
    cancelled: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ImportFailure(f"捕获日志第 {line_number} 行不是有效 JSON。") from exc
        if not isinstance(event, dict) or event.get("schema_version") != 1:
            raise ImportFailure(f"捕获日志第 {line_number} 行版本不受支持。")
        capture_id = event.get("capture_id")
        if not isinstance(capture_id, str) or not re.fullmatch(r"[a-f0-9]{16}", capture_id):
            raise ImportFailure(f"捕获日志第 {line_number} 行 ID 无效。")
        event_type = event.get("event")
        if event_type == "capture":
            captures.setdefault(capture_id, event)
        elif event_type == "cancel":
            cancelled.add(capture_id)
        else:
            raise ImportFailure(f"捕获日志第 {line_number} 行事件类型无效。")
    return captures, cancelled


def validate_capture(capture: dict[str, Any]) -> None:
    required_strings = (
        "capture_id",
        "captured_at",
        "channel",
        "message_id",
        "sender_fingerprint",
        "raw_sha256",
        "raw_text",
    )
    if any(not isinstance(capture.get(key), str) for key in required_strings):
        raise ImportFailure(f"捕获 #{capture.get('capture_id', '?')} 字段不完整。")
    if capture["channel"] not in CHANNEL_LABELS:
        raise ImportFailure(f"捕获 #{capture['capture_id']} 来源不在允许范围。")
    raw_text = capture["raw_text"]
    expected_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    if expected_hash != capture["raw_sha256"]:
        raise ImportFailure(f"捕获 #{capture['capture_id']} 原文校验失败。")
    if len(raw_text) > 12_000 or "\x00" in raw_text:
        raise ImportFailure(f"捕获 #{capture['capture_id']} 原文不符合限制。")


def existing_capture_ids(vault: Path) -> set[str]:
    found: set[str] = set()
    for path in vault.rglob("*.md"):
        if ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found.update(CAPTURE_ID_RE.findall(text))
    return found


def _dynamic_fence(text: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(4, longest + 1)


def render_note(capture: dict[str, Any]) -> tuple[str, str]:
    validate_capture(capture)
    try:
        timestamp = datetime.fromisoformat(capture["captured_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImportFailure(f"捕获 #{capture['capture_id']} 时间格式无效。") from exc
    if timestamp.tzinfo is None:
        raise ImportFailure(f"捕获 #{capture['capture_id']} 缺少时区。")
    local_time = timestamp.astimezone(ZoneInfo("Asia/Shanghai"))
    capture_id = capture["capture_id"]
    source_label = CHANNEL_LABELS[capture["channel"]]
    filename = f"Hermes-{local_time:%Y-%m-%d-%H%M}-{capture_id}.md"
    message_fingerprint = hashlib.sha256(capture["message_id"].encode("utf-8")).hexdigest()[:16]
    fence = _dynamic_fence(capture["raw_text"])
    note = (
        "---\n"
        f"创建日期: {local_time:%Y-%m-%d}\n"
        f"AI 备注: {source_label} 通过“记一下”显式捕获的用户原文，未经改写，待分诊。\n"
        "---\n\n"
        f"# Hermes 捕获 · {local_time:%Y-%m-%d %H:%M}\n\n"
        f"来源：{source_label}\n\n"
        f"捕获时间：{local_time.isoformat()}\n\n"
        f"捕获 ID：{capture_id}\n\n"
        f"消息指纹：{message_fingerprint}\n\n"
        f"原文 SHA-256：{capture['raw_sha256']}\n\n"
        "## 原文\n\n"
        f"{fence}text\n{capture['raw_text']}\n{fence}\n"
    )
    return filename, note


def import_captures(
    *,
    journal_text: str,
    vault: Path,
    dry_run: bool = False,
) -> list[Path]:
    vault = vault.resolve()
    inbox = (vault / "x").resolve()
    if not inbox.is_dir() or inbox.parent != vault:
        raise ImportFailure("ACT 顶层 x/ 收件箱不存在或路径异常。")

    captures, cancelled = parse_journal(journal_text)
    imported = existing_capture_ids(vault)
    pending = [
        capture
        for capture_id, capture in captures.items()
        if capture_id not in cancelled and capture_id not in imported
    ]
    rendered: list[tuple[Path, str]] = []
    for capture in pending:
        filename, note = render_note(capture)
        destination = inbox / filename
        if destination.parent != inbox:
            raise ImportFailure("捕获文件路径逃逸，已拒绝。")
        rendered.append((destination, note))

    if dry_run:
        return [path for path, _ in rendered]

    created: list[Path] = []
    for destination, note in rendered:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(destination, flags, 0o600)
        except FileExistsError as exc:
            raise ImportFailure(f"目标文件已存在：{destination.name}") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(note)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        created.append(destination)
    return created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导入 Hermes 显式捕获到 ACT x/")
    parser.add_argument("--journal", type=Path, help="读取本地测试 journal，跳过 SSH")
    parser.add_argument("--vault", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--ssh-target", default=os.getenv("ACT_CAPTURE_SSH_TARGET", DEFAULT_SSH_TARGET))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.journal and not args.ssh_target:
            raise ImportFailure(
                "请通过 --ssh-target 或 ACT_CAPTURE_SSH_TARGET 指定远端；未修改 ACT。"
            )
        journal_text = (
            args.journal.read_text(encoding="utf-8")
            if args.journal
            else read_remote_journal(args.ssh_target)
        )
        created = import_captures(
            journal_text=journal_text,
            vault=args.vault,
            dry_run=args.dry_run,
        )
    except (ImportFailure, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    verb = "待导入" if args.dry_run else "已导入"
    print(f"{verb} {len(created)} 条 Hermes 捕获。")
    for path in created:
        print(path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
