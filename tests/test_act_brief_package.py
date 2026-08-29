from __future__ import annotations

import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


VAULT = Path(__file__).resolve().parents[1]
DEPLOY = VAULT / "deploy/hermes-ai-brief"
sys.path.insert(0, str(DEPLOY))

import act_brief_package as package  # noqa: E402


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def news(number: int, url: str | None = None) -> dict[str, object]:
    actual = url or f"https://example.com/{number}"
    return {
        "title": f"重要 AI 更新 {number}",
        "source": "Official Lab",
        "published_at": f"2026-08-18T0{number % 9}:00:00Z",
        "summary": f"第 {number} 条摘要",
        "category": "ai-models",
        "url": actual,
        "canonical_url": actual,
        "impact": "影响模型与工具选择。",
        "is_update": False,
    }


def make_package(count: int, *, first_url: str | None = None) -> dict[str, object]:
    items = [news(index, first_url if index == 1 else None) for index in range(1, count + 1)]
    return package.build_package(
        slot="morning",
        start=dt("2026-08-17T11:00:00Z"),
        end=dt("2026-08-17T23:00:00Z"),
        items=items,
        coverage={"available": ["selected", "all"], "candidate_count": count},
        generated_at=dt("2026-08-18T00:00:00Z"),
    )


class PackageShapeTests(unittest.TestCase):
    def test_card_thresholds_and_four_item_cap(self):
        expected = {0: 0, 1: 1, 4: 1, 5: 2, 8: 2, 9: 3, 12: 3}
        for count, cards in expected.items():
            with self.subTest(count=count):
                groups = package.card_groups(count)
                self.assertEqual(len(groups), cards)
                self.assertTrue(all(len(group) <= 4 for group in groups))

    def test_package_has_stable_numbers_top_link_and_short_guide(self):
        result = make_package(5)
        self.assertEqual([item["number"] for item in result["items"]], [1, 2, 3, 4, 5])
        self.assertEqual(result["top_url"], "https://example.com/1")
        self.assertIn("https://example.com/1", result["guide_text"])
        self.assertIn("展开 2", result["guide_text"])
        self.assertIn("今日重点：___", result["guide_text"])
        self.assertIn("今天最值得先看的是", result["guide_text"])
        self.assertIn("早上好，昨夜新闻已整理", result["guide_text"])
        self.assertIn("今天最重要的一件事是什么", result["guide_text"])
        self.assertIn("帮我从 ACT 里选", result["guide_text"])
        self.assertIn("AIHOT 摘要属于外部整理", result["guide_text"])
        self.assertNotIn("最重要：", result["guide_text"])
        self.assertEqual([card["item_numbers"] for card in result["cards"]], [[1, 2, 3, 4], [5]])

    def test_private_copy_can_be_loaded_from_external_file(self):
        private_copy = dict(package.DEFAULT_BRIEF_COPY)
        private_copy["top_item_prefix"] = "PRIVATE COPY TOP:"
        with tempfile.TemporaryDirectory() as directory:
            copy_path = Path(directory) / "brief-copy.private.json"
            copy_path.write_text(json.dumps(private_copy, ensure_ascii=False), encoding="utf-8")
            with patch.dict(os.environ, {"ACT_BRIEF_COPY_FILE": str(copy_path)}):
                result = make_package(1)
        self.assertIn("PRIVATE COPY TOP:", result["guide_text"])

    def test_private_copy_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            copy_path = Path(directory) / "brief-copy.private.json"
            copy_path.write_text('{"unknown": "value"}', encoding="utf-8")
            with patch.dict(os.environ, {"ACT_BRIEF_COPY_FILE": str(copy_path)}):
                with self.assertRaises(package.PackageError):
                    make_package(1)

    def test_renderer_container_is_digest_pinned_and_isolated_by_wrapper(self):
        dockerfile = (DEPLOY / "container/Dockerfile").read_text(encoding="utf-8")
        wrapper = (DEPLOY / "render-guizang-container.sh").read_text(encoding="utf-8")
        self.assertIn("sha256:9bd26ad900bb5e0f4dee75839e957a89ae89c2b7ab1e76050e559790e946b948", dockerfile)
        self.assertIn('"playwright": "1.60.0"', (DEPLOY / "container/package.json").read_text(encoding="utf-8"))
        for guard in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges"):
            self.assertIn(guard, wrapper)
        self.assertNotIn("/knowledge/ACT", wrapper)

    def test_zero_items_has_no_cards_and_keeps_planning_entry(self):
        result = make_package(0)
        self.assertEqual(result["cards"], [])
        self.assertIn("这个时间窗没有达到门槛", result["guide_text"])
        self.assertIn("今日重点：___", result["guide_text"])


class OfficialImageTests(unittest.TestCase):
    def png(self, width: int = 1200, height: int = 630) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height) + b"payload"

    def test_official_og_success_records_source_hash_and_dimensions(self):
        result = make_package(1, first_url="https://openai.com/news/example")
        html = b'<meta property="og:image" content="https://cdn.openai.com/hero.png">'
        image = self.png()
        responses = [
            ("https://openai.com/news/example", "text/html", html),
            ("https://cdn.openai.com/hero.png", "image/png", image),
        ]
        with tempfile.TemporaryDirectory() as directory, patch(
            "act_brief_package._safe_https_fetch", side_effect=responses
        ):
            evidence = package.acquire_official_image(result, Path(directory))
            self.assertEqual((evidence["width"], evidence["height"]), (1200, 630))
            self.assertEqual(evidence["domain"], "cdn.openai.com")
            self.assertEqual(evidence["sha256"], __import__("hashlib").sha256(image).hexdigest())
            self.assertTrue(Path(evidence["file"]).is_file())

    def test_nonofficial_page_and_no_og_fall_back_without_image(self):
        result = make_package(1, first_url="https://news.example/story")
        with patch("act_brief_package._safe_https_fetch") as fetch:
            self.assertIsNone(package.acquire_official_image(result, Path("/unused")))
            fetch.assert_not_called()
        official = make_package(1, first_url="https://openai.com/news/example")
        with tempfile.TemporaryDirectory() as directory, patch(
            "act_brief_package._safe_https_fetch",
            return_value=("https://openai.com/news/example", "text/html", b"<title>No image</title>"),
        ):
            self.assertIsNone(package.acquire_official_image(official, Path(directory)))
            package.apply_official_image_fallback(official)
            self.assertEqual(official["cards"][0]["layout"], "S01")

    def test_private_dns_wrong_mime_magic_and_oversize_fail_closed(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=fake):
            with self.assertRaises(package.PackageError):
                package._public_addresses("cdn.openai.com")
        with self.assertRaises(package.PackageError):
            package.image_dimensions(self.png(), "image/jpeg")
        result = make_package(1, first_url="https://openai.com/news/example")
        with tempfile.TemporaryDirectory() as directory, patch(
            "act_brief_package._safe_https_fetch",
            side_effect=package.PackageError("官方资源超过大小限制。"),
        ):
            with self.assertRaises(package.PackageError):
                package.acquire_official_image(result, Path(directory))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.package = make_package(5)
        self.images = [self.state / "1.png", self.state / "2.png"]
        for image in self.images:
            image.write_bytes(b"png")
        self.clock = dt("2026-08-18T00:00:00Z")

    def tearDown(self):
        self.temp.cleanup()

    def ticking_now(self):
        self.clock += timedelta(seconds=1)
        return self.clock

    def test_image_two_failure_resumes_without_resending_image_one(self):
        sent: list[str] = []

        def image_sender(path: Path):
            sent.append(path.name)
            if path.name == "2.png":
                raise package.PackageError("send failed")

        with self.assertRaises(package.PackageError):
            package.deliver_resumably(
                package=self.package, state_dir=self.state, media_files=self.images,
                fallback_text="fallback", image_sender=image_sender,
                text_sender=lambda text: sent.append(text), sleeper=lambda _: None,
                now=self.ticking_now,
            )
        self.assertEqual(sent, ["1.png", "2.png"])
        sent.clear()
        manifest = package.deliver_resumably(
            package=self.package, state_dir=self.state, media_files=self.images,
            fallback_text="fallback", image_sender=lambda path: sent.append(path.name),
            text_sender=lambda text: sent.append("text:" + text), sleeper=lambda _: None,
            now=self.ticking_now,
        )
        self.assertEqual(sent[0], "2.png")
        self.assertNotIn("1.png", sent)
        self.assertTrue(manifest["delivered_complete"])

    def test_text_failure_resumes_only_text_and_latest_appears_on_completion(self):
        image_sent: list[str] = []
        with self.assertRaises(package.PackageError):
            package.deliver_resumably(
                package=self.package, state_dir=self.state, media_files=self.images[:1],
                fallback_text="fallback", image_sender=lambda path: image_sent.append(path.name),
                text_sender=lambda _text: (_ for _ in ()).throw(package.PackageError("text failed")),
                sleeper=lambda _: None, now=self.ticking_now,
            )
        self.assertEqual(image_sent, ["1.png"])
        self.assertFalse((self.state / "delivery-manifests/latest.json").exists())
        image_sent.clear()
        texts: list[str] = []
        package.deliver_resumably(
            package=self.package, state_dir=self.state, media_files=self.images[:1],
            fallback_text="fallback", image_sender=lambda path: image_sent.append(path.name),
            text_sender=texts.append, sleeper=lambda _: None, now=self.ticking_now,
        )
        self.assertEqual(image_sent, [])
        self.assertEqual(len(texts), 1)
        self.assertTrue((self.state / "delivery-manifests/latest.json").is_file())

    def test_no_media_uses_full_text_fallback(self):
        texts: list[str] = []
        package.deliver_resumably(
            package=self.package, state_dir=self.state, media_files=[],
            fallback_text="full 1900-safe text", image_sender=lambda _path: None,
            text_sender=texts.append, sleeper=lambda _: None, now=self.ticking_now,
        )
        self.assertEqual(texts, ["full 1900-safe text"])

    def test_oldest_pending_manifest_is_recovered_across_slots(self):
        older = make_package(1)
        newer = make_package(1)
        older["brief_id"] = "20260817T2300Z-morning-aaaaaaaaaaaa"
        older["slot"] = "morning"
        older["window"] = {
            "start": "2026-08-17T11:00:00Z",
            "end": "2026-08-17T23:00:00Z",
        }
        newer["brief_id"] = "20260818T1100Z-evening-bbbbbbbbbbbb"
        newer["slot"] = "evening"
        newer["window"] = {
            "start": "2026-08-17T23:00:00Z",
            "end": "2026-08-18T11:00:00Z",
        }
        for brief in (newer, older):
            with self.assertRaises(package.PackageError):
                package.deliver_resumably(
                    package=brief, state_dir=self.state, media_files=[], fallback_text="pending",
                    image_sender=lambda _path: None,
                    text_sender=lambda _text: (_ for _ in ()).throw(package.PackageError("wait")),
                    sleeper=lambda _: None, now=self.ticking_now,
                )
        pending = package.find_oldest_pending_manifest(self.state)
        self.assertEqual(pending["brief_id"], older["brief_id"])


class LatestItemTests(unittest.TestCase):
    def test_valid_out_of_range_expired_and_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            brief = make_package(2)
            package.deliver_resumably(
                package=brief, state_dir=state, media_files=[], fallback_text="ok",
                image_sender=lambda _path: None, text_sender=lambda _text: None,
                sleeper=lambda _: None, now=lambda: dt("2026-08-18T00:00:00Z"),
            )
            item = package.read_latest_brief_item(state, 2, now=dt("2026-08-18T01:00:00Z"))
            self.assertEqual(item["number"], 2)
            self.assertEqual(item["item_count"], 2)
            self.assertEqual(item["url"], "https://example.com/2")
            with self.assertRaises(package.PackageError):
                package.read_latest_brief_item(state, 3, now=dt("2026-08-18T01:00:00Z"))
            with self.assertRaises(package.PackageError):
                package.read_latest_brief_item(state, 1, now=dt("2026-08-20T01:00:01Z"))
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(package.PackageError):
                package.read_latest_brief_item(Path(empty), 1)


if __name__ == "__main__":
    unittest.main()
