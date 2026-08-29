#!/usr/bin/env python3
"""Restrict Weixin skill discovery to the audited ACT companion skill pack."""

from __future__ import annotations

import argparse

from hermes_cli.config import load_config, save_config
from tools.skills_tool import _find_all_skills


KEEP = {
    "act-daily-companion",
    "act-article-intake",
    "aihot",
    "act-news-brief",
    "act-source-verification",
    "act-context-query",
    "act-usage-guide",
    "act-shark-companion",
    "grounded-citations",
}


def desired_state() -> tuple[dict, list[str], set[str]]:
    config = load_config()
    discovered = {
        str(item.get("name") or "").strip()
        for item in _find_all_skills(skip_disabled=True)
        if str(item.get("name") or "").strip()
    }
    missing = KEEP - discovered
    globally_disabled = set((config.get("skills") or {}).get("disabled") or [])
    blocked_keep = KEEP.intersection(globally_disabled)
    if missing:
        raise RuntimeError(f"missing required skills: {', '.join(sorted(missing))}")
    if blocked_keep:
        raise RuntimeError(
            "required skills are globally disabled: " + ", ".join(sorted(blocked_keep))
        )
    return config, sorted(discovered - KEEP), discovered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    config, disabled, discovered = desired_state()
    if not args.check:
        config.setdefault("skills", {}).setdefault("platform_disabled", {})["weixin"] = disabled
        save_config(config)
    print(
        f"mode={'check' if args.check else 'applied'} "
        f"discovered={len(discovered)} enabled_weixin={len(KEEP)} disabled_weixin={len(disabled)}"
    )
    print("enabled: " + ", ".join(sorted(KEEP)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
