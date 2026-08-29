#!/usr/bin/env python3
"""Deterministic morning/evening AI brief runner for Hermes.

AIHOT is treated as untrusted, read-only input. The runner never opens ACT,
never executes text from news items, and stores delivery state only under the
Hermes data directory. State advances only after a successful platform send.
"""

from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo


DEPLOY_DIR = Path(__file__).resolve().parent
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

from act_brief_package import (  # noqa: E402
    PackageError,
    acquire_official_image,
    apply_official_image_fallback,
    atomic_write_json,
    build_package,
    deliver_resumably,
    find_oldest_pending_manifest,
    load_brief_copy,
    render_package,
    render_via_queue,
)


SCHEMA_VERSION = 1
AIHOT_HOST = "aihot.virxact.com"
AIHOT_PATH = "/api/public/items"
AIHOT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
    "Safari/537.36 aihot-skill/0.2.0"
)
BEIJING = ZoneInfo("Asia/Shanghai")
MORNING_TIME = clock_time(7, 0)
EVENING_TIME = clock_time(19, 0)
OVERLAP = timedelta(hours=2)
LEDGER_RETENTION = timedelta(days=14)
MAX_API_BYTES = 2_000_000
MAX_PAGES = {"selected": 1, "all": 2}
WEIXIN_CHUNK_DELAY_SECONDS = "35"
SEND_TIMEOUT_SECONDS = 90
MESSAGE_CHAR_BUDGET = 1_900
DELIVERY_TARGETS = {"weixin": "微信", "telegram": "Telegram"}
TRACKING_KEYS = {
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "mkt_tok",
    "spm", "yclid", "_hsenc", "_hsmi",
}
UPDATE_MARKERS = (
    "更新", "进展", "后续", "现已", "正式", "确认", "回应", "修复",
    "版本", "开源", "降价", "上线", "发布", "update", "updated",
    "confirmed", "released", "launched", "available", "fix",
)
IMPORTANT_MARKERS = (
    "发布", "推出", "上线", "开源", "融资", "收购", "监管", "政策",
    "诉讼", "合作", "漏洞", "安全", "降价", "涨价", "release",
    "launch", "open source", "funding", "acquire", "regulation",
)
LOW_VALUE_MARKERS = (
    "教程", "技巧", "提示词", "指南", "如何", "清单", "合集",
    "tutorial", "prompt", "tips", "guide", "how to",
)
OFFICIAL_SOURCE_MARKERS = (
    "openai", "anthropic", "google", "deepmind", "meta", "microsoft",
    "nvidia", "hugging face", "github", "apple", "amazon", "aws",
    "alibaba", "qwen", "deepseek", "zhipu", "智谱", "moonshot",
    "月之暗面", "bytedance", "字节", "tencent", "腾讯", "mistral", "xai",
)
GENERIC_ENTITY_TOKENS = {
    "about", "agent", "agents", "artificial", "available", "company",
    "launch", "model", "models", "news", "official", "open", "release",
    "released", "source", "update", "updated",
}
CATEGORY_LABELS = {
    "ai-models": "模型",
    "ai-products": "产品",
    "industry": "行业",
    "paper": "研究",
    "tip": "实践",
}
CATEGORY_SCORES = {
    "ai-models": 45,
    "ai-products": 36,
    "industry": 33,
    "paper": 29,
    "tip": 8,
}


class BriefError(RuntimeError):
    """A fail-closed error safe to record without response bodies or secrets."""


@dataclass(frozen=True)
class NewsItem:
    item_id: str
    title: str
    source: str
    published_at: datetime
    summary: str
    category: str
    url: str
    canonical_url: str
    selected: bool = False
    score: int = 0
    is_update: bool = False


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    fetch_start: datetime
    already_complete: bool = False
    recovery_truncated: bool = False


@dataclass(frozen=True)
class RunResult:
    slot: str
    message: str
    sent_count: int
    skipped: bool = False
    dry_run: bool = False
    package: Optional[dict[str, Any]] = None


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _single_line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def canonicalize_url(value: str) -> Optional[str]:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").lower() if parsed.hostname else ""
    except (ValueError, AttributeError, UnicodeError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    if parsed.username or parsed.password:
        return None
    if port not in {None, 80, 443}:
        return None
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    kept_query: list[tuple[str, str]] = []
    for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in TRACKING_KEYS:
            continue
        kept_query.append((key, val))
    query = urllib.parse.urlencode(sorted(kept_query))
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def _title_norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\b(the|a|an|ai|artificial intelligence)\b", " ", value)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def _summary_norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", value).strip()[:500]


def _bigrams(value: str) -> set[str]:
    if len(value) < 2:
        return {value} if value else set()
    return {value[index:index + 2] for index in range(len(value) - 1)}


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    sequence = difflib.SequenceMatcher(None, left, right).ratio()
    left_grams = _bigrams(left)
    right_grams = _bigrams(right)
    union = left_grams | right_grams
    jaccard = len(left_grams & right_grams) / len(union) if union else 0.0
    return max(sequence, jaccard)


def _entity_tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"[a-z][a-z0-9._+-]{2,}", value.casefold()))
    return {token for token in tokens if token not in GENERIC_ENTITY_TOKENS}


def _story_similarity(
    left_title: str,
    left_summary: str,
    right_title: str,
    right_summary: str,
) -> float:
    title_similarity = _similarity(_title_norm(left_title), _title_norm(right_title))
    shared_entities = _entity_tokens(left_title) & _entity_tokens(right_title)
    entity_similarity = 0.90 if len(shared_entities) >= 2 else 0.0
    summary_similarity = _similarity(_summary_norm(left_summary), _summary_norm(right_summary))
    return max(title_similarity, entity_similarity, summary_similarity)


def compute_window(slot: str, now: datetime, state: dict[str, Any]) -> Window:
    if slot not in {"morning", "evening"}:
        raise BriefError("未知简报时段。")
    if now.tzinfo is None:
        raise BriefError("运行时间必须包含时区。")
    local_now = now.astimezone(BEIJING)
    target_time = MORNING_TIME if slot == "morning" else EVENING_TIME
    end_local = datetime.combine(local_now.date(), target_time, tzinfo=BEIJING)
    if local_now < end_local:
        end_local -= timedelta(days=1)
    if slot == "morning":
        default_start_local = datetime.combine(
            end_local.date() - timedelta(days=1), EVENING_TIME, tzinfo=BEIJING
        )
    else:
        default_start_local = datetime.combine(
            end_local.date(), MORNING_TIME, tzinfo=BEIJING
        )
    end = end_local.astimezone(timezone.utc)
    start = default_start_local.astimezone(timezone.utc)
    last_success = _parse_datetime(state.get("last_success_end"))
    if last_success is not None and last_success >= end:
        return Window(start=end, end=end, fetch_start=end, already_complete=True)
    if last_success is not None and last_success < start:
        start = last_success
    recovery_truncated = False
    oldest_allowed = end - timedelta(days=7)
    if start < oldest_allowed:
        start = oldest_allowed
        recovery_truncated = True
    fetch_start = max(start - OVERLAP, oldest_allowed)
    return Window(
        start=start,
        end=end,
        fetch_start=fetch_start,
        recovery_truncated=recovery_truncated,
    )


def latest_due_slot(now: datetime) -> str:
    """Return the most recent Beijing 07:00/19:00 delivery boundary."""
    if now.tzinfo is None:
        raise BriefError("运行时间必须包含时区。")
    local_time = now.astimezone(BEIJING).time()
    return "morning" if MORNING_TIME <= local_time < EVENING_TIME else "evening"


def _api_page(mode: str, since: datetime, cursor: Optional[str] = None) -> dict[str, Any]:
    query: dict[str, str] = {
        "mode": mode,
        "since": _iso_utc(since),
        "take": "100",
    }
    if cursor:
        query["cursor"] = cursor
    path = f"{AIHOT_PATH}?{urllib.parse.urlencode(query)}"
    connection = http.client.HTTPSConnection(
        AIHOT_HOST,
        port=443,
        timeout=25,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Host": AIHOT_HOST,
                "User-Agent": AIHOT_USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_API_BYTES + 1)
        if response.status != 200:
            raise BriefError(f"AIHOT {mode} 候选读取失败（HTTP {response.status}）。")
        if len(body) > MAX_API_BYTES:
            raise BriefError(f"AIHOT {mode} 返回超过大小限制。")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BriefError(f"AIHOT {mode} 返回无效数据。") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise BriefError(f"AIHOT {mode} 返回结构不受支持。")
        return payload
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        raise BriefError(f"AIHOT {mode} 网络读取失败。") from exc
    finally:
        connection.close()


def _clean_item(raw: Any, *, selected: bool) -> Optional[NewsItem]:
    if not isinstance(raw, dict):
        return None
    title = _single_line(raw.get("title"), 220)
    source = _single_line(raw.get("source"), 100)
    summary = _single_line(raw.get("summary"), 360)
    category = _single_line(raw.get("category"), 40)
    item_id = _single_line(raw.get("id"), 160)
    url = _single_line(raw.get("url"), 2_000)
    canonical_url = canonicalize_url(url)
    published_at = _parse_datetime(raw.get("publishedAt"))
    if not title or not source or not canonical_url or published_at is None:
        return None
    return NewsItem(
        item_id=item_id or hashlib.sha256(canonical_url.encode()).hexdigest()[:24],
        title=title,
        source=source,
        published_at=published_at,
        summary=summary,
        category=category if category in CATEGORY_LABELS else "",
        url=url,
        canonical_url=canonical_url,
        selected=selected,
    )


def _fetch_pool(mode: str, fetch_start: datetime, end: datetime) -> tuple[list[NewsItem], bool]:
    items: list[NewsItem] = []
    cursor: Optional[str] = None
    partial = False
    for page_number in range(MAX_PAGES[mode]):
        try:
            payload = _api_page(mode, fetch_start, cursor)
        except BriefError:
            if page_number == 0:
                raise
            partial = True
            break
        for raw in payload["items"]:
            item = _clean_item(raw, selected=mode == "selected")
            if item and fetch_start <= item.published_at <= end:
                items.append(item)
        cursor_value = payload.get("nextCursor")
        if not payload.get("hasNext") or not isinstance(cursor_value, str) or not cursor_value:
            break
        cursor = cursor_value
        time.sleep(0.2)
    return items, partial


def _importance_score(item: NewsItem, end: datetime) -> int:
    score = CATEGORY_SCORES.get(item.category, 20)
    if item.selected:
        score += 80
    searchable = f"{item.title} {item.summary}".casefold()
    source = item.source.casefold()
    score += min(24, sum(8 for marker in IMPORTANT_MARKERS if marker in searchable))
    if any(marker in source for marker in OFFICIAL_SOURCE_MARKERS):
        score += 12
    if any(marker in searchable for marker in LOW_VALUE_MARKERS):
        score -= 20
    age_hours = max(0.0, (end - item.published_at).total_seconds() / 3600)
    score += max(0, 10 - min(10, int(age_hours)))
    return score


def gather_candidates(fetch_start: datetime, end: datetime) -> tuple[list[NewsItem], dict[str, Any]]:
    merged: dict[str, NewsItem] = {}
    available: list[str] = []
    failures: list[str] = []
    partial = False
    for mode in ("selected", "all"):
        try:
            pool, pool_partial = _fetch_pool(mode, fetch_start, end)
        except BriefError:
            failures.append(mode)
            continue
        available.append(mode)
        partial = partial or pool_partial
        for item in pool:
            key = item.item_id or item.canonical_url
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
            elif item.selected and not existing.selected:
                merged[key] = replace(existing, selected=True)
    if not available:
        raise BriefError("AIHOT 精选与全量候选当前均不可用，未推进发送水位线。")
    scored = [replace(item, score=_importance_score(item, end)) for item in merged.values()]
    return scored, {
        "available": available,
        "failures": failures,
        "partial": partial,
        "candidate_count": len(scored),
    }


def _history_since(state: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    cutoff = now.astimezone(timezone.utc) - LEDGER_RETENTION
    kept: list[dict[str, Any]] = []
    for record in state.get("sent", []):
        if not isinstance(record, dict):
            continue
        sent_at = _parse_datetime(record.get("sent_at"))
        if sent_at is not None and sent_at >= cutoff:
            kept.append(record)
    return kept


def _material_update(item: NewsItem, record: dict[str, Any], title_similarity: float) -> bool:
    if title_similarity >= 0.95:
        return False
    previous_published = _parse_datetime(record.get("published_at"))
    if previous_published is None or item.published_at < previous_published + timedelta(hours=2):
        return False
    searchable = f"{item.title} {item.summary}".casefold()
    if not any(marker in searchable for marker in UPDATE_MARKERS):
        return False
    old_summary = _summary_norm(str(record.get("summary", "")))
    new_summary = _summary_norm(item.summary)
    return _similarity(old_summary, new_summary) < 0.65


def classify_history(item: NewsItem, history: Iterable[dict[str, Any]]) -> str:
    for record in history:
        if item.item_id and item.item_id == record.get("item_id"):
            return "duplicate"
        if item.canonical_url and item.canonical_url == record.get("canonical_url"):
            return "duplicate"
        old_title = str(record.get("title", ""))
        if not old_title:
            old_title = str(record.get("title_norm", ""))
        similarity = _story_similarity(
            item.title,
            item.summary,
            old_title,
            str(record.get("summary", "")),
        )
        if similarity >= 0.82:
            return "update" if _material_update(item, record, similarity) else "duplicate"
    return "new"


def _dedupe_current(items: Iterable[NewsItem]) -> list[NewsItem]:
    kept: list[NewsItem] = []
    for item in sorted(items, key=lambda value: (value.score, value.published_at), reverse=True):
        duplicate = False
        for existing in kept:
            if item.item_id == existing.item_id or item.canonical_url == existing.canonical_url:
                duplicate = True
                break
            if _story_similarity(
                item.title,
                item.summary,
                existing.title,
                existing.summary,
            ) >= 0.84:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return kept


def select_items(
    slot: str,
    candidates: Iterable[NewsItem],
    history: Iterable[dict[str, Any]],
) -> dict[str, list[NewsItem]]:
    total_limit = 8 if slot == "morning" else 12
    must_limit = 4 if slot == "morning" else 5
    tip_limit = 1 if slot == "morning" else 2
    fresh: list[NewsItem] = []
    updates: list[NewsItem] = []
    for item in _dedupe_current(candidates):
        classification = classify_history(item, history)
        if classification == "duplicate":
            continue
        if not item.selected and item.score < 25:
            continue
        if classification == "update":
            updates.append(replace(item, is_update=True))
        else:
            fresh.append(item)
    updates = sorted(updates, key=lambda value: (value.score, value.published_at), reverse=True)[:2]
    remaining = total_limit - len(updates)
    selected: list[NewsItem] = []
    category_counts: dict[str, int] = {}
    for item in sorted(fresh, key=lambda value: (value.score, value.published_at), reverse=True):
        if len(selected) >= remaining:
            break
        if item.category == "tip" and category_counts.get("tip", 0) >= tip_limit:
            continue
        selected.append(item)
        category_counts[item.category] = category_counts.get(item.category, 0) + 1
    return {
        "must": selected[:must_limit],
        "glance": selected[must_limit:],
        "updates": updates,
    }


def _why_it_matters(item: NewsItem) -> str:
    searchable = f"{item.title} {item.summary}".casefold()
    if any(marker in searchable for marker in ("agent", "智能体", "多代理", "multi-agent")):
        return "与你正在搭建的多 Agent 工作方式直接相关，值得观察能力和协作边界。"
    if any(marker in searchable for marker in ("开源", "open source", "权重")):
        return "开源通常会降低试用门槛，并影响后续模型与工具选型。"
    if any(marker in searchable for marker in ("价格", "降价", "涨价", "定价", "成本", "pricing")):
        return "它可能直接改变模型或产品的使用成本。"
    if any(marker in searchable for marker in ("隐私", "广告", "数据使用", "privacy")):
        return "它可能影响平台的数据使用方式、体验与用户选择边界。"
    if any(marker in searchable for marker in ("融资", "收购", "估值", "funding", "acquire")):
        return "它反映 AI 创业、资本与产业资源正在流向哪里。"
    if any(marker in searchable for marker in ("监管", "政策", "法案", "诉讼", "regulation", "lawsuit")):
        return "它可能改变 AI 产品的可用范围与合规边界。"
    if item.category == "ai-models":
        return "模型能力、价格或生态变化可能影响你的技术选型。"
    if item.category == "ai-products":
        return "新产品能力可能改变现有 AI 工作流的做法。"
    if item.category == "paper":
        return "这项研究可能影响后续能力方向，但应用价值仍需观察。"
    if item.category == "industry":
        return "这条动态反映 AI 行业竞争或商业格局的变化。"
    return "可作为近期 AI 实践信号，优先级低于模型、产品和行业事件。"


def _format_window(start: datetime, end: datetime) -> str:
    local_start = start.astimezone(BEIJING)
    local_end = end.astimezone(BEIJING)
    if local_start.date() == local_end.date():
        return f"{local_start:%-m月%-d日 %H:%M}—{local_end:%H:%M}"
    return f"{local_start:%-m月%-d日 %H:%M}—{local_end:%-m月%-d日 %H:%M}"


def _item_lines(item: NewsItem, number: int) -> list[str]:
    category = CATEGORY_LABELS.get(item.category, "动态")
    published = item.published_at.astimezone(BEIJING).strftime("%-m月%-d日 %H:%M")
    lines = [
        f"{number}. {item.title}",
        f"   {category} · {item.source} · {published}",
        f"   影响：{_why_it_matters(item)}",
    ]
    lines.append(f"   {item.url}")
    return lines


def build_message(
    slot: str,
    window: Window,
    groups: dict[str, list[NewsItem]],
    coverage: dict[str, Any],
) -> str:
    copy = load_brief_copy()
    label = "昨夜 AI 大事" if slot == "morning" else "今日 AI 总结"
    lines = [
        f"AI 简报｜{label}",
        _format_window(window.start, window.end),
        "已与近 14 天发送记录去重；同一事件仅保留新增进展。",
    ]
    number = 1
    section_labels = (
        ("must", "必须知道"),
        ("glance", "值得扫一眼"),
        ("updates", "进展更新"),
    )
    total = sum(len(groups[key]) for key, _ in section_labels)
    if total == 0:
        lines.extend(["", "本时间窗没有达到简报门槛的重要更新，不用为了数量硬看新闻。"])
    else:
        for key, section_label in section_labels:
            if not groups[key]:
                continue
            lines.extend(["", f"【{section_label}】"])
            for item in groups[key]:
                lines.extend(_item_lines(item, number))
                number += 1
    if coverage.get("failures") or coverage.get("partial"):
        lines.extend(["", "注：本期候选读取不完整，已只使用成功返回的数据；下次仍会补查未覆盖时间窗。"])
    if window.recovery_truncated:
        lines.extend(["", "注：距离上次成功发送已超过 7 天，本次按 AIHOT 可查询的最近 7 天恢复。"])
    lines.extend(
        [
            "",
            copy["source_note"],
            "想继续看新闻：复制某条链接并说“整理这篇 <链接>”。",
            "",
        ]
    )
    if slot == "morning":
        lines.extend([copy["full_morning_guide"]])
    else:
        lines.extend([copy["full_evening_guide"]])
    return "\n".join(lines).strip() + "\n"


def fit_message_budget(
    slot: str,
    window: Window,
    groups: dict[str, list[NewsItem]],
    coverage: dict[str, Any],
) -> tuple[dict[str, list[NewsItem]], str]:
    """Fit a timed brief into one messaging payload without hiding chosen items.

    The candidate pool and ranking stay unchanged. We drop the lowest-priority
    displayed item until the complete message, including guidance and URLs,
    fits the conservative single-bubble budget. The returned groups are also
    used for the delivery ledger, so only items actually shown are deduped.
    """
    fitted = {key: list(groups.get(key, [])) for key in ("must", "glance", "updates")}
    while True:
        message = build_message(slot, window, fitted, coverage)
        if len(message) <= MESSAGE_CHAR_BUDGET:
            return fitted, message
        for key in ("glance", "updates", "must"):
            if fitted[key]:
                fitted[key].pop()
                break
        else:
            raise BriefError("简报固定提示超过单消息预算，已停止发送。")


def _ensure_state_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "sent": []}
    if path.is_symlink() or not path.is_file():
        raise BriefError("简报状态文件不是普通文件。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BriefError("简报状态无法读取；为避免重复，已停止发送。") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise BriefError("简报状态版本不受支持；为避免重复，已停止发送。")
    if not isinstance(payload.get("sent", []), list):
        raise BriefError("简报发送记录结构无效。")
    return payload


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    _ensure_state_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix="state-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _sent_record(item: NewsItem, sent_at: datetime) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "canonical_url": item.canonical_url,
        "title": item.title,
        "title_norm": _title_norm(item.title),
        "summary": item.summary[:360],
        "published_at": _iso_utc(item.published_at),
        "sent_at": _iso_utc(sent_at),
        "was_update": item.is_update,
    }


def _delivery_target() -> str:
    target = os.environ.get("ACT_BRIEF_TARGET", "weixin").strip().lower()
    if target not in DELIVERY_TARGETS:
        raise BriefError("ACT_BRIEF_TARGET 只允许 weixin 或 telegram。")
    return target


def send_brief(message: str, state_dir: Path) -> None:
    hermes = shutil.which("hermes")
    if not hermes:
        raise BriefError("找不到 Hermes CLI，未发送简报。")
    _ensure_state_dir(state_dir)
    descriptor, temporary = tempfile.mkstemp(prefix="brief-", suffix=".txt", dir=state_dir)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(message)
            handle.flush()
            os.fsync(handle.fileno())
        send_env = os.environ.copy()
        target = _delivery_target()
        if target == "weixin":
            # iLink rate-limits rapid long-message chunks. Keep this override
            # local so ordinary interactive replies are unchanged.
            send_env["WEIXIN_SEND_CHUNK_DELAY_SECONDS"] = WEIXIN_CHUNK_DELAY_SECONDS
        result = subprocess.run(
            [hermes, "send", "--to", target, "--file", temporary, "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=SEND_TIMEOUT_SECONDS,
            check=False,
            env=send_env,
        )
        if result.returncode != 0:
            detail = _single_line(result.stdout or result.stderr or "", 240)
            suffix = f" Hermes: {detail}" if detail else ""
            raise BriefError(
                f"{DELIVERY_TARGETS[target]}简报发送失败，发送水位线未推进。{suffix}"
            )
    except subprocess.TimeoutExpired as exc:
        target = _delivery_target()
        raise BriefError(
            f"{DELIVERY_TARGETS[target]}简报发送超时，发送水位线未推进。"
        ) from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def send_brief_image(path: Path) -> None:
    hermes = shutil.which("hermes")
    if not hermes:
        raise BriefError("找不到 Hermes CLI，未发送简报图片。")
    target = _delivery_target()
    result = subprocess.run(
        [hermes, "send", "--to", target, f"MEDIA:{path}", "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=SEND_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        detail = _single_line(result.stdout or result.stderr or "", 240)
        raise BriefError(
            f"{DELIVERY_TARGETS[target]}简报图片发送失败。"
            f"{(' Hermes: ' + detail) if detail else ''}"
        )


def _package_item(item: NewsItem) -> dict[str, Any]:
    return {
        "title": item.title,
        "source": item.source,
        "published_at": _iso_utc(item.published_at),
        "summary": item.summary,
        "category": item.category,
        "url": item.url,
        "canonical_url": item.canonical_url,
        "impact": _why_it_matters(item),
        "is_update": item.is_update,
    }


def _news_from_package(package: dict[str, Any]) -> list[NewsItem]:
    items: list[NewsItem] = []
    for raw in package.get("items", []):
        published = _parse_datetime(raw.get("published_at"))
        canonical = canonicalize_url(str(raw.get("canonical_url", "")))
        if published is None or canonical is None:
            raise BriefError("待续传简报包含无效条目，已停止推进水位线。")
        items.append(NewsItem(
            item_id=hashlib.sha256(canonical.encode()).hexdigest()[:24],
            title=_single_line(raw.get("title"), 220),
            source=_single_line(raw.get("source"), 100),
            published_at=published,
            summary=_single_line(raw.get("summary"), 360),
            category=_single_line(raw.get("category"), 40),
            url=_single_line(raw.get("url"), 2_000),
            canonical_url=canonical,
            is_update=bool(raw.get("is_update", False)),
        ))
    return items


def _render_visual_package(package: dict[str, Any], state_dir: Path) -> list[Path]:
    brief_dir = state_dir / "delivery-manifests" / package["brief_id"]
    assets_dir = brief_dir / "assets"
    try:
        acquire_official_image(package, assets_dir)
    except PackageError as exc:
        package["official_image"] = None
        package["official_image_error"] = _single_line(str(exc), 240)
    apply_official_image_fallback(package)
    package_path = brief_dir / "package.json"
    atomic_write_json(package_path, package)
    if not package.get("cards"):
        package["render"] = {"status": "not_needed", "files": []}
        atomic_write_json(package_path, package)
        return []
    try:
        if os.environ.get("ACT_BRIEF_RENDER_MODE") == "queue":
            files = render_via_queue(package, state_dir)
        else:
            renderer = DEPLOY_DIR / "render_guizang_brief.mjs"
            template = DEPLOY_DIR / "third_party/guizang-social-card-skill/template-swiss-card.html"
            node = os.environ.get("ACT_BRIEF_NODE", "node")
            render_env = os.environ.copy()
            if os.environ.get("ACT_BRIEF_NODE_PATH"):
                render_env["NODE_PATH"] = os.environ["ACT_BRIEF_NODE_PATH"]
            files = render_package(
                package_path,
                brief_dir / "render",
                renderer=renderer,
                template=template,
                node=node,
                env=render_env,
            )
    except (PackageError, OSError) as exc:
        package["render"] = {"status": "failed", "files": [], "error": _single_line(str(exc), 300)}
        atomic_write_json(package_path, package)
        return []
    package["render"] = {"status": "ok", "files": [str(path) for path in files]}
    atomic_write_json(package_path, package)
    return files


def _append_error(state_dir: Path, slot: str, exc: Exception) -> None:
    try:
        _ensure_state_dir(state_dir)
        path = state_dir / "errors.jsonl"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        os.chmod(path, 0o600)
        payload = {
            "at": _iso_utc(datetime.now(timezone.utc)),
            "slot": slot,
            "error": _single_line(str(exc), 300),
        }
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


CandidateProvider = Callable[[datetime, datetime], tuple[list[NewsItem], dict[str, Any]]]
Sender = Callable[[str, Path], None]


def run_brief(
    slot: str,
    *,
    now: Optional[datetime] = None,
    state_dir: Optional[Path] = None,
    dry_run: bool = False,
    candidate_provider: CandidateProvider = gather_candidates,
    sender: Sender = send_brief,
    package_output: Optional[Path] = None,
    shadow_output: Optional[Path] = None,
    visual_delivery: Optional[bool] = None,
    retry_pending_only: bool = False,
) -> RunResult:
    now = now or datetime.now(timezone.utc)
    state_dir = state_dir or Path(os.environ.get("HERMES_HOME", "/opt/data")) / "act-ai-brief"
    _ensure_state_dir(state_dir)
    lock_path = state_dir / "run.lock"
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    os.chmod(lock_path, 0o600)
    with os.fdopen(lock_descriptor, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state_path = state_dir / "state.json"
        state = _load_state(state_path)
        visual_delivery = (
            os.environ.get("ACT_BRIEF_VISUAL_DELIVERY") == "1"
            if visual_delivery is None else visual_delivery
        )
        pending = None if dry_run or not visual_delivery else find_oldest_pending_manifest(state_dir)
        if pending is not None:
            package = pending["package"]
            media = [Path(part["file"]) for part in pending["parts"] if part.get("kind") == "image"]
            fallback = next(part["text"] for part in pending["parts"] if part.get("kind") == "text")
            deliver_resumably(
                package=package,
                state_dir=state_dir,
                media_files=media,
                fallback_text=fallback,
                image_sender=send_brief_image,
                text_sender=lambda text: sender(text, state_dir),
            )
            chosen = _news_from_package(package)
            sent_at = datetime.now(timezone.utc)
            pending_end = _parse_datetime(package.get("window", {}).get("end"))
            if pending_end is None:
                raise BriefError("待续传简报时间窗无效，水位线未推进。")
            prior_end = _parse_datetime(state.get("last_success_end"))
            success_end = max(value for value in (pending_end, prior_end) if value is not None)
            updated_state = {
                "schema_version": SCHEMA_VERSION,
                "last_success_end": _iso_utc(success_end),
                "last_success_at": _iso_utc(sent_at),
                "last_slot": package.get("slot", slot),
                "last_count": len(chosen),
                "sent": _history_since(state, now) + [_sent_record(item, sent_at) for item in chosen],
            }
            _write_state(state_path, updated_state)
            return RunResult(slot=package.get("slot", slot), message=fallback, sent_count=len(chosen), package=package)
        if retry_pending_only:
            return RunResult(slot=slot, message="", sent_count=0, skipped=True)
        window = compute_window(slot, now, {} if dry_run else state)
        if window.already_complete:
            return RunResult(slot=slot, message="", sent_count=0, skipped=True)
        candidates, coverage = candidate_provider(window.fetch_start, window.end)
        history = _history_since(state, now)
        groups = select_items(slot, candidates, history)
        structured_items = groups["must"] + groups["glance"] + groups["updates"]
        package = build_package(
            slot=slot,
            start=window.start,
            end=window.end,
            items=[_package_item(item) for item in structured_items],
            coverage=coverage,
            generated_at=now,
        )
        groups, message = fit_message_budget(slot, window, groups, coverage)
        chosen = groups["must"] + groups["glance"] + groups["updates"]
        if package_output:
            atomic_write_json(package_output, package)
        if shadow_output:
            shadow_output.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                acquire_official_image(package, shadow_output / "assets")
            except PackageError as exc:
                package["official_image"] = None
                package["official_image_error"] = _single_line(str(exc), 240)
            apply_official_image_fallback(package)
            preview_path = shadow_output / "package.json"
            atomic_write_json(preview_path, package)
            if package["cards"]:
                files = render_package(
                    preview_path,
                    shadow_output / "output",
                    renderer=DEPLOY_DIR / "render_guizang_brief.mjs",
                    template=DEPLOY_DIR / "third_party/guizang-social-card-skill/template-swiss-card.html",
                    node=os.environ.get("ACT_BRIEF_NODE", "node"),
                    env={**os.environ, "NODE_PATH": os.environ.get("ACT_BRIEF_NODE_PATH", os.environ.get("NODE_PATH", ""))},
                )
                package["render"] = {"status": "ok", "files": [str(path) for path in files]}
                atomic_write_json(preview_path, package)
        if dry_run:
            return RunResult(
                slot=slot,
                message=message,
                sent_count=len(chosen),
                dry_run=True,
                package=package,
            )
        if visual_delivery:
            media = _render_visual_package(package, state_dir)
            fallback_delivery_text = (
                package["guide_text"]
                if package.get("render", {}).get("status") == "not_needed"
                else message
            )
            deliver_resumably(
                package=package,
                state_dir=state_dir,
                media_files=media,
                fallback_text=fallback_delivery_text,
                image_sender=send_brief_image,
                text_sender=lambda text: sender(text, state_dir),
            )
            chosen = structured_items
        else:
            sender(message, state_dir)
        sent_at = datetime.now(timezone.utc)
        updated_history = history + [_sent_record(item, sent_at) for item in chosen]
        updated_state = {
            "schema_version": SCHEMA_VERSION,
            "last_success_end": _iso_utc(window.end),
            "last_success_at": _iso_utc(sent_at),
            "last_slot": slot,
            "last_count": len(chosen),
            "sent": updated_history,
        }
        _write_state(state_path, updated_state)
        return RunResult(slot=slot, message=message, sent_count=len(chosen), package=package)


def _parse_cli_time(value: str) -> datetime:
    try:
        local = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--at 需要 ISO 时间。") from exc
    if local.tzinfo is None:
        local = local.replace(tzinfo=BEIJING)
    return local.astimezone(timezone.utc)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", choices=("morning", "evening"), required=True)
    parser.add_argument("--dry-run", action="store_true", help="生成预览，不发送且不更新状态")
    parser.add_argument("--at", type=_parse_cli_time, help="仅用于预演/测试的运行时间")
    parser.add_argument("--state-dir", type=Path, help="覆盖 Hermes 外部状态目录")
    parser.add_argument("--package-json", type=Path, help="额外写出稳定 package JSON")
    parser.add_argument("--shadow-render", type=Path, help="渲染样图但不发送、不更新状态")
    parser.add_argument("--retry-pending", action="store_true", help="只续传最早的未完成简报；无欠账时直接退出")
    args = parser.parse_args(argv)
    state_dir = args.state_dir or Path(os.environ.get("HERMES_HOME", "/opt/data")) / "act-ai-brief"
    try:
        result = run_brief(
            args.slot,
            now=args.at,
            state_dir=state_dir,
            dry_run=args.dry_run or args.shadow_render is not None,
            package_output=args.package_json,
            shadow_output=args.shadow_render,
            retry_pending_only=args.retry_pending,
        )
    except Exception as exc:
        _append_error(state_dir, args.slot, exc)
        print(f"AI brief failed: {_single_line(str(exc), 300)}", file=sys.stderr)
        return 1
    if args.dry_run or args.shadow_render:
        sys.stdout.write(result.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
