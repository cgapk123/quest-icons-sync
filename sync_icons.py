#!/usr/bin/env python3
"""
Quest 图标自动同步工具
- 从 MetaMetadata (GitHub) 或本地 data/common 读取 JSON
- 下载、压缩图标，保存为 com.xxxx.jpg
- 可选 SFTP 上传到服务器
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import requests
from PIL import Image
from requests.adapters import HTTPAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

DEFAULT_METADATA_REPO = "https://github.com/threethan/MetaMetadata.git"
DEFAULT_METADATA_RAW = "https://raw.githubusercontent.com/threethan/MetaMetadata/main"
DEFAULT_ICON_DIR = "icons"
DEFAULT_MANIFEST = "manifest.json"
DEFAULT_MAX_FILE_SIZE = 50 * 1024  # 50KB
DEFAULT_MAX_WORKERS = 8

ICON_FIELD_PRIORITY = {
    "landscape": 1,
    "icon": 2,
    "square": 3,
    "portrait": 4,
    "logo": 5,
    "hero": 6,
}

SKIP_URL_FRAGMENTS = (".gif", "steam/apps")

session = requests.Session()
adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
session.mount("http://", adapter)
session.mount("https://", adapter)

icon_stats = {"downloaded": 0, "updated": 0, "compressed": 0, "failed": 0, "skipped": 0}


@dataclass
class AppIcon:
    package_name: str
    icon_url: str
    icon_type: str
    source: str


# ---------------------------------------------------------------------------
# 图片处理（沿用本地脚本逻辑）
# ---------------------------------------------------------------------------

def compress_image(image_data: bytes, target_size: int = DEFAULT_MAX_FILE_SIZE) -> Optional[bytes]:
    try:
        img = Image.open(io.BytesIO(image_data))

        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        max_dimension = 512
        if max(img.size) > max_dimension:
            img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

        quality = 85
        compressed_data = image_data
        while quality >= 20:
            output = io.BytesIO()
            img.save(output, format="JPEG", quality=quality, optimize=True)
            compressed_data = output.getvalue()
            if len(compressed_data) <= target_size or quality <= 20:
                icon_stats["compressed"] += 1
                return compressed_data
            quality -= 10

        if len(compressed_data) > target_size:
            for size in (256, 128, 64):
                img_small = img.copy()
                img_small.thumbnail((size, size), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                img_small.save(output, format="JPEG", quality=20, optimize=True)
                compressed_data = output.getvalue()
                if len(compressed_data) <= target_size:
                    break

        return compressed_data
    except Exception as exc:
        log.error("压缩失败: %s", exc)
        return image_data if len(image_data) <= target_size * 2 else None


def safe_package_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# MetaMetadata 数据获取
# ---------------------------------------------------------------------------

def git_sparse_clone(repo_url: str, target_dir: Path, sparse_dirs: List[str]) -> None:
    """稀疏克隆 MetaMetadata，只拉取指定目录。

    注意: git sparse-checkout (cone 模式) 只支持目录，不能混单个 .json 文件。
    图标同步只需 data/common，其中已包含合并后的 icon/landscape 等字段。
    """
    dirs = [p.rstrip("/") for p in sparse_dirs if not p.endswith(".json")]
    if not dirs:
        raise ValueError("sparse_dirs 至少需要一个目录路径")

    if target_dir.exists() and (target_dir / ".git").exists():
        log.info("更新已有 MetaMetadata 克隆: %s", target_dir)
        subprocess.run(["git", "-C", str(target_dir), "pull", "--ff-only"], check=True)
        return

    log.info("稀疏克隆 MetaMetadata -> %s (dirs: %s)", target_dir, dirs)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
            repo_url, str(target_dir),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(target_dir), "sparse-checkout", "set", *dirs],
        check=True,
    )


def fetch_json_raw(base_url: str, rel_path: str, cache_dir: Optional[Path] = None) -> Optional[dict]:
    url = f"{base_url.rstrip('/')}/{rel_path.lstrip('/')}"
    cache_file = cache_dir / rel_path.replace("/", "_") if cache_dir else None

    if cache_file and cache_file.exists():
        try:
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            if cache_file:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            return data
        except Exception as exc:
            log.warning("拉取 %s 失败 (第 %d 次): %s", rel_path, attempt + 1, exc)
            time.sleep(2)
    return None


def load_package_names_from_known(metadata_dir: Path) -> List[str]:
    names: set[str] = set()
    for rel in ("data/known_oculus_apps.json", "data/known_sidequest_apps.json"):
        path = metadata_dir / rel
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            apps = json.load(f)
        for app in apps:
            pkg = app.get("packageName")
            if pkg:
                names.add(pkg)
    return sorted(names)


def extract_icon_from_common(data: dict, package_name: str) -> Optional[AppIcon]:
    candidates: List[AppIcon] = []
    for field, _priority in sorted(ICON_FIELD_PRIORITY.items(), key=lambda x: x[1]):
        url = data.get(field)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            candidates.append(AppIcon(package_name, url, field, "common"))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: ICON_FIELD_PRIORITY.get(x.icon_type, 999))[0]


def collect_icons_from_local(metadata_dir: Path) -> List[AppIcon]:
    common_dir = metadata_dir / "data" / "common"
    if not common_dir.exists():
        raise FileNotFoundError(f"未找到 {common_dir}，请先克隆 MetaMetadata 或指定 --metadata-dir")

    icons: List[AppIcon] = []
    json_files = list(common_dir.glob("*.json"))
    log.info("扫描 %s (%d 个 JSON)", common_dir, len(json_files))

    for path in json_files:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            icon = extract_icon_from_common(data, path.stem)
            if icon:
                icons.append(icon)
        except Exception as exc:
            log.debug("跳过 %s: %s", path.name, exc)

    return icons


def collect_icons_from_remote(raw_base: str, cache_dir: Path) -> List[AppIcon]:
    """通过 raw.githubusercontent.com 按需拉取 common JSON。"""
    oculus_known = fetch_json_raw(raw_base, "data/known_oculus_apps.json", cache_dir) or []
    sidequest_known = fetch_json_raw(raw_base, "data/known_sidequest_apps.json", cache_dir) or []

    packages: set[str] = set()
    for app in oculus_known + sidequest_known:
        pkg = app.get("packageName")
        if pkg:
            packages.add(pkg)

    log.info("远程模式: 共 %d 个包名待拉取 common JSON", len(packages))
    icons: List[AppIcon] = []

    def fetch_one(pkg: str) -> Optional[AppIcon]:
        data = fetch_json_raw(raw_base, f"data/common/{pkg}.json", cache_dir)
        if not data:
            return None
        return extract_icon_from_common(data, pkg)

    with ThreadPoolExecutor(max_workers=DEFAULT_MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one, pkg): pkg for pkg in packages}
        done = 0
        for future in as_completed(futures):
            done += 1
            if done % 500 == 0:
                log.info("已拉取 JSON: %d/%d", done, len(packages))
            try:
                icon = future.result()
                if icon:
                    icons.append(icon)
            except Exception as exc:
                log.debug("拉取失败 %s: %s", futures[future], exc)

    return icons


def filter_icons(icons: List[AppIcon]) -> List[AppIcon]:
    by_package: Dict[str, AppIcon] = {}
    for icon in icons:
        url_lower = icon.icon_url.lower()
        if any(frag in url_lower for frag in SKIP_URL_FRAGMENTS):
            continue
        existing = by_package.get(icon.package_name)
        if not existing or ICON_FIELD_PRIORITY.get(icon.icon_type, 999) < ICON_FIELD_PRIORITY.get(existing.icon_type, 999):
            by_package[icon.package_name] = icon
    return list(by_package.values())


# ---------------------------------------------------------------------------
# Manifest（追踪 URL 变化，支持增量更新）
# ---------------------------------------------------------------------------

def load_manifest(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def needs_download(icon: AppIcon, icon_dir: Path, manifest: dict, force: bool) -> bool:
    if force:
        return True
    safe_name = safe_package_name(icon.package_name)
    icon_path = icon_dir / f"{safe_name}.jpg"
    entry = manifest.get(icon.package_name)
    if not icon_path.exists():
        return True
    if not entry:
        return True
    return entry.get("url_hash") != url_hash(icon.icon_url)


# ---------------------------------------------------------------------------
# 下载图标
# ---------------------------------------------------------------------------

def download_and_save_icon(icon: AppIcon, icon_dir: Path, manifest: dict, force: bool) -> bool:
    if not needs_download(icon, icon_dir, manifest, force):
        icon_stats["skipped"] += 1
        return True

    safe_name = safe_package_name(icon.package_name)
    icon_path = icon_dir / f"{safe_name}.jpg"
    is_update = icon_path.exists()

    headers = {
        "User-Agent": "QuestIconsSync/1.0",
        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    }

    try:
        time.sleep(0.05)
        resp = session.get(icon.icon_url, headers=headers, timeout=30, allow_redirects=True)
        resp.raise_for_status()

        if resp.content.startswith((b"<!DOCTYPE", b"<html")):
            icon_stats["failed"] += 1
            return False

        compressed = compress_image(resp.content)
        if not compressed or len(compressed) < 1024:
            icon_stats["failed"] += 1
            return False

        icon_dir.mkdir(parents=True, exist_ok=True)
        with open(icon_path, "wb") as f:
            f.write(compressed)

        manifest[icon.package_name] = {
            "url": icon.icon_url,
            "url_hash": url_hash(icon.icon_url),
            "icon_type": icon.icon_type,
            "size": len(compressed),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        if is_update:
            icon_stats["updated"] += 1
        else:
            icon_stats["downloaded"] += 1
        return True

    except Exception as exc:
        log.debug("下载失败 %s: %s", icon.package_name, exc)
        icon_stats["failed"] += 1
        return False


def download_icons(icons: List[AppIcon], icon_dir: Path, manifest_path: Path, max_workers: int, force: bool) -> dict:
    manifest = load_manifest(manifest_path)
    todo = [i for i in icons if needs_download(i, icon_dir, manifest, force)]
    log.info("需下载/更新: %d / 总计: %d", len(todo), len(icons))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(download_and_save_icon, icon, icon_dir, manifest, force) for icon in todo]
        for i, future in enumerate(as_completed(futures), 1):
            future.result()
            if i % 100 == 0:
                log.info("下载进度: %d/%d", i, len(todo))

    save_manifest(manifest_path, manifest)
    return manifest


# ---------------------------------------------------------------------------
# 上传到服务器
# ---------------------------------------------------------------------------

def upload_via_sftp(local_dir: Path, remote_dir: str) -> None:
    try:
        import paramiko
    except ImportError as exc:
        raise SystemExit("SFTP 上传需要 paramiko: pip install paramiko") from exc

    host = os.environ["SFTP_HOST"]
    port = int(os.environ.get("SFTP_PORT", "22"))
    user = os.environ["SFTP_USER"]
    password = os.environ.get("SFTP_PASSWORD")
    key_path = os.environ.get("SFTP_KEY_PATH")

    transport = paramiko.Transport((host, port))
    if key_path:
        pkey = paramiko.RSAKey.from_private_key_file(key_path)
        transport.connect(username=user, pkey=pkey)
    else:
        if not password:
            raise SystemExit("请设置 SFTP_PASSWORD 或 SFTP_KEY_PATH")
        transport.connect(username=user, password=password)

    sftp = paramiko.SFTPClient.from_transport(transport)
    assert sftp is not None

    def ensure_remote_dir(path: str) -> None:
        parts = path.strip("/").split("/")
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            try:
                sftp.stat(current)
            except IOError:
                sftp.mkdir(current)

    ensure_remote_dir(remote_dir.rstrip("/"))

    uploaded = 0
    for local_file in local_dir.glob("*.jpg"):
        remote_path = f"{remote_dir.rstrip('/')}/{local_file.name}"
        sftp.put(str(local_file), remote_path)
        uploaded += 1

    manifest_local = local_dir / DEFAULT_MANIFEST
    if manifest_local.exists():
        sftp.put(str(manifest_local), f"{remote_dir.rstrip('/')}/{DEFAULT_MANIFEST}")

    sftp.close()
    transport.close()
    log.info("SFTP 上传完成: %d 个 jpg + manifest", uploaded)


def deploy_to_path(local_dir: Path, deploy_path: str) -> None:
    """服务器本地部署：复制到 nginx 静态目录等。"""
    dest = Path(deploy_path)
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in local_dir.glob("*.jpg"):
        shutil.copy2(src, dest / src.name)
        copied += 1
    manifest_src = local_dir / DEFAULT_MANIFEST
    if manifest_src.exists():
        shutil.copy2(manifest_src, dest / DEFAULT_MANIFEST)
    log.info("已复制 %d 个图标到 %s", copied, dest)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Quest 图标自动同步")
    p.add_argument(
        "--mode",
        choices=("git", "remote", "local"),
        default=os.environ.get("SYNC_MODE", "git"),
        help="git=稀疏克隆MetaMetadata; remote=HTTP拉JSON; local=本地已有data目录",
    )
    p.add_argument("--metadata-dir", default=os.environ.get("METADATA_DIR", ".cache/metmetadata"))
    p.add_argument("--metadata-repo", default=os.environ.get("METADATA_REPO", DEFAULT_METADATA_REPO))
    p.add_argument("--metadata-raw", default=os.environ.get("METADATA_RAW", DEFAULT_METADATA_RAW))
    p.add_argument("--icon-dir", default=os.environ.get("ICON_DIR", DEFAULT_ICON_DIR))
    p.add_argument("--max-workers", type=int, default=int(os.environ.get("MAX_WORKERS", DEFAULT_MAX_WORKERS)))
    p.add_argument("--force", action="store_true", help="强制重新下载所有图标")
    p.add_argument("--no-upload", action="store_true", help="跳过上传步骤")
    p.add_argument("--upload-only", action="store_true", help="仅上传已有 icons 目录，不拉取数据")
    return p.parse_args()


def run_upload(icon_dir: Path) -> None:
    upload_method = os.environ.get("UPLOAD_METHOD", "").lower()
    if upload_method == "sftp":
        remote = os.environ.get("SFTP_REMOTE_DIR", "/var/www/icons")
        upload_via_sftp(icon_dir, remote)
    elif upload_method == "copy":
        deploy = os.environ.get("DEPLOY_PATH")
        if not deploy:
            raise SystemExit("UPLOAD_METHOD=copy 时需要设置 DEPLOY_PATH")
        deploy_to_path(icon_dir, deploy)
    elif upload_method:
        raise SystemExit(f"未知 UPLOAD_METHOD={upload_method}")
    else:
        log.info("未设置 UPLOAD_METHOD，跳过上传")


def main() -> int:
    args = parse_args()
    metadata_dir = Path(args.metadata_dir)
    icon_dir = Path(args.icon_dir)
    manifest_path = icon_dir / DEFAULT_MANIFEST

    log.info("=== Quest 图标同步 ===")
    log.info("模式: %s | 输出: %s", args.mode, icon_dir)

    start = time.time()

    if args.upload_only:
        if not args.no_upload:
            run_upload(icon_dir)
        log.info("仅上传模式完成")
        return 0

    if args.mode == "git":
        git_sparse_clone(
            args.metadata_repo,
            metadata_dir,
            ["data/common"],
        )
        icons = collect_icons_from_local(metadata_dir)
    elif args.mode == "local":
        icons = collect_icons_from_local(Path(args.metadata_dir))
    else:
        cache_dir = Path(".cache/json")
        icons = collect_icons_from_remote(args.metadata_raw, cache_dir)

    icons = filter_icons(icons)
    log.info("有效图标: %d 个", len(icons))

    if not icons:
        log.error("未找到任何图标，退出")
        return 1

    download_icons(icons, icon_dir, manifest_path, args.max_workers, args.force)

    if not args.no_upload:
        run_upload(icon_dir)

    elapsed = time.time() - start
    jpg_count = len(list(icon_dir.glob("*.jpg")))
    log.info(
        "完成 | 耗时 %.1fs | 新增 %d | 更新 %d | 跳过 %d | 失败 %d | 文件总数 %d",
        elapsed,
        icon_stats["downloaded"],
        icon_stats["updated"],
        icon_stats["skipped"],
        icon_stats["failed"],
        jpg_count,
    )
    return 0 if icon_stats["failed"] == 0 or icon_stats["downloaded"] + icon_stats["updated"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
