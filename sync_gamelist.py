#!/usr/bin/env python3
"""
从 vrsrc.fyi 官方 API 同步 VRP-GameList.txt（Rookie / VRP 客户端兼容格式）。

数据源: https://vrsrc.fyi/api/games
输出: VRP-GameList.txt + gamelist-manifest.json

用法:
  python sync_gamelist.py
  python sync_gamelist.py --offline
  python sync_gamelist.py --force
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VRSRC_GAMES_URL = "https://vrsrc.fyi/api/games"
API_KEY_ENC = (
    "USFedtuii+BQcEI3hbCLrEJregVqm/+H5hYzTGiD5JSwUzkdNZmgn+MMJxshmO2GpxMZcQpowPXU/1h+EXXc8Q=="
)
API_KEY_SALT = "vrp.downloader.vrsrc.api.key.v1"
API_HEADER = "X-API-Key"

HEADER = (
    "Game Name;Release Name;Package Name;Version Code;Last Updated;"
    "Size (MB);Downloads;Rating;Rating Count"
)

REPO_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = REPO_DIR / "VRP-GameList.txt"
DEFAULT_MANIFEST = REPO_DIR / "gamelist-manifest.json"
DEFAULT_CACHE = REPO_DIR / ".cache" / "vrsrc_api_cache.json"

_RELEASE_NAME_TITLE = re.compile(r" v\d+\+")

# 成人向内容检测（仅匹配标题/包名，避免 releasename 中 v18+ 版本号误伤）
_ADULT_MARKER_PARENS = re.compile(r"\(\s*18\s*\+\s*\)|\[\s*18\s*\+\s*\]", re.IGNORECASE)
_ADULT_KEYWORDS = re.compile(
    r"(?i)\b(r18|nsfw|porn(?:ography)?|hentai|erotic|xxx)\b"
)
_ADULT_SEX_WORD = re.compile(r"(?i)(^|[^a-z])sex([^a-z]|$)")
_ADULT_PHRASES = re.compile(
    r"(?i)(visual novel.*\(18\+\)|\(18\+\).*visual novel|vr sex|adult only|adults only)"
)
# 已知成人向发行包名前缀（防御性过滤，防止 API 未标记 isblacklisted）
_ADULT_PACKAGE_PREFIXES = (
    "com.rrrjpn.",
    "baddiesinc_",
    "com.oldhiccup.",
    "com.shattered.",
    "com.orchestranw.",
)


def decode_api_key() -> str:
    raw = base64.b64decode(API_KEY_ENC.strip())
    salt = API_KEY_SALT.encode("utf-8")
    decoded = bytes(
        raw[i] ^ salt[i % len(salt)] ^ ((i * 31 + 17) & 0xFF) for i in range(len(raw))
    )
    return decoded.decode("utf-8")


def emit_github_output(**kwargs: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in kwargs.items():
            f.write(f"{key}={value}\n")


def fetch_games(timeout: int = 180) -> list[dict]:
    api_key = decode_api_key()
    req = urllib.request.Request(
        VRSRC_GAMES_URL,
        headers={
            API_HEADER: api_key,
            "User-Agent": "quest-icons-sync-push/1.0",
            "Accept": "application/json",
        },
    )
    print(f"Fetching {VRSRC_GAMES_URL} ...")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
    print(f"Downloaded {len(payload):,} bytes")
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Unexpected API response type: {type(data).__name__}")
    return data


def load_cache(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"Cache not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected cache type: {type(data).__name__}")
    return data


def format_last_updated(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def size_mb(size_bytes: str | int | None) -> str:
    try:
        n = int(size_bytes or 0)
    except (TypeError, ValueError):
        return "0"
    if n <= 0:
        return "0"
    return str(round(n / 1024 / 1024))


def display_name(item: dict) -> str:
    name = (item.get("friendlyname") or item.get("gamename") or "").strip()
    if name:
        return name
    release = str(item.get("releasename") or "").strip()
    if not release:
        return ""
    return _RELEASE_NAME_TITLE.split(release, maxsplit=1)[0].strip()


def release_title(releasename: str) -> str:
    """Release Name 中版本号前的标题部分（不含 v123+version）。"""
    return _RELEASE_NAME_TITLE.split(releasename, maxsplit=1)[0].strip()


def adult_search_blob(item: dict) -> str:
    """合并用于成人向关键字检测的文本（不含版本号段）。"""
    parts = [
        str(item.get("friendlyname") or ""),
        str(item.get("gamename") or ""),
        release_title(str(item.get("releasename") or "")),
        str(item.get("packagename") or ""),
    ]
    return " ".join(p.strip() for p in parts if p.strip())


def is_adult_content(item: dict) -> bool:
    """
    检测成人向 / R18 游戏。

    规则：
    - (18+) / [18+] 出现在游戏名或 Release 标题（非 v18+ 版本号）
    - r18 / nsfw / sex / porn / hentai / erotic / xxx 等关键字
    - 已知成人向包名前缀
    """
    blob = adult_search_blob(item)
    if not blob:
        return False

    pkg = str(item.get("packagename") or "").lower()
    for prefix in _ADULT_PACKAGE_PREFIXES:
        if pkg.startswith(prefix):
            return True

    if _ADULT_MARKER_PARENS.search(blob):
        return True
    if _ADULT_KEYWORDS.search(blob):
        return True
    if _ADULT_SEX_WORD.search(blob):
        return True
    if _ADULT_PHRASES.search(blob):
        return True

    display = (item.get("friendlyname") or item.get("gamename") or "").strip()
    if display and re.search(r"(?i)\bxxx\b", display):
        return True

    return False


def filter_releases(items: list[dict]) -> tuple[list[dict], int]:
    out: list[dict] = []
    adult_filtered = 0
    for item in items:
        if not item.get("releasename"):
            continue
        if item.get("isblacklisted"):
            continue
        if is_adult_content(item):
            adult_filtered += 1
            continue
        out.append(item)
    return out, adult_filtered


def to_csv_row(item: dict) -> str:
    name = display_name(item)
    release = str(item.get("releasename") or "")
    package = str(item.get("packagename") or "")
    version = str(item.get("versioncode") or "")
    updated = format_last_updated(str(item.get("releasedutc") or "1970-01-01T00:00:00.000Z"))
    size = size_mb(item.get("sizebytes"))
    return ";".join([name, release, package, version, updated, size, "0", "0", "0"])


def build_gamelist(items: list[dict]) -> tuple[str, int]:
    releases, adult_filtered = filter_releases(items)
    releases.sort(key=lambda x: display_name(x).casefold())
    lines = [HEADER, *(to_csv_row(x) for x in releases)]
    return "\n".join(lines) + "\n", adult_filtered


def file_bytes(content: str) -> bytes:
    return b"\xef\xbb\xbf" + content.encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_existing_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def build_manifest(
    *,
    game_count: int,
    api_total_count: int,
    adult_filtered_count: int,
    content_sha256: str,
    file_size: int,
) -> dict:
    return {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "vrsrc.fyi",
        "api_url": VRSRC_GAMES_URL,
        "output_file": DEFAULT_OUTPUT.name,
        "game_count": game_count,
        "api_total_count": api_total_count,
        "adult_filtered_count": adult_filtered_count,
        "content_sha256": content_sha256,
        "file_size_bytes": file_size,
        "format": {
            "delimiter": ";",
            "columns": HEADER.split(";"),
        },
        "filters": {
            "skip_isblacklisted": True,
            "skip_adult_content": True,
            "adult_markers": ["(18+)", "r18", "nsfw", "sex", "porn", "hentai", "erotic", "xxx"],
        },
    }


def save_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync VRP-GameList.txt from vrsrc.fyi")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--save-cache", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    try:
        if args.offline:
            print(f"Offline mode, reading {args.cache}")
            items = load_cache(args.cache)
        else:
            items = fetch_games(timeout=args.timeout)
            if args.save_cache:
                args.cache.parent.mkdir(parents=True, exist_ok=True)
                with args.cache.open("w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False)
                print(f"Saved API cache: {args.cache} ({len(items):,} items)")

        content, adult_filtered = build_gamelist(items)
        releases, _ = filter_releases(items)
        payload = file_bytes(content)
        new_sha = sha256_bytes(payload)
        old_sha = read_existing_sha256(args.output)
        changed = old_sha != new_sha

        if changed or args.force or not args.output.is_file():
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(payload)
            manifest = build_manifest(
                game_count=len(releases),
                api_total_count=len(items),
                adult_filtered_count=adult_filtered,
                content_sha256=new_sha,
                file_size=len(payload),
            )
            save_manifest(args.manifest, manifest)
            print(
                f"Wrote {args.output} ({len(payload):,} bytes, {len(releases):,} games, "
                f"adult filtered: {adult_filtered})"
            )
            print(f"Manifest: {args.manifest}")
        else:
            print(
                f"No changes detected (sha256={new_sha[:12]}..., {len(releases):,} games, "
                f"adult filtered: {adult_filtered})"
            )

        emit_github_output(
            changed="1" if changed else "0",
            game_count=len(releases),
            api_total_count=len(items),
            adult_filtered_count=adult_filtered,
            content_sha256=new_sha,
        )
        print(
            f"API total: {len(items):,} | releases: {len(releases):,} | "
            f"adult filtered: {adult_filtered} | changed: {changed}"
        )
        return 0

    except urllib.error.HTTPError as exc:
        print(f"HTTP error {exc.code}: {exc.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error: {exc.reason}", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
