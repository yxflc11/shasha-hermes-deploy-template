from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


VAULT = Path(__file__).resolve().parents[1]
MODULE_PATH = VAULT / "deploy/hermes-ai-brief/act_ai_brief.py"
SPEC = importlib.util.spec_from_file_location("act_ai_brief", MODULE_PATH)
assert SPEC and SPEC.loader
brief = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = brief
SPEC.loader.exec_module(brief)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def item(
    number: int,
    *,
    category: str = "ai-models",
    score: int = 60,
    selected: bool = False,
    title: str | None = None,
    summary: str = "这是外部摘要，用于验证简报结构。",
    published_at: datetime | None = None,
):
    return brief.NewsItem(
        item_id=f"item-{number}",
        title=title or f"模型公司发布重要更新 {number}",
        source="Official AI Lab",
        published_at=published_at or dt("2026-08-16T10:00:00Z") + timedelta(minutes=number),
        summary=summary,
        category=category,
        url=f"https://example.com/news/{number}?utm_source=test",
        canonical_url=f"https://example.com/news/{number}",
        selected=selected,
        score=score,
    )


class WindowTests(unittest.TestCase):
    def test_evening_and_morning_use_beijing_boundaries(self):
        evening = brief.compute_window(
            "evening", dt("2026-08-16T14:00:00Z"), {}
        )
        self.assertEqual(evening.start, dt("2026-08-15T23:00:00Z"))
        self.assertEqual(evening.end, dt("2026-08-16T11:00:00Z"))
        self.assertEqual(evening.fetch_start, dt("2026-08-15T21:00:00Z"))

        morning = brief.compute_window(
            "morning", dt("2026-08-16T00:10:00Z"), {}
        )
        self.assertEqual(morning.start, dt("2026-08-15T11:00:00Z"))
        self.assertEqual(morning.end, dt("2026-08-15T23:00:00Z"))

    def test_failed_prior_window_is_recovered_and_repeat_is_skipped(self):
        state = {"last_success_end": "2026-08-15T09:00:00Z"}
        window = brief.compute_window("morning", dt("2026-08-16T00:10:00Z"), state)
        self.assertEqual(window.start, dt("2026-08-15T09:00:00Z"))

        complete = brief.compute_window(
            "morning",
            dt("2026-08-16T00:10:00Z"),
            {"last_success_end": "2026-08-15T23:00:00Z"},
        )
        self.assertTrue(complete.already_complete)

    def test_latest_due_slot_uses_beijing_0700_and_1900_boundaries(self):
        self.assertEqual(brief.latest_due_slot(dt("2026-08-17T22:59:00Z")), "evening")
        self.assertEqual(brief.latest_due_slot(dt("2026-08-17T23:00:00Z")), "morning")
        self.assertEqual(brief.latest_due_slot(dt("2026-08-18T10:59:00Z")), "morning")
        self.assertEqual(brief.latest_due_slot(dt("2026-08-18T11:00:00Z")), "evening")


class DedupeTests(unittest.TestCase):
    def test_url_tracking_is_removed(self):
        canonical = brief.canonicalize_url(
            "https://Example.com/a?utm_source=x&gclid=y&keep=1#fragment"
        )
        self.assertEqual(canonical, "https://example.com/a?keep=1")

    def test_exact_and_near_duplicate_history_are_suppressed(self):
        current = item(1, title="OpenAI 正式发布全新模型")
        history = [
            {
                "item_id": "old-id",
                "canonical_url": "https://different.example/story",
                "title_norm": brief._title_norm("OpenAI 发布全新模型"),
                "summary": current.summary,
                "published_at": "2026-08-16T08:00:00Z",
                "sent_at": "2026-08-16T08:10:00Z",
            }
        ]
        self.assertEqual(brief.classify_history(current, history), "duplicate")
        exact = [dict(history[0], item_id="item-1")]
        self.assertEqual(brief.classify_history(current, exact), "duplicate")

    def test_material_progress_can_be_reported_as_update(self):
        current = item(
            2,
            title="OpenAI 模型事件确认修复进展",
            summary="官方确认已经修复此前故障，并重新开放服务。",
            published_at=dt("2026-08-16T12:00:00Z"),
        )
        history = [
            {
                "item_id": "old",
                "canonical_url": "https://example.com/old",
                "title_norm": brief._title_norm("OpenAI 模型事件修复进展"),
                "summary": "此前服务发生大范围故障，原因仍在调查。",
                "published_at": "2026-08-16T08:00:00Z",
                "sent_at": "2026-08-16T08:30:00Z",
            }
        ]
        self.assertEqual(brief.classify_history(current, history), "update")

    def test_tip_cap_and_total_limits(self):
        candidates = [item(index, category="tip", score=50) for index in range(1, 7)]
        candidates += [item(index, category="industry", score=70) for index in range(10, 20)]
        groups = brief.select_items("morning", candidates, [])
        chosen = groups["must"] + groups["glance"]
        self.assertLessEqual(len(chosen), 8)
        self.assertLessEqual(sum(value.category == "tip" for value in chosen), 1)

    def test_same_named_project_from_different_sources_is_one_story(self):
        first = item(
            30,
            title="DeepSeek Harness 三天获得大量 GitHub 星标",
            summary="DeepSeek Harness 是可组合的智能体编排层，并采用 MIT 许可。",
            score=90,
        )
        second = item(
            31,
            title="DeepSeek 开源 Harness 及内部工程 Skill",
            summary="DeepSeek Harness 仓库包含多个工程 Skill 和编排组件。",
            score=80,
        )
        groups = brief.select_items("evening", [first, second], [])
        chosen = groups["must"] + groups["glance"]
        self.assertEqual(len(chosen), 1)


class DeliveryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "state"
        self.state_dir.mkdir()
        self.now = dt("2026-08-16T14:00:00Z")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def provider(_start, _end):
        return [item(21, score=90, selected=True)], {
            "available": ["selected", "all"],
            "failures": [],
            "partial": False,
            "candidate_count": 1,
        }

    def test_state_advances_only_after_successful_send(self):
        sent: list[str] = []

        def sender(message, _state_dir):
            sent.append(message)

        result = brief.run_brief(
            "evening",
            now=self.now,
            state_dir=self.state_dir,
            candidate_provider=self.provider,
            sender=sender,
        )
        self.assertEqual(result.sent_count, 1)
        self.assertEqual(len(sent), 1)
        state = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_success_end"], "2026-08-16T11:00:00Z")
        self.assertEqual(len(state["sent"]), 1)

        repeat = brief.run_brief(
            "evening",
            now=self.now,
            state_dir=self.state_dir,
            candidate_provider=self.provider,
            sender=sender,
        )
        self.assertTrue(repeat.skipped)
        self.assertEqual(len(sent), 1)

    def test_failed_send_does_not_create_state(self):
        def failing_sender(_message, _state_dir):
            raise brief.BriefError("send failed")

        with self.assertRaises(brief.BriefError):
            brief.run_brief(
                "evening",
                now=self.now,
                state_dir=self.state_dir,
                candidate_provider=self.provider,
                sender=failing_sender,
            )
        self.assertFalse((self.state_dir / "state.json").exists())

    def test_dry_run_never_sends_or_updates(self):
        def unexpected_sender(_message, _state_dir):
            self.fail("dry-run must not call sender")

        result = brief.run_brief(
            "morning",
            now=dt("2026-08-16T00:10:00Z"),
            state_dir=self.state_dir,
            dry_run=True,
            candidate_provider=self.provider,
            sender=unexpected_sender,
        )
        self.assertTrue(result.dry_run)
        self.assertIn("昨夜 AI 大事", result.message)
        self.assertIn("近 14 天", result.message)
        self.assertIn("今日重点：___", result.message)
        self.assertIn("昨夜新闻已整理", result.message)
        self.assertFalse((self.state_dir / "state.json").exists())

    def test_evening_message_contains_wrap_guidance(self):
        window = brief.compute_window("evening", dt("2026-08-16T14:00:00Z"), {})
        message = brief.build_message(
            "evening",
            window,
            {"must": [], "glance": [], "updates": []},
            {"failures": [], "partial": False},
        )
        self.assertIn("今日收尾：推进___", message)
        self.assertIn("帮我一步步回顾", message)
        self.assertIn("今天的新闻已整理", message)

    @patch("act_ai_brief.subprocess.run")
    @patch("act_ai_brief.shutil.which", return_value="/opt/hermes/bin/hermes")
    def test_weixin_sender_waits_past_ilink_cooldown_between_chunks(self, _which, run):
        run.return_value = SimpleNamespace(returncode=0, stderr="")
        brief.send_brief("short preview", self.state_dir)
        self.assertEqual(
            run.call_args.kwargs["env"]["WEIXIN_SEND_CHUNK_DELAY_SECONDS"],
            "35",
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 90)
        self.assertIn("--json", run.call_args.args[0])
        self.assertIn("weixin", run.call_args.args[0])

    @patch.dict("os.environ", {"ACT_BRIEF_TARGET": "telegram"})
    @patch("act_ai_brief.subprocess.run")
    @patch("act_ai_brief.shutil.which", return_value="/opt/hermes/bin/hermes")
    def test_telegram_sender_uses_home_target_without_weixin_delay(self, _which, run):
        run.return_value = SimpleNamespace(returncode=0, stderr="")
        brief.send_brief("short preview", self.state_dir)
        self.assertIn("telegram", run.call_args.args[0])
        self.assertNotIn("WEIXIN_SEND_CHUNK_DELAY_SECONDS", run.call_args.kwargs["env"])

    @patch("act_ai_brief.subprocess.run")
    @patch("act_ai_brief.shutil.which", return_value="/opt/hermes/bin/hermes")
    def test_image_sender_uses_media_protocol(self, _which, run):
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        image = self.state_dir / "card.png"
        image.write_bytes(b"png")
        brief.send_brief_image(image)
        self.assertIn(f"MEDIA:{image}", run.call_args.args[0])
        self.assertIn("--json", run.call_args.args[0])

    @patch("act_ai_brief._render_visual_package", return_value=[])
    def test_render_failure_falls_back_to_text_and_advances_after_send(self, _render):
        sent = []
        result = brief.run_brief(
            "evening", now=self.now, state_dir=self.state_dir,
            candidate_provider=self.provider,
            sender=lambda message, _state: sent.append(message),
            visual_delivery=True,
        )
        self.assertEqual(result.sent_count, 1)
        self.assertEqual(len(sent), 1)
        self.assertTrue((self.state_dir / "state.json").is_file())
        manifest = next((self.state_dir / "delivery-manifests").glob("*/manifest.json"))
        self.assertTrue(json.loads(manifest.read_text(encoding="utf-8"))["delivered_complete"])

    def test_zero_item_visual_brief_sends_short_guide_without_image(self):
        sent = []

        def empty_provider(_start, _end):
            return [], {"available": ["selected", "all"], "failures": [], "partial": False, "candidate_count": 0}

        result = brief.run_brief(
            "morning", now=dt("2026-08-16T00:10:00Z"), state_dir=self.state_dir,
            candidate_provider=empty_provider,
            sender=lambda message, _state: sent.append(message),
            visual_delivery=True,
        )
        self.assertEqual(sent, [result.package["guide_text"]])
        self.assertEqual(result.package["cards"], [])

    @patch("act_ai_brief.send_brief_image", side_effect=brief.BriefError("image failed"))
    @patch("act_ai_brief._render_visual_package")
    def test_image_failure_preserves_manifest_without_advancing_state(self, render, _send_image):
        image = self.state_dir / "card.png"
        image.write_bytes(b"png")
        render.return_value = [image]
        with self.assertRaises(brief.BriefError):
            brief.run_brief(
                "evening", now=self.now, state_dir=self.state_dir,
                candidate_provider=self.provider,
                sender=lambda _message, _state: None,
                visual_delivery=True,
            )
        self.assertFalse((self.state_dir / "state.json").exists())
        manifest = next((self.state_dir / "delivery-manifests").glob("*/manifest.json"))
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertFalse(payload["delivered_complete"])
        self.assertIsNone(payload["parts"][0]["sent_at"])

    def test_budget_keeps_timed_brief_in_one_payload(self):
        window = brief.compute_window("evening", dt("2026-08-16T14:00:00Z"), {})
        groups = {
            "must": [item(index, title=("重要模型更新" * 10)) for index in range(1, 6)],
            "glance": [item(index, title=("值得关注的产品变化" * 8)) for index in range(6, 13)],
            "updates": [],
        }
        fitted, message = brief.fit_message_budget(
            "evening",
            window,
            groups,
            {"failures": [], "partial": False},
        )
        self.assertLessEqual(len(message), 1900)
        self.assertGreater(sum(len(values) for values in fitted.values()), 0)
        self.assertIn("今日收尾：推进___", message)

    @patch("act_ai_brief._render_visual_package", return_value=[])
    def test_retry_only_recovers_prior_slot_without_selecting_new_news(self, _render):
        morning_now = dt("2026-08-16T00:10:00Z")
        with self.assertRaises(brief.BriefError):
            brief.run_brief(
                "morning", now=morning_now, state_dir=self.state_dir,
                candidate_provider=self.provider,
                sender=lambda _message, _state: (_ for _ in ()).throw(brief.BriefError("text failed")),
                visual_delivery=True,
            )
        delivered: list[str] = []

        def unexpected_provider(_start, _end):
            self.fail("retry-only must not fetch or select a new window")

        result = brief.run_brief(
            "evening", now=self.now, state_dir=self.state_dir,
            candidate_provider=unexpected_provider,
            sender=lambda message, _state: delivered.append(message),
            visual_delivery=True,
            retry_pending_only=True,
        )
        self.assertEqual(result.slot, "morning")
        self.assertEqual(len(delivered), 1)
        state = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_success_end"], "2026-08-15T23:00:00Z")
        self.assertEqual(state["last_slot"], "morning")

    def test_retry_only_without_pending_is_a_noop(self):
        result = brief.run_brief(
            "morning", now=self.now, state_dir=self.state_dir,
            candidate_provider=lambda _start, _end: self.fail("must not fetch"),
            visual_delivery=True,
            retry_pending_only=True,
        )
        self.assertTrue(result.skipped)
        self.assertFalse((self.state_dir / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
