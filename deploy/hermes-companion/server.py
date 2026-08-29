#!/usr/bin/env python3
"""Narrow MCP server for ACT daily briefs, article reading, and external staging."""

from __future__ import annotations

import os
from pathlib import Path

from hermes_constants import get_hermes_home
try:
    from mcp.server import MCPServer as FastMCP
except ImportError:  # Hermes <= 0.20.0 / MCP 1.x
    from mcp.server.fastmcp import FastMCP

from companion_core import (
    CompanionError,
    CompanionStore,
    fetch_aihot,
    fetch_news,
    read_brief_item,
    recent_user_confirmation_token,
)


STORE = CompanionStore(
    Path(get_hermes_home()) / "act-companion",
    channel=os.environ.get("ACT_COMPANION_CHANNEL", "weixin_home"),
)
BRIEF_STATE = Path(get_hermes_home()) / "act-ai-brief"
HERMES_STATE_DB = Path(get_hermes_home()) / "state.db"
MCP = FastMCP("act-companion")
PROMPT_SHORTCUT_MENU_PATTERNS = (
    r"(?m)^\s*(?:[-*]\s*)?1\s*[.、｜|)]\s*收下这个提示词(?:\s|$|[—-])",
    r"(?m)^\s*(?:[-*]\s*)?2\s*[.、｜|)]\s*把完整提示词给我看(?:\s|$|[—-])",
    r"(?m)^\s*(?:[-*]\s*)?3\s*[.、｜|)]\s*不保存(?:\s|$|[—-])",
)


def _failure(exc: CompanionError) -> dict[str, object]:
    return {"ok": False, "error": str(exc)}


@MCP.tool()
def companion_status() -> dict[str, object]:
    """Show the external-staging boundary and record counts without returning user content."""
    return {"ok": True, **STORE.status()}


@MCP.tool()
def companion_aihot(hours: int = 24, take: int = 10) -> dict[str, object]:
    """Read AIHOT selected AI news for the last 1-168 hours. Public read only; every item includes a source URL."""
    try:
        return {"ok": True, **fetch_aihot(hours=hours, take=take)}
    except CompanionError as exc:
        return _failure(exc)


@MCP.tool()
def companion_news(take: int = 9) -> dict[str, object]:
    """Read recent headlines from the fixed BBC Chinese, UN Chinese, and Guardian World RSS allowlist."""
    try:
        return {"ok": True, **fetch_news(take=take)}
    except CompanionError as exc:
        return _failure(exc)


@MCP.tool()
def companion_brief_item(number: int) -> dict[str, object]:
    """Read one numbered item from the latest fully delivered brief within 48 hours. Accepts no path or URL and never writes ACT."""
    try:
        return {"ok": True, **read_brief_item(BRIEF_STATE, number)}
    except CompanionError as exc:
        return _failure(exc)


@MCP.tool()
def companion_article_extract(url: str) -> dict[str, object]:
    """Safely extract one public HTTP(S) article. Blocks private hosts, auth, nonstandard ports, oversized content, and non-text files."""
    try:
        return {"ok": True, **STORE.cache_article(url)}
    except CompanionError as exc:
        return _failure(exc)


@MCP.tool()
def companion_article_stage(article_id: str, confirmation_phrase: str) -> dict[str, object]:
    """Stage the last extracted article outside ACT only after the user says exactly '收下这篇'. Never writes the Vault."""
    try:
        token = recent_user_confirmation_token(
            HERMES_STATE_DB,
            channel=STORE.channel,
            exact_phrase="收下这篇",
        )
        STORE.claim_confirmation(token, "article")
        return {"ok": True, **STORE.stage_article(article_id, confirmation_phrase)}
    except CompanionError as exc:
        return _failure(exc)


@MCP.tool()
def companion_prompt_stage(
    article_id: str,
    prompt_name: str,
    retrieval_terms: str,
    suitable_material: str,
    target_effect: str,
    unsuitable: str,
    source_author: str,
    prompt_text: str,
    confirmation_phrase: str,
) -> dict[str, object]:
    """Stage one verbatim prompt after the exact phrase or a verified adjacent prompt-menu choice 1."""
    try:
        # Validate source fidelity and obvious truncation before consuming the
        # user's one-time confirmation, so a corrected tool call can retry.
        STORE.preflight_prompt_text(article_id, prompt_text)
        confirmation = confirmation_phrase.strip()
        if confirmation == "收下这个提示词":
            menu_patterns: tuple[str, ...] = ()
        elif confirmation == "1":
            menu_patterns = PROMPT_SHORTCUT_MENU_PATTERNS
        else:
            raise CompanionError(
                "只接受用户原样发送“收下这个提示词”，"
                "或在刚显示的提示词菜单后回复单个数字“1”。"
            )
        token = recent_user_confirmation_token(
            HERMES_STATE_DB,
            channel=STORE.channel,
            exact_phrase=confirmation,
            required_previous_assistant_patterns=menu_patterns,
        )
        STORE.claim_confirmation(token, "prompt")
        return {
            "ok": True,
            **STORE.stage_prompt(
                article_id=article_id,
                prompt_name=prompt_name,
                retrieval_terms=retrieval_terms,
                suitable_material=suitable_material,
                target_effect=target_effect,
                unsuitable=unsuitable,
                source_author=source_author,
                prompt_text=prompt_text,
                confirmation_phrase="收下这个提示词",
            ),
        }
    except CompanionError as exc:
        return _failure(exc)


@MCP.tool()
def companion_daily_stage(
    entry_type: str,
    content: str,
    confirmation_phrase: str,
) -> dict[str, object]:
    """Stage outside ACT. Morning focus requires '开始今天' or '今日重点'; daily wrap requires '确认收尾'. Never writes the Vault."""
    try:
        return {
            "ok": True,
            **STORE.stage_daily(
                entry_type=entry_type,
                content=content,
                confirmation_phrase=confirmation_phrase,
            ),
        }
    except CompanionError as exc:
        return _failure(exc)


if __name__ == "__main__":
    MCP.run(transport="stdio")
