"""Hermes Gateway hook for ACT controlled capture.

Only authorized Weixin or Telegram DM text beginning with ``记一下`` is
intercepted. The exact inbound platform text is written outside the ACT mount;
the LLM never receives or rewrites a capture command.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

from .capture_core import CaptureJournal, Decision, decide_message


logger = logging.getLogger(__name__)

CHANNELS = {
    "weixin": "weixin_home",
    "telegram": "telegram_dm",
}


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower()


def _original_weixin_text(event: Any) -> Optional[str]:
    """Extract only the first original text item, not Weixin's batched text."""
    raw_message = getattr(event, "raw_message", None)
    if not isinstance(raw_message, dict):
        return None
    item_list = raw_message.get("item_list")
    if not isinstance(item_list, list):
        return None
    for item in item_list:
        if not isinstance(item, dict) or item.get("type") != 1:
            continue
        text_item = item.get("text_item")
        if not isinstance(text_item, dict):
            return None
        text = text_item.get("text")
        return text if isinstance(text, str) else None
    return None


def _original_telegram_text(event: Any) -> Optional[str]:
    """Extract Telegram's original text field; captions/media are excluded."""
    raw_message = getattr(event, "raw_message", None)
    text = getattr(raw_message, "text", None)
    return text if isinstance(text, str) else None


def _original_text(event: Any, platform: str) -> Optional[str]:
    if platform == "weixin":
        return _original_weixin_text(event)
    if platform == "telegram":
        return _original_telegram_text(event)
    return None


def _schedule_reply(gateway: Any, event: Any, message: str) -> None:
    try:
        adapter = gateway._adapter_for_source(event.source)
        if adapter is None:
            raise RuntimeError("adapter unavailable")
        loop = asyncio.get_running_loop()
        loop.create_task(adapter.send(event.source.chat_id, message))
    except Exception as exc:
        logger.warning("ACT capture acknowledgement failed: %s", exc)


def _handle_capture(
    *,
    event: Any,
    gateway: Any,
    journal: CaptureJournal,
) -> Optional[dict[str, str]]:
    source = getattr(event, "source", None)
    platform = _enum_value(getattr(source, "platform", None)) if source else ""
    if source is None or platform not in CHANNELS:
        return None
    if _enum_value(getattr(source, "chat_type", None)) != "dm":
        return None

    # The hook runs before Hermes auth. Reuse the gateway's own authorization
    # decision and fail closed if the API is unavailable.
    is_authorized = getattr(gateway, "_is_user_authorized", None)
    if not callable(is_authorized) or not is_authorized(source):
        return None

    raw_text = _original_text(event, platform)
    if raw_text is None:
        return None
    sender_id = str(getattr(source, "user_id", "") or "")
    message_id = str(getattr(event, "message_id", "") or "")
    if not sender_id or not message_id:
        # Idempotence is mandatory. Without both identifiers, let normal chat
        # continue rather than creating an untraceable capture.
        return None

    decision: Decision = decide_message(
        journal,
        raw_text=raw_text,
        message_id=message_id,
        sender_id=sender_id,
        channel=CHANNELS[platform],
    )
    if decision.action == "allow":
        return None

    _schedule_reply(gateway, event, decision.message)
    logger.info(
        "ACT controlled capture action=%s id=%s",
        decision.action,
        decision.capture_id or "none",
    )
    return {"action": "skip", "reason": f"act_capture:{decision.action}"}


def register(ctx) -> None:
    journal = CaptureJournal(
        Path(get_hermes_home()) / "act-capture" / "journal.jsonl"
    )
    logger.warning("ACT controlled capture plugin active; ACT mount remains read-only")

    def on_pre_gateway_dispatch(
        event: Any = None,
        gateway: Any = None,
        **_: Any,
    ) -> Optional[dict[str, str]]:
        try:
            return _handle_capture(event=event, gateway=gateway, journal=journal)
        except Exception as exc:
            # Fail open for ordinary Hermes chat, but never partially write ACT.
            logger.exception("ACT controlled capture failed: %s", exc)
            return None

    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
