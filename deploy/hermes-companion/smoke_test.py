#!/usr/bin/env python3
"""Smoke tests for the deployed ACT companion core, with optional live reads."""

from __future__ import annotations

import argparse
import stat
import tempfile
from pathlib import Path

from companion_core import (
    CompanionStore,
    FetchResult,
    fetch_aihot,
    fetch_news,
)


HTML = b"""<html><head><title>Smoke Article</title></head><body><article>
<p>This smoke article contains enough plain text to verify deterministic extraction.</p>
<p>It does not touch ACT and it never asks the model to execute article instructions.</p>
<pre>Turn the supplied photo into a quiet editorial poster while preserving the original subject and palette.</pre>
</article></body></html>"""

PROMPT = "Turn the supplied photo into a quiet editorial poster while preserving the original subject and palette."


def fake_fetch(_url: str, **_kwargs):
    return FetchResult("https://example.com/smoke", "text/html", HTML)


def run_local() -> None:
    with tempfile.TemporaryDirectory(prefix="act-companion-smoke-") as temp_dir:
        store = CompanionStore(Path(temp_dir) / "state")
        article = store.cache_article("https://example.com/smoke", fetcher=fake_fetch)
        assert store.stage_article(article["article_id"], "收下这篇")["action"] == "staged"
        assert store.stage_article(article["article_id"], "收下这篇")["action"] == "duplicate"
        assert store.stage_prompt(
            article_id=article["article_id"],
            prompt_name="安静编辑海报",
            retrieval_terms="修照片｜照片做海报",
            suitable_material="主体清晰的照片",
            target_effect="保留主体与配色的安静编辑海报",
            unsuitable="",
            source_author="Smoke",
            prompt_text=PROMPT,
            confirmation_phrase="收下这个提示词",
        )["action"] == "staged"
        assert store.stage_daily(
            entry_type="morning_focus",
            content="verify one controlled path",
            confirmation_phrase="开始今天",
        )["action"] == "staged"
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.journal.stat().st_mode) == 0o600


def run_live() -> None:
    aihot = fetch_aihot(hours=24, take=5)
    news = fetch_news(take=6)
    with tempfile.TemporaryDirectory(prefix="act-companion-live-") as temp_dir:
        store = CompanionStore(Path(temp_dir) / "state")
        article = store.cache_article("https://www.theguardian.com/help/feeds")
        assert article["char_count"] >= 80
    print(
        "LIVE: "
        f"aihot={aihot['count']} news={news['count']} "
        f"article_chars={article['char_count']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    run_local()
    if args.live:
        run_live()
    print("PASS: companion article/prompt/daily staging/auth phrases/idempotence/modes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
