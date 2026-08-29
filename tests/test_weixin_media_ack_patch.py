from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "hermes-weixin-media-ack"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


patcher = load_module("weixin_media_ack_patcher", DEPLOY / "patch_weixin_media_ack.py")
web_patcher = load_module(
    "web_extract_single_url_patcher", DEPLOY / "patch_web_extract_single_url.py"
)
repair = load_module("weixin_media_ack_repair", DEPLOY / "repair_false_media_ack.py")
confirm = load_module("weixin_media_ack_confirm", DEPLOY / "confirm_observed_delivery.py")


class PatchTests(unittest.TestCase):
    def test_exact_block_is_replaced_with_business_error_logic(self):
        patched = patcher.patch_text(patcher.OLD_BLOCK)
        self.assertNotIn(patcher.OLD_BLOCK, patched)
        self.assertIn("ret == 0", patched)
        self.assertIn("errcode == 0", patched)
        self.assertIn("media sendmessage rate limited", patched)
        self.assertNotIn("no business acknowledgement", patched)

    def test_absent_or_duplicate_block_is_rejected(self):
        with self.assertRaises(RuntimeError):
            patcher.patch_text("")
        with self.assertRaises(RuntimeError):
            patcher.patch_text(patcher.OLD_BLOCK + patcher.OLD_BLOCK)

    def test_web_extract_is_hard_limited_to_one_url(self):
        source = web_patcher.OLD_GUARD_BLOCK + web_patcher.OLD_SCHEMA_BLOCK
        patched = web_patcher.patch_text(source)
        self.assertIn("len(urls) != 1", patched)
        self.assertIn('"minItems": 1', patched)
        self.assertIn('"maxItems": 1', patched)
        self.assertIn("exactly one URL per call", patched)

    def test_web_extract_patch_rejects_source_drift(self):
        with self.assertRaises(RuntimeError):
            web_patcher.patch_text("")
        with self.assertRaises(RuntimeError):
            web_patcher.patch_text(
                web_patcher.OLD_GUARD_BLOCK
                + web_patcher.OLD_SCHEMA_BLOCK
                + web_patcher.OLD_SCHEMA_BLOCK
            )

    def test_image_and_compose_inputs_are_pinned(self):
        dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "deploy" / "hermes-p2-compose.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("@sha256:e0df6adebddf29b91112aefc999d4aaf6846c9eb544faca5672a16a13590ff79", dockerfile)
        self.assertIn("patch_web_extract_single_url.py", dockerfile)
        self.assertIn("image: ${HERMES_IMAGE", compose)
        self.assertIn("${ACT_VAULT_DIR", compose)
        self.assertIn(":/knowledge/ACT:ro", compose)
        self.assertIn("command:\n      - gateway\n      - run", compose)
        self.assertIn("HERMES_DASHBOARD: \"1\"", compose)
        self.assertIn("HERMES_DASHBOARD_HOST: 0.0.0.0", compose)
        self.assertIn("${HERMES_DASHBOARD_BIND:-127.0.0.1}", compose)
        self.assertIn("${HERMES_DASHBOARD_PORT:-9119}:9119", compose)
        self.assertIn("/knowledge/ACT:ro", compose)


class RepairTests(unittest.TestCase):
    def test_false_image_receipts_are_invalidated_with_history(self):
        manifest = {
            "brief_id": "brief",
            "completed_at": None,
            "delivered_complete": False,
            "parts": [
                {"id": "image-01", "kind": "image", "sent_at": "old-1"},
                {"id": "image-02", "kind": "image", "sent_at": "old-2"},
                {"id": "text", "kind": "text", "sent_at": None},
            ],
        }
        repaired = repair.invalidate(manifest, brief_id="brief", reason="confirmed missing")
        self.assertEqual([part["sent_at"] for part in repaired["parts"]], [None, None, None])
        self.assertEqual(
            repaired["repair_history"][0]["invalidated"],
            [
                {"id": "image-01", "sent_at": "old-1"},
                {"id": "image-02", "sent_at": "old-2"},
            ],
        )

    def test_completed_manifest_is_never_rewritten(self):
        manifest = {
            "brief_id": "brief",
            "completed_at": "done",
            "delivered_complete": True,
            "parts": [{"id": "image-01", "kind": "image", "sent_at": "old"}],
        }
        with self.assertRaises(RuntimeError):
            repair.invalidate(manifest, brief_id="brief", reason="should fail")

    def test_user_observed_image_can_be_reconciled_once(self):
        manifest = {
            "brief_id": "brief",
            "completed_at": None,
            "delivered_complete": False,
            "parts": [{"id": "image-01", "kind": "image", "sent_at": None}],
        }
        reconciled = confirm.confirm(
            manifest,
            brief_id="brief",
            part_id="image-01",
            sent_at="2026-08-18T04:28:58Z",
            evidence="user confirmed visible",
        )
        self.assertEqual(reconciled["parts"][0]["sent_at"], "2026-08-18T04:28:58Z")
        self.assertEqual(
            reconciled["observed_delivery_history"][0]["evidence"],
            "user confirmed visible",
        )
        with self.assertRaises(RuntimeError):
            confirm.confirm(
                reconciled,
                brief_id="brief",
                part_id="image-01",
                sent_at="2026-08-18T04:28:58Z",
                evidence="duplicate",
            )


if __name__ == "__main__":
    unittest.main()
