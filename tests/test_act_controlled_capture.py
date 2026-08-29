from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


VAULT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = VAULT / "deploy/hermes-plugins/act-controlled-capture"
sys.path.insert(0, str(PLUGIN_DIR))

from capture_core import CaptureJournal, decide_message  # noqa: E402


def load_importer():
    path = VAULT / "scripts/act-capture-import.py"
    spec = importlib.util.spec_from_file_location("act_capture_import", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


importer = load_importer()


class CaptureJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal_path = Path(self.temp.name) / "state" / "journal.jsonl"
        self.journal = CaptureJournal(self.journal_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_ordinary_chat_is_not_recorded(self):
        decision = decide_message(
            self.journal,
            raw_text="这个方案还能再简化吗？",
            message_id="m-1",
            sender_id="使用者",
        )
        self.assertEqual(decision.action, "allow")
        self.assertFalse(self.journal_path.exists())

    def test_capture_preserves_exact_raw_text(self):
        raw = "记一下：今天想到一个点子\n第二行保留。"
        decision = decide_message(
            self.journal,
            raw_text=raw,
            message_id="m-2",
            sender_id="使用者",
        )
        self.assertEqual(decision.action, "captured")
        event = json.loads(self.journal_path.read_text(encoding="utf-8"))
        self.assertEqual(event["raw_text"], raw)

    def test_telegram_capture_records_channel(self):
        raw = "记一下：从 Telegram 迁移"
        decision = decide_message(
            self.journal,
            raw_text=raw,
            message_id="tg-2",
            sender_id="使用者-telegram-id",
            channel="telegram_dm",
        )
        self.assertEqual(decision.action, "captured")
        event = json.loads(self.journal_path.read_text(encoding="utf-8"))
        self.assertEqual(event["channel"], "telegram_dm")

    def test_duplicate_message_is_idempotent(self):
        kwargs = dict(raw_text="记一下 重复测试", message_id="m-3", sender_id="使用者")
        first = decide_message(self.journal, **kwargs)
        second = decide_message(self.journal, **kwargs)
        self.assertEqual(first.action, "captured")
        self.assertEqual(second.action, "duplicate")
        self.assertEqual(len(self.journal_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_cancel_appends_marker(self):
        captured = decide_message(
            self.journal,
            raw_text="记一下 准备撤销",
            message_id="m-4",
            sender_id="使用者",
        )
        cancelled = decide_message(
            self.journal,
            raw_text="撤销上一条",
            message_id="m-5",
            sender_id="使用者",
        )
        self.assertEqual(cancelled.action, "cancelled")
        self.assertEqual(cancelled.capture_id, captured.capture_id)
        self.assertEqual(len(self.journal_path.read_text(encoding="utf-8").splitlines()), 2)

    def test_empty_capture_is_intercepted_without_write(self):
        decision = decide_message(
            self.journal,
            raw_text="记一下：  ",
            message_id="m-6",
            sender_id="使用者",
        )
        self.assertEqual(decision.action, "empty")
        self.assertFalse(self.journal_path.exists())


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "ACT"
        (self.vault / "x").mkdir(parents=True)
        self.journal_path = self.root / "journal.jsonl"
        self.journal = CaptureJournal(self.journal_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_import_creates_top_level_x_note_with_two_frontmatter_fields(self):
        raw = "记一下：保留 `代码` 和\n换行"
        decision = self.journal.capture(
            raw_text=raw,
            message_id="m-7",
            sender_id="使用者",
            captured_at="2026-08-16T08:00:00+00:00",
        )
        created = importer.import_captures(
            journal_text=self.journal_path.read_text(encoding="utf-8"),
            vault=self.vault,
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].parent, (self.vault / "x").resolve())
        text = created[0].read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        self.assertEqual(
            [line.split(":", 1)[0] for line in frontmatter.strip().splitlines()],
            ["创建日期", "AI 备注"],
        )
        self.assertIn(raw, text)
        self.assertIn(f"捕获 ID：{decision.capture_id}", text)

    def test_cancelled_capture_is_not_imported(self):
        self.journal.capture(
            raw_text="记一下：不要导入",
            message_id="m-8",
            sender_id="使用者",
        )
        self.journal.cancel_latest(sender_id="使用者")
        created = importer.import_captures(
            journal_text=self.journal_path.read_text(encoding="utf-8"),
            vault=self.vault,
        )
        self.assertEqual(created, [])

    def test_telegram_capture_import_keeps_telegram_source(self):
        self.journal.capture(
            raw_text="记一下：Telegram 来源",
            message_id="tg-8",
            sender_id="使用者",
            channel="telegram_dm",
            captured_at="2026-08-23T01:00:00+00:00",
        )
        created = importer.import_captures(
            journal_text=self.journal_path.read_text(encoding="utf-8"),
            vault=self.vault,
        )
        text = created[0].read_text(encoding="utf-8")
        self.assertIn("来源：Telegram 私聊", text)

    def test_hash_mismatch_fails_without_write(self):
        self.journal.capture(
            raw_text="记一下：不能篡改",
            message_id="m-9",
            sender_id="使用者",
        )
        event = json.loads(self.journal_path.read_text(encoding="utf-8"))
        event["raw_text"] = "记一下：已经篡改"
        bad = json.dumps(event, ensure_ascii=False) + "\n"
        with self.assertRaises(importer.ImportFailure):
            importer.import_captures(journal_text=bad, vault=self.vault)
        self.assertEqual(list((self.vault / "x").iterdir()), [])

    def test_second_import_is_idempotent_even_after_note_moves(self):
        decision = self.journal.capture(
            raw_text="记一下：只导入一次",
            message_id="m-10",
            sender_id="使用者",
        )
        journal_text = self.journal_path.read_text(encoding="utf-8")
        first = importer.import_captures(journal_text=journal_text, vault=self.vault)
        archive = self.vault / "20-Card"
        archive.mkdir()
        first[0].rename(archive / first[0].name)
        second = importer.import_captures(journal_text=journal_text, vault=self.vault)
        self.assertEqual(second, [])
        self.assertTrue(any(decision.capture_id in p.read_text(encoding="utf-8") for p in archive.iterdir()))


if __name__ == "__main__":
    unittest.main()
