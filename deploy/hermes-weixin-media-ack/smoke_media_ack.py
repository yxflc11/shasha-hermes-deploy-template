#!/usr/bin/env python3
"""Offline smoke tests for the patched Weixin media acknowledgement path."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace

from gateway.platforms import weixin


class TokenStore:
    def __init__(self, context_token: str | None) -> None:
        self.context_token = context_token
        self._cache = {"account:chat": context_token}

    def get(self, account_id: str, chat_id: str) -> str | None:
        return self.context_token

    def _key(self, account_id: str, chat_id: str) -> str:
        return f"{account_id}:{chat_id}"


def adapter(context_token: str | None = "stale-token"):
    instance = weixin.WeixinAdapter.__new__(weixin.WeixinAdapter)
    instance._send_session = object()
    instance._token = "test-token"
    instance._base_url = "https://invalid.example"
    instance._cdn_base_url = "https://invalid.example"
    instance.platform = SimpleNamespace(value="weixin")
    instance._account_id = "account"
    instance._token_store = TokenStore(context_token)
    instance._send_text_gate = asyncio.Lock()
    instance._rate_limit_cooldown_remaining = lambda: 0.0
    instance._rate_limit_error = lambda: RuntimeError("cooldown")
    instance._reset_rate_limit_circuit = lambda: None
    instance._record_rate_limit_event = lambda: False
    instance._outbound_media_builder = lambda path, force_file_attachment=False: (
        weixin.MEDIA_IMAGE,
        lambda **kwargs: {"type": weixin.ITEM_IMAGE, "image_item": {"test": True}},
    )
    return instance


async def exercise(responses: list[dict], context_token: str | None = "stale-token"):
    calls: list[dict] = []

    async def fake_get_upload_url(*args, **kwargs):
        return {"upload_full_url": "https://invalid.example/upload"}

    async def fake_upload(*args, **kwargs):
        return "encrypted-query"

    async def fake_post(*args, **kwargs):
        calls.append(kwargs["payload"])
        return responses.pop(0)

    original_get_upload_url = weixin._get_upload_url
    original_upload = weixin._upload_ciphertext
    original_post = weixin._api_post
    weixin._get_upload_url = fake_get_upload_url
    weixin._upload_ciphertext = fake_upload
    weixin._api_post = fake_post
    try:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "card.png"
            image_path.write_bytes(b"not-a-real-png-but-encryption-is-real")
            result = await adapter(context_token)._send_file("chat", str(image_path), "")
            return result, calls
    finally:
        weixin._get_upload_url = original_get_upload_url
        weixin._upload_ciphertext = original_upload
        weixin._api_post = original_post


async def main() -> None:
    message_id, calls = await exercise([{"ret": 0}])
    assert message_id.startswith("hermes-weixin-")
    assert len(calls) == 1

    try:
        await exercise([{"ret": -2, "errmsg": "too frequent"}], context_token=None)
    except RuntimeError as exc:
        assert "rate limited" in str(exc)
    else:
        raise AssertionError("rate-limit response was falsely accepted")

    message_id, calls = await exercise(
        [{"ret": -2, "errmsg": "unknown error"}, {"ret": 0}]
    )
    assert message_id.startswith("hermes-weixin-")
    assert len(calls) == 2
    assert "context_token" in calls[0]["msg"]
    assert "context_token" not in calls[1]["msg"]

    message_id, calls = await exercise([{}], context_token=None)
    assert message_id.startswith("hermes-weixin-")
    assert len(calls) == 1

    try:
        await exercise([{"ret": 0, "errcode": 9, "errmsg": "rejected"}], context_token=None)
    except RuntimeError as exc:
        assert "errcode=9" in str(exc)
    else:
        raise AssertionError("non-zero errcode was falsely accepted")

    print("media acknowledgement smoke tests: 5 passed")


if __name__ == "__main__":
    asyncio.run(main())
