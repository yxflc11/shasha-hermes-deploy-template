#!/usr/bin/env python3
"""Apply the audited ACT persona, Skill allowlist, and tools to Telegram."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Optional

KEEP_SKILLS = {
    "act-daily-companion",
    "act-article-intake",
    "aihot",
    "act-news-brief",
    "act-source-verification",
    "act-context-query",
    "act-usage-guide",
    "act-shark-companion",
    "act-web-research",
    "grounded-citations",
}
KEEP_TOOLSETS = ["memory", "session_search", "skills", "todo", "web"]
PERSONA_CONTRACT_MARKERS = (
    "[鲨鲨全局回复协议 v1]",
    "无论本轮调用零个、一个或多个 Skill",
    "按语义选场景",
    "避免近期重复",
    "专业通用",
    "精确确认词",
)
BUSINESS_PERSONA_COUPLING_MARKERS = (
    "表达遵循鲨鲨人格",
    "鲨鲨式态度",
    "必须先用一句亲昵吐槽",
    "日常傲娇采用",
    "标准语气参考",
)
COMPANION_ENV = {
    "HERMES_HOME": "/opt/data",
    "ACT_COMPANION_CHANNEL": "telegram_dm",
}


def _read_prompt(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("persona prompt must be a regular file")
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt or "\x00" in prompt or len(prompt) > 8_000:
        raise RuntimeError("persona prompt is empty or invalid")
    missing_markers = [
        marker for marker in PERSONA_CONTRACT_MARKERS if marker not in prompt
    ]
    if missing_markers:
        raise RuntimeError(
            "persona prompt is missing global contract markers: "
            + ", ".join(missing_markers)
        )
    skills_root = path.parents[2]
    if skills_root.is_dir():
        coupled: list[str] = []
        for skill_path in sorted(skills_root.glob("*/*/SKILL.md")):
            if skill_path.parent.name == "act-shark-companion":
                continue
            text = skill_path.read_text(encoding="utf-8")
            matches = [
                marker
                for marker in BUSINESS_PERSONA_COUPLING_MARKERS
                if marker in text
            ]
            if matches:
                relative_skill = skill_path.parent.relative_to(skills_root)
                coupled.append(f"{relative_skill}: {', '.join(matches)}")
        if coupled:
            raise RuntimeError(
                "business skills duplicate the global persona contract: "
                + "; ".join(coupled)
            )
    return prompt


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def policy_snapshot(config: dict) -> dict[str, object]:
    hints = ((config.get("agent") or {}).get("platform_hints") or {})
    current_hint = hints.get("telegram")
    current_prompt = (
        current_hint.get("append") if isinstance(current_hint, dict) else None
    )
    disabled = (
        ((config.get("skills") or {}).get("platform_disabled") or {}).get(
            "telegram"
        )
    )
    toolsets = (config.get("platform_toolsets") or {}).get("telegram")
    return {
        "prompt_sha256": (
            hashlib.sha256(current_prompt.encode("utf-8")).hexdigest()
            if isinstance(current_prompt, str)
            else None
        ),
        "platform_disabled_sha256": _canonical_sha256(disabled),
        "platform_disabled_count": len(disabled) if isinstance(disabled, list) else None,
        "toolsets": toolsets,
        "toolsets_sha256": _canonical_sha256(toolsets),
    }


def _validate_preserved_prompt(config: dict, expected_sha256: str) -> None:
    hints = ((config.get("agent") or {}).get("platform_hints") or {})
    current_hint = hints.get("telegram")
    current_prompt = (
        current_hint.get("append") if isinstance(current_hint, dict) else None
    )
    if not isinstance(current_prompt, str):
        raise RuntimeError("telegram platform hint has an unexpected structure")
    current_digest = hashlib.sha256(current_prompt.encode("utf-8")).hexdigest()
    if current_digest != expected_sha256:
        raise RuntimeError(
            "telegram platform hint changed unexpectedly: "
            f"expected {expected_sha256}, got {current_digest}"
        )


def _allow_policy_replacement(
    *,
    current_disabled: object,
    current_toolsets: object,
    migrate_platform_policy: bool,
    expected_current_disabled_sha256: Optional[str],
    expected_current_toolsets_sha256: Optional[str],
) -> None:
    if not migrate_platform_policy:
        raise RuntimeError("telegram Skill/tool policy already differs from the ACT allowlist")
    if not expected_current_disabled_sha256 or not expected_current_toolsets_sha256:
        raise RuntimeError(
            "policy migration requires expected disabled and toolset sha256 values"
        )
    disabled_digest = _canonical_sha256(current_disabled)
    toolsets_digest = _canonical_sha256(current_toolsets)
    if disabled_digest != expected_current_disabled_sha256:
        raise RuntimeError(
            "telegram disabled Skill policy changed unexpectedly: "
            f"expected {expected_current_disabled_sha256}, got {disabled_digest}"
        )
    if toolsets_digest != expected_current_toolsets_sha256:
        raise RuntimeError(
            "telegram toolsets changed unexpectedly: "
            f"expected {expected_current_toolsets_sha256}, got {toolsets_digest}"
        )


def desired_state(
    prompt: Optional[str],
    *,
    preserve_existing_prompt_sha256: Optional[str] = None,
    replace_existing_prompt: bool = False,
    expected_current_prompt_sha256: Optional[str] = None,
    migrate_platform_policy: bool = False,
    expected_current_disabled_sha256: Optional[str] = None,
    expected_current_toolsets_sha256: Optional[str] = None,
) -> tuple[dict, str, list[str], set[str]]:
    from hermes_cli.config import load_config
    from tools.skills_tool import _find_all_skills

    config = load_config()
    discovered = {
        str(item.get("name") or "").strip()
        for item in _find_all_skills(skip_disabled=True)
        if str(item.get("name") or "").strip()
    }
    missing = KEEP_SKILLS - discovered
    enabled = KEEP_SKILLS
    globally_disabled = set((config.get("skills") or {}).get("disabled") or [])
    blocked_keep = enabled.intersection(globally_disabled)
    if missing:
        raise RuntimeError(f"missing required skills: {', '.join(sorted(missing))}")
    if blocked_keep:
        raise RuntimeError(
            "required skills are globally disabled: " + ", ".join(sorted(blocked_keep))
        )

    disabled = sorted(discovered - enabled)
    if prompt is not None and preserve_existing_prompt_sha256:
        raise RuntimeError("choose either a new prompt or preserve-existing-prompt")
    hints = config.setdefault("agent", {}).setdefault("platform_hints", {})
    if preserve_existing_prompt_sha256:
        _validate_preserved_prompt(config, preserve_existing_prompt_sha256)
        digest = preserve_existing_prompt_sha256
    elif prompt is not None:
        desired_hint = {"append": prompt}
        current_hint = hints.get("telegram")
        if current_hint not in (None, desired_hint):
            current_prompt = (
                current_hint.get("append") if isinstance(current_hint, dict) else None
            )
            if not isinstance(current_prompt, str):
                raise RuntimeError("telegram platform hint has an unexpected structure")
            current_digest = hashlib.sha256(current_prompt.encode("utf-8")).hexdigest()
            if not replace_existing_prompt:
                raise RuntimeError("telegram platform hint already contains different content")
            if not expected_current_prompt_sha256:
                raise RuntimeError(
                    "replacing the telegram prompt requires its expected current sha256"
                )
            if current_digest != expected_current_prompt_sha256:
                raise RuntimeError(
                    "telegram platform hint changed unexpectedly: "
                    f"expected {expected_current_prompt_sha256}, got {current_digest}"
                )
        hints["telegram"] = desired_hint
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    else:
        raise RuntimeError("a prompt file or preserved prompt sha256 is required")

    platform_disabled = config.setdefault("skills", {}).setdefault(
        "platform_disabled", {}
    )
    current_disabled = platform_disabled.get("telegram")
    platform_toolsets = config.setdefault("platform_toolsets", {})
    current_toolsets = platform_toolsets.get("telegram")
    policy_differs = current_disabled not in (None, disabled) or current_toolsets not in (
        None,
        KEEP_TOOLSETS,
    )
    if policy_differs:
        _allow_policy_replacement(
            current_disabled=current_disabled,
            current_toolsets=current_toolsets,
            migrate_platform_policy=migrate_platform_policy,
            expected_current_disabled_sha256=expected_current_disabled_sha256,
            expected_current_toolsets_sha256=expected_current_toolsets_sha256,
        )
    platform_disabled["telegram"] = disabled

    platform_toolsets["telegram"] = KEEP_TOOLSETS

    security = config.setdefault("security", {})
    current_private = security.get("allow_private_urls")
    if current_private not in (None, False):
        raise RuntimeError("security.allow_private_urls must remain false")
    security["allow_private_urls"] = False

    web = config.setdefault("web", {})
    for key in ("backend", "search_backend", "extract_backend", "provider_tier"):
        if key in web and web[key] not in (None, {}, ""):
            raise RuntimeError(
                f"web.{key} is already configured; free keyless migration refuses to override it"
            )
    for key in ("keyless_fallback", "keyless_rescue"):
        if web.get(key) not in (None, True):
            raise RuntimeError(f"web.{key} must remain enabled")
        web[key] = True

    mcp_servers = config.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        raise RuntimeError("mcp_servers config is missing or invalid")
    companion = mcp_servers.get("act-companion")
    if not isinstance(companion, dict):
        raise RuntimeError("act-companion MCP server is missing")
    companion_env = companion.get("env")
    if companion_env is None:
        companion_env = {}
    if not isinstance(companion_env, dict):
        raise RuntimeError("act-companion MCP env is invalid")
    for key, value in COMPANION_ENV.items():
        current = companion_env.get(key)
        if current not in (None, value):
            raise RuntimeError(
                f"act-companion MCP env {key} already differs from the ACT policy"
            )
        companion_env[key] = value
    companion["env"] = companion_env

    telegram_display = (
        config.setdefault("display", {})
        .setdefault("platforms", {})
        .setdefault("telegram", {})
    )
    telegram_display["show_reasoning"] = False

    return config, digest, disabled, discovered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt-file", type=Path)
    prompt_group.add_argument("--preserve-existing-prompt-sha256")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--replace-existing-prompt", action="store_true")
    parser.add_argument("--expected-current-prompt-sha256")
    parser.add_argument("--migrate-platform-policy", action="store_true")
    parser.add_argument("--expected-current-disabled-sha256")
    parser.add_argument("--expected-current-toolsets-sha256")
    args = parser.parse_args()

    if args.snapshot:
        from hermes_cli.config import load_config

        print(json.dumps(policy_snapshot(load_config()), ensure_ascii=False, sort_keys=True))
        return 0
    if not args.prompt_file and not args.preserve_existing_prompt_sha256:
        parser.error("one of --prompt-file or --preserve-existing-prompt-sha256 is required")
    prompt = _read_prompt(args.prompt_file) if args.prompt_file else None
    config, digest, disabled, discovered = desired_state(
        prompt,
        preserve_existing_prompt_sha256=args.preserve_existing_prompt_sha256,
        replace_existing_prompt=args.replace_existing_prompt,
        expected_current_prompt_sha256=args.expected_current_prompt_sha256,
        migrate_platform_policy=args.migrate_platform_policy,
        expected_current_disabled_sha256=args.expected_current_disabled_sha256,
        expected_current_toolsets_sha256=args.expected_current_toolsets_sha256,
    )
    if not args.check:
        from hermes_cli.config import save_config

        save_config(config)
    print(
        f"mode={'check' if args.check else 'applied'} platform=telegram "
        f"prompt_sha256={digest} discovered={len(discovered)} "
        f"enabled_skills={len(discovered) - len(disabled)} "
        f"disabled_skills={len(disabled)} "
        f"toolsets={','.join(KEEP_TOOLSETS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
