#!/usr/bin/env python3
"""Reject non-zero Weixin media business responses instead of trusting HTTP 200."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys


EXPECTED_SOURCE_SHA256 = "3354b015bd56c300bf251b131116714fbc0912462d4fdb8dd5311ad9d4e0e9a2"

OLD_BLOCK = '''        last_message_id = None
        if caption:
            last_message_id = f"hermes-weixin-{uuid.uuid4().hex}"
            await _send_message(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                to=chat_id,
                text=self.format_message(caption),
                context_token=context_token,
                client_id=last_message_id,
            )

        last_message_id = f"hermes-weixin-{uuid.uuid4().hex}"
        await _api_post(
            self._send_session,
            base_url=self._base_url,
            endpoint=EP_SEND_MESSAGE,
            payload={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": last_message_id,
                    "message_type": MSG_TYPE_BOT,
                    "message_state": MSG_STATE_FINISH,
                    "item_list": [media_item],
                    **({"context_token": context_token} if context_token else {}),
                }
            },
            token=self._token,
            timeout_ms=API_TIMEOUT_MS,
        )
        return last_message_id
'''

NEW_BLOCK = '''        if caption:
            caption_message_id = f"hermes-weixin-{uuid.uuid4().hex}"
            await self._send_text_chunk(
                chat_id=chat_id,
                chunk=self.format_message(caption),
                context_token=context_token,
                client_id=caption_message_id,
            )

        last_message_id = f"hermes-weixin-{uuid.uuid4().hex}"
        retried_without_token = False
        async with self._send_text_gate:
            if self._rate_limit_cooldown_remaining() > 0:
                raise self._rate_limit_error()
            while True:
                response = await _api_post(
                    self._send_session,
                    base_url=self._base_url,
                    endpoint=EP_SEND_MESSAGE,
                    payload={
                        "msg": {
                            "from_user_id": "",
                            "to_user_id": chat_id,
                            "client_id": last_message_id,
                            "message_type": MSG_TYPE_BOT,
                            "message_state": MSG_STATE_FINISH,
                            "item_list": [media_item],
                            **({"context_token": context_token} if context_token else {}),
                        }
                    },
                    token=self._token,
                    timeout_ms=API_TIMEOUT_MS,
                )
                if not isinstance(response, dict):
                    raise RuntimeError(
                        f"iLink media sendmessage returned invalid response: {type(response).__name__}"
                    )
                ret = response.get("ret")
                errcode = response.get("errcode")
                if (ret is None or ret == 0) and (errcode is None or errcode == 0):
                    self._reset_rate_limit_circuit()
                    return last_message_id

                errmsg = response.get("errmsg") or response.get("msg") or "unknown error"
                is_session_expired = (
                    ret == SESSION_EXPIRED_ERRCODE
                    or errcode == SESSION_EXPIRED_ERRCODE
                    or _is_stale_session_ret(ret, errcode, response.get("errmsg"))
                )
                if is_session_expired and not retried_without_token and context_token:
                    retried_without_token = True
                    context_token = None
                    self._token_store._cache.pop(
                        self._token_store._key(self._account_id, chat_id), None
                    )
                    logger.warning(
                        "[%s] media session expired for %s; retrying without context_token",
                        self.name, _safe_id(chat_id),
                    )
                    continue

                if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE:
                    self._record_rate_limit_event()
                    raise RuntimeError(
                        f"iLink media sendmessage rate limited: ret={ret} "
                        f"errcode={errcode} errmsg={errmsg}"
                    )
                raise RuntimeError(
                    f"iLink media sendmessage error: ret={ret} errcode={errcode} errmsg={errmsg}"
                )
'''


def patch_text(source: str) -> str:
    occurrences = source.count(OLD_BLOCK)
    if occurrences != 1:
        raise RuntimeError(f"expected one media send block, found {occurrences}")
    return source.replace(OLD_BLOCK, NEW_BLOCK, 1)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_weixin_media_ack.py PATH_TO_WEIXIN_PY")

    source_path = Path(sys.argv[1])
    source_bytes = source_path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    if source_hash != EXPECTED_SOURCE_SHA256:
        raise RuntimeError(
            f"refusing to patch unexpected source: {source_hash} != {EXPECTED_SOURCE_SHA256}"
        )

    patched = patch_text(source_bytes.decode("utf-8"))
    compile(patched, str(source_path), "exec")

    temporary_path = source_path.with_suffix(source_path.suffix + ".tmp")
    temporary_path.write_text(patched, encoding="utf-8")
    os.chmod(temporary_path, source_path.stat().st_mode)
    os.replace(temporary_path, source_path)
    print(f"patched {source_path} sha256={hashlib.sha256(patched.encode()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
