"""Structured package, safe official-image intake, and resumable delivery.

This module is deliberately independent from ACT and from any agent runtime.
It accepts already selected news as plain data, writes only below the caller's
state directory, and keeps the legacy brief state file untouched.
"""

from __future__ import annotations

import hashlib
import html.parser
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


PACKAGE_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
MAX_HTML_BYTES = 512_000
MAX_IMAGE_BYTES = 8_000_000
MAX_REDIRECTS = 3
MIN_IMAGE_WIDTH = 600
MIN_IMAGE_HEIGHT = 300
MAX_IMAGE_PIXELS = 16_000_000
PART_DELAY_SECONDS = 35
BRIEF_MAX_AGE = timedelta(hours=48)

DEFAULT_BRIEF_COPY = {
    "no_items": "这个时间窗没有达到门槛的重要更新。",
    "top_item_prefix": "今天最值得先看的是：",
    "more_hint": "想继续往下看，就回「展开 2」；想先看完整清单，就回「更多新闻」。",
    "short_morning_guide": (
        "早上好，昨夜新闻已整理。\n\n"
        "今天最重要的一件事是什么？想好了就回「今日重点：___」；"
        "还没想好可以说「帮我从 ACT 里选」。"
    ),
    "short_evening_guide": (
        "今天的新闻已整理。你可以回复「今日收尾：推进___；学到___；"
        "卡点___；明天___」，或者说「帮我一步步回顾」。"
    ),
    "full_morning_guide": (
        "早上好，昨夜新闻已整理。\n\n"
        "你接下来可以直接回复：\n"
        "- 今日重点：___\n"
        "- 帮我从 ACT 里选\n"
        "- 今天先不规划"
    ),
    "full_evening_guide": (
        "今天的新闻已整理。\n"
        "你接下来可以直接回复：\n"
        "- 今日收尾：推进___；学到___；卡点___；明天___\n"
        "- 帮我一步步回顾\n"
        "- 今天不保存"
    ),
    "source_note": "AIHOT 摘要属于外部整理，重要结论请回原链接核对。",
}

# Page hosts and image hosts are maintained separately. A matching publisher
# page never grants arbitrary CDN access. Unknown or changed hosts fall back to
# the pure Swiss layout.
OFFICIAL_PAGE_DOMAINS = {
    "openai.com", "anthropic.com", "deepmind.google", "blog.google",
    "microsoft.com", "azure.microsoft.com", "ai.meta.com", "about.fb.com",
    "nvidia.com", "huggingface.co", "github.com", "qwenlm.github.io",
    "alibabacloud.com", "deepseek.com", "mistral.ai", "x.ai",
}
OFFICIAL_IMAGE_DOMAINS = OFFICIAL_PAGE_DOMAINS | {
    "cdn.openai.com", "assets.anthropic.com",
    "news.microsoft.com", "developer-blogs.nvidia.com",
    "github.blog",
}


class PackageError(RuntimeError):
    """Fail-closed error safe to persist in a manifest."""


def load_brief_copy() -> dict[str, str]:
    """Load optional private delivery copy without modifying the public repo."""
    configured = os.environ.get("ACT_BRIEF_COPY_FILE", "").strip()
    if not configured:
        return dict(DEFAULT_BRIEF_COPY)

    copy_path = Path(configured)
    if copy_path.is_symlink() or not copy_path.is_file():
        raise PackageError("ACT_BRIEF_COPY_FILE 必须指向普通文件。")
    if copy_path.stat().st_size > 32_000:
        raise PackageError("私人简报文案文件过大。")
    try:
        payload = json.loads(copy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError("无法读取私人简报文案文件。") from exc
    if not isinstance(payload, dict) or set(payload) != set(DEFAULT_BRIEF_COPY):
        raise PackageError("私人简报文案字段不完整或包含未知字段。")
    if any(
        not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 8_000
        for value in payload.values()
    ):
        raise PackageError("私人简报文案包含无效值。")
    return payload


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _domain_allowed(host: str, allowlist: set[str]) -> bool:
    host = host.rstrip(".").lower()
    return any(host == domain or host.endswith("." + domain) for domain in allowlist)


def is_official_page_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (ValueError, UnicodeError):
        return False
    return (
        parsed.scheme == "https"
        and bool(host)
        and not parsed.username
        and not parsed.password
        and port in {None, 443}
        and _domain_allowed(host, OFFICIAL_PAGE_DOMAINS)
    )


def card_groups(item_count: int) -> list[list[int]]:
    """Map 0..12 items to 0..3 cards with at most four items per card."""
    if not isinstance(item_count, int) or not 0 <= item_count <= 12:
        raise PackageError("简报条目数必须在 0 到 12 之间。")
    return [list(range(start + 1, min(start + 4, item_count) + 1)) for start in range(0, item_count, 4)]


def build_short_text(slot: str, package: dict[str, Any]) -> str:
    copy = load_brief_copy()
    items = package.get("items", [])
    if not items:
        news = copy["no_items"]
    else:
        first = items[0]
        news = f"{copy['top_item_prefix']}\n{first['title']}\n{first['url']}"
        if len(items) > 1:
            news += "\n" + copy["more_hint"]
    if slot == "morning":
        guide = copy["short_morning_guide"]
    else:
        guide = copy["short_evening_guide"]
    note = copy["source_note"]
    return f"{news}\n\n{guide}\n\n{note}"


def build_package(
    *,
    slot: str,
    start: datetime,
    end: datetime,
    items: Iterable[dict[str, Any]],
    coverage: dict[str, Any],
    generated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    if slot not in {"morning", "evening"}:
        raise PackageError("未知简报时段。")
    generated_at = generated_at or datetime.now(timezone.utc)
    normalized: list[dict[str, Any]] = []
    for number, item in enumerate(items, 1):
        if number > 12:
            break
        normalized.append({
            "number": number,
            "title": str(item.get("title", ""))[:220],
            "source": str(item.get("source", ""))[:100],
            "published_at": str(item.get("published_at", "")),
            "summary": str(item.get("summary", ""))[:360],
            "category": str(item.get("category", ""))[:40],
            "url": str(item.get("url", ""))[:2000],
            "canonical_url": str(item.get("canonical_url", ""))[:2000],
            "impact": str(item.get("impact", ""))[:240],
            "is_update": bool(item.get("is_update", False)),
        })
    digest_input = json.dumps(
        {
            "slot": slot,
            "start": _iso_utc(start),
            "end": _iso_utc(end),
            "items": [(item["canonical_url"], item["title"]) for item in normalized],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    brief_id = f"{end:%Y%m%dT%H%MZ}-{slot}-{hashlib.sha256(digest_input).hexdigest()[:12]}"
    groups = card_groups(len(normalized))
    cards = []
    for index, numbers in enumerate(groups, 1):
        has_official_candidate = index == 1 and bool(normalized) and is_official_page_url(normalized[0]["canonical_url"])
        if has_official_candidate:
            layout = "S04"
        elif len(numbers) <= 2:
            layout = "S01"
        elif index == len(groups):
            layout = "S07"
        else:
            layout = "S11"
        cards.append({"card_id": f"card-{index:02d}", "item_numbers": numbers, "layout": layout})
    package = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "brief_id": brief_id,
        "slot": slot,
        "window": {"start": _iso_utc(start), "end": _iso_utc(end)},
        "generated_at": _iso_utc(generated_at),
        "items": normalized,
        "cards": cards,
        "top_url": normalized[0]["url"] if normalized else "",
        "coverage": {
            "available": list(coverage.get("available", [])),
            "failures": list(coverage.get("failures", [])),
            "partial": bool(coverage.get("partial", False)),
            "candidate_count": int(coverage.get("candidate_count", len(normalized))),
        },
        "official_image": None,
        "render": {"status": "pending" if cards else "not_needed", "files": []},
    }
    package["guide_text"] = build_short_text(slot, package)
    return package


def apply_official_image_fallback(package: dict[str, Any]) -> None:
    """Keep the declared recipe aligned with the actual no-image render."""
    cards = package.get("cards", [])
    if not cards or package.get("official_image") or cards[0].get("layout") != "S04":
        return
    count = len(cards[0].get("item_numbers", []))
    cards[0]["layout"] = "S01" if count <= 2 else ("S07" if len(cards) == 1 else "S11")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
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


class _OGParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "meta" or self.image:
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        if values.get("property", "").lower() in {"og:image", "og:image:secure_url"}:
            self.image = values.get("content", "").strip()


def extract_og_image(page_url: str, body: bytes) -> Optional[str]:
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return None
    parser = _OGParser()
    parser.feed(text)
    if not parser.image:
        return None
    candidate = urllib.parse.urljoin(page_url, parser.image)
    try:
        parsed = urllib.parse.urlsplit(candidate)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (ValueError, UnicodeError):
        return None
    if (
        parsed.scheme != "https" or not host or parsed.username or parsed.password
        or port not in {None, 443} or not _domain_allowed(host, OFFICIAL_IMAGE_DOMAINS)
    ):
        return None
    return urllib.parse.urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _public_addresses(host: str) -> list[str]:
    try:
        info = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PackageError("官方图片主机无法解析。") from exc
    addresses = sorted({entry[4][0] for entry in info})
    if not addresses:
        raise PackageError("官方图片主机没有可用地址。")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise PackageError("官方图片读取拒绝私网或保留地址。")
    return addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float = 20.0):
        self._address = address
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())

    def connect(self) -> None:
        raw = socket.create_connection((self._address, 443), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _safe_https_fetch(url: str, *, limit: int, accepted: tuple[str, ...]) -> tuple[str, str, bytes]:
    current = url
    for redirect in range(MAX_REDIRECTS + 1):
        try:
            parsed = urllib.parse.urlsplit(current)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
            port = parsed.port
        except (ValueError, UnicodeError) as exc:
            raise PackageError("官方资源 URL 无效。") from exc
        allowlist = OFFICIAL_PAGE_DOMAINS if "text/html" in accepted else OFFICIAL_IMAGE_DOMAINS
        if (
            parsed.scheme != "https" or not host or parsed.username or parsed.password
            or port not in {None, 443} or not _domain_allowed(host, allowlist)
        ):
            raise PackageError("官方资源 URL 不在受控 HTTPS 白名单。")
        address = _public_addresses(host)[0]
        connection = _PinnedHTTPS(host, address)
        target = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            target += "?" + urllib.parse.quote(parsed.query, safe="=&%:@!$'()*+,;/?-._~")
        try:
            connection.request("GET", target, headers={
                "Host": host,
                "User-Agent": "ACT-Shark-Brief/1.0",
                "Accept": ",".join(accepted),
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                if redirect >= MAX_REDIRECTS or not headers.get("location"):
                    raise PackageError("官方资源重定向次数过多。")
                response.read(64_000)
                current = urllib.parse.urljoin(current, headers["location"])
                continue
            if response.status != 200:
                raise PackageError(f"官方资源返回 HTTP {response.status}。")
            length = headers.get("content-length", "")
            if length.isdigit() and int(length) > limit:
                raise PackageError("官方资源超过大小限制。")
            body = response.read(limit + 1)
            if len(body) > limit:
                raise PackageError("官方资源超过大小限制。")
            mime = headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
            if mime not in accepted:
                raise PackageError("官方资源 MIME 不受支持。")
            return current, mime, body
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise PackageError("官方资源读取失败。") from exc
        finally:
            connection.close()
    raise PackageError("官方资源重定向次数过多。")


def image_dimensions(body: bytes, mime: str) -> tuple[int, int, str]:
    if mime == "image/png" and body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 24:
        width, height = struct.unpack(">II", body[16:24])
        extension = "png"
    elif mime == "image/jpeg" and body.startswith(b"\xff\xd8"):
        index = 2
        width = height = 0
        while index + 9 < len(body):
            if body[index] != 0xFF:
                index += 1
                continue
            marker = body[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(body):
                break
            length = struct.unpack(">H", body[index:index + 2])[0]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and index + 7 <= len(body):
                height, width = struct.unpack(">HH", body[index + 3:index + 7])
                break
            index += max(length, 2)
        if not width or not height:
            raise PackageError("JPEG 尺寸无法验证。")
        extension = "jpg"
    elif mime == "image/webp" and body.startswith(b"RIFF") and body[8:12] == b"WEBP" and body[12:16] == b"VP8X" and len(body) >= 30:
        width = 1 + int.from_bytes(body[24:27], "little")
        height = 1 + int.from_bytes(body[27:30], "little")
        extension = "webp"
    else:
        raise PackageError("图片 MIME 与魔数不匹配。")
    if width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT or width * height > MAX_IMAGE_PIXELS:
        raise PackageError("官方图片尺寸不符合简报限制。")
    return width, height, extension


def acquire_official_image(package: dict[str, Any], asset_dir: Path) -> Optional[dict[str, Any]]:
    items = package.get("items", [])
    if not items or not is_official_page_url(items[0].get("canonical_url", "")):
        return None
    page_url, _mime, body = _safe_https_fetch(
        items[0]["canonical_url"], limit=MAX_HTML_BYTES, accepted=("text/html",)
    )
    image_url = extract_og_image(page_url, body)
    if not image_url:
        return None
    final_url, mime, image = _safe_https_fetch(
        image_url, limit=MAX_IMAGE_BYTES, accepted=("image/png", "image/jpeg", "image/webp")
    )
    width, height, extension = image_dimensions(image, mime)
    asset_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(image).hexdigest()
    path = asset_dir / f"official-{digest[:16]}.{extension}"
    path.write_bytes(image)
    os.chmod(path, 0o600)
    result = {
        "page_url": page_url,
        "image_url": final_url,
        "domain": urllib.parse.urlsplit(final_url).hostname,
        "sha256": digest,
        "mime": mime,
        "width": width,
        "height": height,
        "file": str(path),
    }
    package["official_image"] = result
    return result


def render_package(
    package_path: Path,
    output_dir: Path,
    *,
    renderer: Path,
    template: Path,
    node: str = "node",
    env: Optional[dict[str, str]] = None,
) -> list[Path]:
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = subprocess.run(
        [node, str(renderer), str(package_path), str(template), str(output_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        detail = re.sub(r"\s+", " ", result.stderr or result.stdout).strip()[:300]
        raise PackageError(f"Guizang 渲染失败：{detail or 'unknown error'}")
    try:
        payload = json.loads(result.stdout)
        files = [Path(value) for value in payload["files"]]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PackageError("Guizang 渲染器返回结构无效。") from exc
    if not files or any(not path.is_file() for path in files):
        raise PackageError("Guizang 渲染结果缺失。")
    return files


def render_via_queue(
    package: dict[str, Any],
    state_dir: Path,
    *,
    timeout_seconds: int = 180,
) -> list[Path]:
    """Submit sanitized inputs to the isolated renderer sidecar and wait for files."""
    brief_id = str(package.get("brief_id", ""))
    if not re.fullmatch(r"[0-9]{8}T[0-9]{4}Z-(?:morning|evening)-[a-f0-9]{12}", brief_id):
        raise PackageError("简报 ID 无法进入渲染队列。")
    job_dir = state_dir / "render-queue" / brief_id
    output_dir = job_dir / "output"
    job_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    queued = json.loads(json.dumps(package, ensure_ascii=False))
    evidence = queued.get("official_image")
    if isinstance(evidence, dict) and evidence.get("file"):
        source = Path(str(evidence["file"]))
        if source.is_file() and not source.is_symlink():
            assets = job_dir / "assets"
            assets.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = assets / source.name
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
            evidence["file"] = f"/queue/{brief_id}/assets/{target.name}"
        else:
            queued["official_image"] = None
    atomic_write_json(job_dir / "package.json", queued)
    result_path = job_dir / "result.json"
    if not result_path.exists():
        atomic_write_json(job_dir / "request.json", {
            "schema_version": 1,
            "brief_id": brief_id,
            "requested_at": _iso_utc(datetime.now(timezone.utc)),
        })
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if result_path.is_file() and not result_path.is_symlink():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PackageError("渲染队列返回无效结果。") from exc
            if not result.get("ok"):
                raise PackageError(f"Guizang 渲染失败：{str(result.get('error', 'unknown'))[:300]}")
            files = [output_dir / Path(str(name)).name for name in result.get("files", [])]
            if not files or any(not path.is_file() or path.is_symlink() for path in files):
                raise PackageError("渲染队列结果文件缺失。")
            return files
        time.sleep(0.5)
    raise PackageError("Guizang 渲染队列超时。")


ImageSender = Callable[[Path], None]
TextSender = Callable[[str], None]


def deliver_resumably(
    *,
    package: dict[str, Any],
    state_dir: Path,
    media_files: list[Path],
    fallback_text: str,
    image_sender: ImageSender,
    text_sender: TextSender,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    manifest_dir = state_dir / "delivery-manifests" / package["brief_id"]
    manifest_path = manifest_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        parts = [
            {"id": f"image-{index:02d}", "kind": "image", "file": str(path), "sent_at": None}
            for index, path in enumerate(media_files, 1)
        ]
        parts.append({
            "id": "text", "kind": "text",
            "text": package.get("guide_text") if media_files else fallback_text,
            "sent_at": None,
        })
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "brief_id": package["brief_id"],
            "slot": package["slot"],
            "window": package["window"],
            "package": package,
            "created_at": _iso_utc(now()),
            "parts": parts,
            "delivered_complete": False,
            "completed_at": None,
        }
        atomic_write_json(manifest_path, manifest)
    pending_images = [part for part in manifest["parts"] if part["kind"] == "image" and not part.get("sent_at")]
    for index, part in enumerate(pending_images):
        image_sender(Path(part["file"]))
        part["sent_at"] = _iso_utc(now())
        atomic_write_json(manifest_path, manifest)
        if index < len(pending_images) - 1:
            sleeper(PART_DELAY_SECONDS)
    text_part = next(part for part in manifest["parts"] if part["kind"] == "text")
    if not text_part.get("sent_at"):
        if pending_images:
            sleeper(PART_DELAY_SECONDS)
        text_sender(text_part["text"])
        text_part["sent_at"] = _iso_utc(now())
        atomic_write_json(manifest_path, manifest)
    manifest["delivered_complete"] = True
    manifest["completed_at"] = _iso_utc(now())
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(state_dir / "delivery-manifests" / "latest.json", {
        "schema_version": 1,
        "manifest": str(manifest_path),
        "brief_id": package["brief_id"],
        "completed_at": manifest["completed_at"],
    })
    return manifest


def find_pending_manifest(state_dir: Path, slot: str, window_end: str) -> Optional[dict[str, Any]]:
    root = state_dir / "delivery-manifests"
    if not root.is_dir():
        return None
    matches: list[tuple[str, dict[str, Any]]] = []
    for path in root.glob("*/manifest.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            payload.get("schema_version") == MANIFEST_SCHEMA_VERSION
            and payload.get("slot") == slot
            and payload.get("window", {}).get("end") == window_end
            and not payload.get("delivered_complete")
        ):
            matches.append((str(payload.get("created_at", "")), payload))
    if not matches:
        return None
    return max(matches, key=lambda value: value[0])[1]


def find_oldest_pending_manifest(state_dir: Path) -> Optional[dict[str, Any]]:
    """Return the oldest valid incomplete delivery, regardless of cron slot.

    A morning delivery can still be incomplete when the evening cron starts.
    Recovery must finish that immutable manifest before selecting newer news.
    """
    root = state_dir / "delivery-manifests"
    if not root.is_dir() or root.is_symlink():
        return None
    root_resolved = root.resolve(strict=True)
    matches: list[tuple[datetime, str, dict[str, Any]]] = []
    for path in root.glob("*/manifest.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.resolve(strict=True).relative_to(root_resolved)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        window_end = _parse_time(payload.get("window", {}).get("end"))
        if (
            payload.get("schema_version") == MANIFEST_SCHEMA_VERSION
            and window_end is not None
            and not payload.get("delivered_complete")
            and isinstance(payload.get("package"), dict)
            and isinstance(payload.get("parts"), list)
        ):
            matches.append((window_end, str(payload.get("created_at", "")), payload))
    if not matches:
        return None
    return min(matches, key=lambda value: (value[0], value[1]))[2]


def read_latest_brief_item(state_dir: Path, number: int, *, now: Optional[datetime] = None) -> dict[str, Any]:
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 12:
        raise PackageError("编号必须是 1 到 12。")
    latest_path = state_dir / "delivery-manifests" / "latest.json"
    if latest_path.is_symlink() or not latest_path.is_file():
        raise PackageError("还没有可展开的已投递简报。")
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    manifest_path = Path(str(latest.get("manifest", "")))
    root = (state_dir / "delivery-manifests").resolve()
    try:
        resolved = manifest_path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise PackageError("最新简报索引无效。") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise PackageError("最新简报 manifest 无效。")
    manifest = json.loads(resolved.read_text(encoding="utf-8"))
    completed = _parse_time(manifest.get("completed_at"))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not manifest.get("delivered_complete") or completed is None or current - completed > BRIEF_MAX_AGE or completed > current + timedelta(minutes=5):
        raise PackageError("最近简报已过期或尚未完整投递。")
    items = manifest.get("package", {}).get("items", [])
    if number > len(items):
        raise PackageError(f"本期只有 {len(items)} 条，编号 {number} 不存在。")
    item = items[number - 1]
    return {
        "brief_id": manifest["brief_id"],
        "number": number,
        "item_count": len(items),
        "title": item["title"],
        "source": item["source"],
        "published_at": item["published_at"],
        "impact": item["impact"],
        "summary": item["summary"],
        "url": item["url"],
    }
