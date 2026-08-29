#!/usr/bin/env python3
"""Container-only smoke test for the Hermes Gateway hook integration."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace


PLUGIN_DIR = Path(__file__).resolve().parent


class FakeContext:
    def __init__(self):
        self.callback = None

    def register_hook(self, name, callback):
        assert name == "pre_gateway_dispatch"
        self.callback = callback


class FakeAdapter:
    def __init__(self):
        self.replies = []

    async def send(self, chat_id, message):
        self.replies.append((chat_id, message))


class FakeGateway:
    def __init__(self):
        self.adapter = FakeAdapter()
        self.authorized = True

    def _is_user_authorized(self, _source):
        return self.authorized

    def _adapter_for_source(self, _source):
        return self.adapter


def load_plugin():
    parent_name = "hermes_plugins"
    if parent_name not in sys.modules:
        parent = types.ModuleType(parent_name)
        parent.__path__ = []
        sys.modules[parent_name] = parent
    module_name = "hermes_plugins.act_controlled_capture_smoke"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def event(text, message_id="m-1", platform="weixin"):
    source = SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        chat_type="dm",
        user_id="authorized-example-user",
        chat_id="home-channel",
    )
    raw_message = (
        {"item_list": [{"type": 1, "text_item": {"text": text}}]}
        if platform == "weixin"
        else SimpleNamespace(text=text)
    )
    return SimpleNamespace(
        text=text + "\n这段是批处理追加文本，不得进入捕获",
        raw_message=raw_message,
        message_id=message_id,
        source=source,
    )


async def main():
    with tempfile.TemporaryDirectory(prefix="act-capture-smoke-") as temp_dir:
        os.environ["HERMES_HOME"] = temp_dir
        plugin = load_plugin()
        ctx = FakeContext()
        plugin.register(ctx)
        assert ctx.callback is not None
        gateway = FakeGateway()

        assert ctx.callback(event=event("普通聊天", "m-ordinary"), gateway=gateway) is None
        journal = Path(temp_dir) / "act-capture" / "journal.jsonl"
        assert not journal.exists()

        capture_text = "记一下：服务器合成验收"
        result = ctx.callback(event=event(capture_text, "m-capture"), gateway=gateway)
        assert result and result["action"] == "skip"
        await asyncio.sleep(0)

        duplicate = ctx.callback(event=event(capture_text, "m-capture"), gateway=gateway)
        assert duplicate and duplicate["action"] == "skip"
        await asyncio.sleep(0)

        cancel = ctx.callback(event=event("撤销上一条", "m-cancel"), gateway=gateway)
        assert cancel and cancel["action"] == "skip"
        await asyncio.sleep(0)

        gateway.authorized = False
        assert ctx.callback(event=event("记一下：未授权不得捕获", "m-denied"), gateway=gateway) is None

        lines = journal.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        captured = json.loads(lines[0])
        cancelled = json.loads(lines[1])
        assert captured["raw_text"] == capture_text
        assert "批处理追加文本" not in captured["raw_text"]
        assert cancelled["event"] == "cancel"
        assert cancelled["capture_id"] == captured["capture_id"]
        assert len(gateway.adapter.replies) == 3

        gateway.authorized = True
        telegram_text = "记一下：Telegram 原始文本"
        telegram = ctx.callback(
            event=event(telegram_text, "tg-capture", platform="telegram"),
            gateway=gateway,
        )
        assert telegram and telegram["action"] == "skip"
        await asyncio.sleep(0)
        telegram_event = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
        assert telegram_event["channel"] == "telegram_dm"
        assert telegram_event["raw_text"] == telegram_text
        print("PASS: gateway hook weixin/telegram capture/auth/raw-text isolation")

if __name__ == "__main__":
    asyncio.run(main())
