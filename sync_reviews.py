#!/usr/bin/env python3
"""
Quest 应用评论同步工具（本地测试 / GitHub Actions / 服务器 cron）

数据来源：Meta 商店公开 GraphQL（无需第三方 reviews.5698452.xyz 密钥）
App ID 来源：MetaMetadata oculus_public / QLoader / OculusDB
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_METADATA_RAW = "https://raw.githubusercontent.com/threethan/MetaMetadata/main"
DEFAULT_REVIEWS_DIR = "reviews"
DEFAULT_MANIFEST = "manifest.json"
DEFAULT_HMD_TYPE = "HOLLYWOOD"  # Meta Quest 商店页使用的平台枚举
DEFAULT_REVIEW_DOC_ID = "26526098833693347"  # MDCAppStoreAppPDPBelowFoldRootQuery（评分摘要）
DEFAULT_REVIEW_PAGE_DOC_ID = "9890550064386319"  # MDCAppStoreV2ParityAppPDPReviewListQuery（分页）
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_REVIEWS = 0  # 0 = 拉取全部可用评论
DEFAULT_SORT = "helpful"  # helpful | newest

SORT_TO_ORDERING = {
    "helpful": ["TOP"],
    "top": ["TOP"],
    "newest": ["MOST_RECENT"],
    "recent": ["MOST_RECENT"],
    "most_recent": ["MOST_RECENT"],
}

META_OCAPI = "https://www.meta.com/ocapi/graphql"
QLoader_URL = "https://qloader.5698452.xyz/api/v1/oculusgames/{package}"
OCULUSDB_ALLAPPS = "https://oculusdb.rui2015.me/api/v1/allapps"

session = requests.Session()
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50)
session.mount("http://", adapter)
session.mount("https://", adapter)
session.headers.update({"User-Agent": "QuestReviewsSync/1.0"})

stats = {"fetched": 0, "skipped": 0, "failed": 0, "no_id": 0}


@dataclass
class AppRef:
    package_name: str
    app_id: str
    name: str = ""
    source: str = ""


@dataclass
class Review:
    id: str
    score: Optional[float]
    review_title: Optional[str]
    review_description: Optional[str]
    date: Optional[int]
    review_helpful_count: Optional[int]
    author_display_name: Optional[str]
    author_alias: Optional[str]
    developer_response: Optional[dict]


# ---------------------------------------------------------------------------
# App ID 解析
# ---------------------------------------------------------------------------

def fetch_json(url: str, retries: int = 3) -> Optional[Any]:
    for i in range(retries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("GET %s 失败 (%d/%d): %s", url, i + 1, retries, exc)
            time.sleep(1.5)
    return None


def resolve_app_id_qloader(package: str) -> Optional[str]:
    data = fetch_json(QLoader_URL.format(package=package))
    if isinstance(data, dict):
        return str(data.get("id")) if data.get("id") else None
    return None


def resolve_app_id_oculus_public(raw_base: str, package: str) -> Optional[str]:
    data = fetch_json(f"{raw_base.rstrip('/')}/data/oculus_public/{package}.json")
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    return None


def resolve_app_id_oculusdb(package: str, cache: Dict[str, str]) -> Optional[str]:
    if package in cache:
        return cache[package]
    return None


def load_oculusdb_package_map() -> Dict[str, str]:
    log.info("拉取 OculusDB allapps 建立包名映射...")
    data = fetch_json(OCULUSDB_ALLAPPS)
    if not isinstance(data, list):
        return {}
    mapping = {}
    for app in data:
        pkg = app.get("packageName")
        app_id = app.get("id")
        if pkg and app_id and "rift" not in str(pkg).lower():
            mapping[pkg] = str(app_id)
    log.info("OculusDB 映射: %d 个包", len(mapping))
    return mapping


def load_apps_from_known(raw_base: str) -> List[AppRef]:
    apps: List[AppRef] = []
    for rel in ("data/known_oculus_apps.json", "data/known_sidequest_apps.json"):
        data = fetch_json(f"{raw_base.rstrip('/')}/{rel}")
        if not isinstance(data, list):
            continue
        for item in data:
            pkg = item.get("packageName")
            app_id = item.get("id")
            if pkg and app_id:
                apps.append(AppRef(pkg, str(app_id), item.get("appName", ""), "known"))
    return apps


def resolve_app_ref(package: str, raw_base: str, odb_map: Dict[str, str]) -> Optional[AppRef]:
    # 1) oculus_public（MetaMetadata 浏览器公开数据，含 id）
    app_id = resolve_app_id_oculus_public(raw_base, package)
    if app_id:
        return AppRef(package, app_id, source="oculus_public")

    # 2) known lists 已在批量模式处理

    # 3) QLoader（yaas 同款，无需密钥）
    app_id = resolve_app_id_qloader(package)
    if app_id:
        return AppRef(package, app_id, source="qloader")

    # 4) OculusDB
    app_id = resolve_app_id_oculusdb(package, odb_map)
    if app_id:
        return AppRef(package, app_id, source="oculusdb")

    return None


# ---------------------------------------------------------------------------
# Meta 评论 GraphQL
# ---------------------------------------------------------------------------

def parse_meta_json(text: str) -> dict:
    """Meta ocapi 返回流式多行 JSON（@defer），需合并含 user_reviews2 的片段。"""
    merged: dict = {"data": {}}
    found_reviews = False

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue

        # 单块完整响应
        if isinstance(chunk.get("data"), dict) and _find_node_with_reviews(chunk["data"]):
            return chunk

        # @defer 分片：label + path + data
        data = chunk.get("data")
        if isinstance(data, dict) and "user_reviews2" in json.dumps(data):
            merged["data"]["app_store_item"] = _deep_merge(
                merged["data"].get("app_store_item", {}),
                data if "user_reviews2" in data else _unwrap_path_data(chunk, data),
            )
            found_reviews = True

    if found_reviews:
        return merged

    # 回退：仅解析第一行
    first = text.splitlines()[0].strip() if text.strip() else "{}"
    try:
        return json.loads(first)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text)
        return obj


def _unwrap_path_data(chunk: dict, data: dict) -> dict:
    """defer 分片的 data 通常就是 app 节点。"""
    if "user_reviews2" in data:
        return data
    for v in data.values():
        if isinstance(v, dict) and "user_reviews2" in v:
            return v
    return data


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _post_meta_graphql(
    doc_id: str,
    variables: dict,
    friendly_name: str,
) -> str:
    data = {
        "lsd": "AVqMsnyvi0U",
        "doc_id": doc_id,
        "variables": json.dumps(variables),
        "fb_api_req_friendly_name": friendly_name,
    }
    headers = {
        "X-FB-LSD": "AVqMsnyvi0U",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
    }
    r = session.post(META_OCAPI, data=data, headers=headers, timeout=60)
    r.raise_for_status()
    return r.text


def parse_pagination_response(text: str) -> Optional[dict]:
    """解析 MDCAppStoreV2ParityAppPDPReviewListQuery 响应（通常为单行 JSON）。"""
    text = text.strip()
    if not text:
        return None
    try:
        payload = json.loads(text.splitlines()[0])
    except json.JSONDecodeError:
        payload = parse_meta_json(text)
    node = (payload.get("data") or {}).get("node")
    if isinstance(node, dict) and "user_reviews2" in node:
        return node
    return _find_node_with_reviews(payload.get("data", {}))


def parse_review_edges(edges: List[dict]) -> List[dict]:
    reviews: List[dict] = []
    seen: set[str] = set()
    for edge in edges:
        n = edge.get("node") or {}
        rid = str(n.get("id", ""))
        if not rid or rid in seen:
            continue
        seen.add(rid)
        author = n.get("author") or {}
        dev = n.get("developer_response")
        reviews.append(
            asdict(
                Review(
                    id=rid,
                    score=n.get("score"),
                    review_title=n.get("review_title"),
                    review_description=n.get("review_description"),
                    date=n.get("date"),
                    review_helpful_count=n.get("review_helpful_count"),
                    author_display_name=author.get("name") or author.get("alias"),
                    author_alias=author.get("alias"),
                    developer_response={
                        "body": dev.get("body"),
                        "date": dev.get("date"),
                    }
                    if isinstance(dev, dict)
                    else None,
                )
            )
        )
    return reviews


def fetch_reviews_page(
    app_id: str,
    *,
    page_size: int,
    cursor: Optional[str],
    ordering: List[str],
    page_doc_id: str = DEFAULT_REVIEW_PAGE_DOC_ID,
) -> dict:
    variables = {
        "id": app_id,
        "count": page_size,
        "cursor": cursor,
        "ordering": ordering,
        "ratingScores": [1, 2, 3, 4, 5],
    }
    text = _post_meta_graphql(
        page_doc_id,
        variables,
        "MDCAppStoreV2ParityAppPDPReviewListQuery",
    )
    node = parse_pagination_response(text)
    if not node:
        raise RuntimeError("分页响应中未找到 user_reviews2")

    ur = node.get("user_reviews2") or {}
    edges = ur.get("edges") or []
    page_info = ur.get("page_info") or {}
    next_cursor = edges[-1].get("cursor") if edges else None

    return {
        "node": node,
        "reviews": parse_review_edges(edges),
        "has_next": bool(page_info.get("has_next_page")),
        "end_cursor": page_info.get("end_cursor"),
        "next_cursor": next_cursor,
    }


def fetch_rating_summary(
    app_id: str,
    *,
    hmd_type: str = DEFAULT_HMD_TYPE,
    doc_id: str = DEFAULT_REVIEW_DOC_ID,
) -> dict:
    """拉取评分摘要（不含分页评论）。"""
    variables = {
        "itemId": app_id,
        "hmdType": hmd_type,
        "__relay_internal__pv__MDCAppStorFortheMetadataLineEnablerelayprovider": False,
        "__relay_internal__pv__MDCAppStoreGenAIReviewSummaryEnabledrelayprovider": False,
    }
    text = _post_meta_graphql(
        doc_id,
        variables,
        "MDCAppStoreAppPDPBelowFoldRootQuery",
    )
    payload = parse_meta_json(text)
    if "errors" in payload and not payload.get("data"):
        raise RuntimeError(json.dumps(payload["errors"], ensure_ascii=False)[:500])
    node = _find_node_with_reviews(payload.get("data", {}))
    if not node and isinstance(payload.get("data"), dict):
        node = payload["data"].get("app_store_item")
    if not node:
        return {}
    return {
        "name": node.get("display_name"),
        "rating_average": node.get("quality_rating_score") or node.get("quality_rating_aggregate"),
        "rating_count": node.get("quality_rating_count"),
        "review_count": node.get("quality_review_count"),
    }


def fetch_reviews_meta(
    app_id: str,
    *,
    hmd_type: str = DEFAULT_HMD_TYPE,
    doc_id: str = DEFAULT_REVIEW_DOC_ID,
    page_doc_id: str = DEFAULT_REVIEW_PAGE_DOC_ID,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_reviews: int = DEFAULT_MAX_REVIEWS,
    sort: str = DEFAULT_SORT,
    request_delay: float = 0.25,
) -> dict:
    ordering = SORT_TO_ORDERING.get(sort.lower(), SORT_TO_ORDERING[DEFAULT_SORT])

    summary = fetch_rating_summary(app_id, hmd_type=hmd_type, doc_id=doc_id)

    all_reviews: List[dict] = []
    cursor: Optional[str] = None
    page_num = 0
    last_node: dict = {}

    while True:
        page_num += 1
        remaining = None
        if max_reviews > 0:
            remaining = max_reviews - len(all_reviews)
            if remaining <= 0:
                break
        fetch_size = min(page_size, remaining) if remaining else page_size

        page = fetch_reviews_page(
            app_id,
            page_size=fetch_size,
            cursor=cursor,
            ordering=ordering,
            page_doc_id=page_doc_id,
        )
        last_node = page["node"]
        batch = page["reviews"]
        if not batch:
            break

        all_reviews.extend(batch)
        log.debug(
            "app %s 第 %d 页 +%d 条，累计 %d",
            app_id, page_num, len(batch), len(all_reviews),
        )

        if max_reviews > 0 and len(all_reviews) >= max_reviews:
            all_reviews = all_reviews[:max_reviews]
            break
        if not page["has_next"]:
            break

        cursor = page["next_cursor"]
        if not cursor:
            break
        time.sleep(request_delay)

    return {
        "app_id": app_id,
        "name": last_node.get("display_name") or summary.get("name"),
        "rating_average": summary.get("rating_average"),
        "rating_count": summary.get("rating_count"),
        "review_count": last_node.get("quality_review_count") or summary.get("review_count"),
        "reviews": all_reviews,
        "total": len(all_reviews),
        "sort": sort,
        "ordering": ordering,
        "pages_fetched": page_num,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _find_node_with_reviews(obj: Any) -> Optional[dict]:
    if isinstance(obj, dict):
        if "user_reviews2" in obj:
            return obj
        for v in obj.values():
            found = _find_node_with_reviews(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_node_with_reviews(item)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def load_manifest(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def sync_one_app(
    app: AppRef,
    out_dir: Path,
    manifest: dict,
    *,
    force: bool,
    hmd_type: str,
    doc_id: str,
    page_doc_id: str,
    page_size: int,
    max_reviews: int,
    sort: str,
    request_delay: float,
) -> bool:
    out_file = out_dir / f"{safe_name(app.package_name)}.json"
    if not force and out_file.exists():
        stats["skipped"] += 1
        return True

    try:
        result = fetch_reviews_meta(
            app.app_id,
            hmd_type=hmd_type,
            doc_id=doc_id,
            page_doc_id=page_doc_id,
            page_size=page_size,
            max_reviews=max_reviews,
            sort=sort,
            request_delay=request_delay,
        )
        result["package_name"] = app.package_name
        result["app_id"] = app.app_id
        result["id_source"] = app.source

        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        manifest[app.package_name] = {
            "app_id": app.app_id,
            "review_count": result.get("total", 0),
            "store_review_count": result.get("review_count"),
            "rating_average": result.get("rating_average"),
            "sort": sort,
            "pages_fetched": result.get("pages_fetched"),
            "updated_at": result.get("fetched_at"),
        }
        stats["fetched"] += 1
        return True
    except Exception as exc:
        log.warning("评论拉取失败 %s (%s): %s", app.package_name, app.app_id, exc)
        stats["failed"] += 1
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Quest 应用评论同步")
    p.add_argument("--package", help="仅测试单个包名，如 com.beatgames.beatsaber")
    p.add_argument("--app-id", help="直接指定 Meta app id（与 --package 联用）")
    p.add_argument("--metadata-raw", default=os.environ.get("METADATA_RAW", DEFAULT_METADATA_RAW))
    p.add_argument("--reviews-dir", default=os.environ.get("REVIEWS_DIR", DEFAULT_REVIEWS_DIR))
    p.add_argument(
        "--page-size", type=int,
        default=int(os.environ.get("REVIEW_PAGE_SIZE", DEFAULT_PAGE_SIZE)),
        help="每页拉取条数",
    )
    p.add_argument(
        "--max-reviews", type=int,
        default=int(os.environ.get("MAX_REVIEWS", DEFAULT_MAX_REVIEWS)),
        help="每个应用最多拉取条数，0=全部",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="同 --max-reviews（兼容旧参数）",
    )
    p.add_argument(
        "--sort", choices=tuple(SORT_TO_ORDERING.keys()),
        default=os.environ.get("REVIEW_SORT", DEFAULT_SORT),
        help="helpful=最有帮助 | newest=最新",
    )
    p.add_argument("--max-workers", type=int, default=int(os.environ.get("MAX_WORKERS", "4")))
    p.add_argument("--max-apps", type=int, default=int(os.environ.get("MAX_APPS", "0")), help="0=不限制")
    p.add_argument("--force", action="store_true")
    p.add_argument("--hmd-type", default=os.environ.get("HMD_TYPE", DEFAULT_HMD_TYPE))
    p.add_argument("--doc-id", default=os.environ.get("REVIEW_DOC_ID", DEFAULT_REVIEW_DOC_ID))
    p.add_argument(
        "--page-doc-id",
        default=os.environ.get("REVIEW_PAGE_DOC_ID", DEFAULT_REVIEW_PAGE_DOC_ID),
    )
    p.add_argument("--delay", type=float, default=float(os.environ.get("REQUEST_DELAY", "0.3")))
    return p.parse_args()


def _resolve_max_reviews(args: argparse.Namespace) -> int:
    if args.limit is not None:
        return args.limit
    return args.max_reviews


def main() -> int:
    args = parse_args()
    out_dir = Path(args.reviews_dir)
    manifest_path = out_dir / DEFAULT_MANIFEST
    manifest = load_manifest(manifest_path)

    max_reviews = _resolve_max_reviews(args)

    log.info("=== Quest 评论同步 ===")
    log.info(
        "输出: %s | 排序: %s | 每页: %d | 上限: %s",
        out_dir, args.sort, args.page_size,
        "全部" if max_reviews <= 0 else str(max_reviews),
    )

    sync_kwargs = dict(
        hmd_type=args.hmd_type,
        doc_id=args.doc_id,
        page_doc_id=args.page_doc_id,
        page_size=args.page_size,
        max_reviews=max_reviews,
        sort=args.sort,
        request_delay=args.delay,
    )

    # 单包测试模式
    if args.package:
        app_id = args.app_id
        source = "cli"
        if not app_id:
            odb_map = load_oculusdb_package_map()
            ref = resolve_app_ref(args.package, args.metadata_raw, odb_map)
            if not ref:
                log.error("无法解析 app id: %s", args.package)
                return 1
            app_id, source = ref.app_id, ref.source
        app = AppRef(args.package, app_id, source=source)
        ok = sync_one_app(app, out_dir, manifest, force=True, **sync_kwargs)
        save_manifest(manifest_path, manifest)
        if ok:
            log.info("成功 -> %s", out_dir / f"{safe_name(args.package)}.json")
            with open(out_dir / f"{safe_name(args.package)}.json", encoding="utf-8") as f:
                preview = json.load(f)
            log.info(
                "评论: %d 条 | 商店总数: %s | 评分: %s | 页数: %s",
                len(preview.get("reviews", [])),
                preview.get("review_count"),
                preview.get("rating_average"),
                preview.get("pages_fetched"),
            )
        return 0 if ok else 1

    # 批量：从 MetaMetadata known 列表
    apps = load_apps_from_known(args.metadata_raw)
    if not apps:
        log.error("未能加载 known_oculus_apps / known_sidequest_apps")
        return 1

    if args.max_apps > 0:
        apps = apps[: args.max_apps]

    log.info("批量同步 %d 个应用（请控制速率，Meta 可能限流）", len(apps))

    def worker(app: AppRef) -> None:
        time.sleep(args.delay)
        sync_one_app(app, out_dir, manifest, force=args.force, **sync_kwargs)

    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = [pool.submit(worker, app) for app in apps]
        for i, fut in enumerate(as_completed(futures), 1):
            fut.result()
            if i % 50 == 0:
                save_manifest(manifest_path, manifest)
                log.info("进度 %d/%d", i, len(apps))

    save_manifest(manifest_path, manifest)
    log.info(
        "完成 | 新增/更新 %d | 跳过 %d | 失败 %d",
        stats["fetched"], stats["skipped"], stats["failed"],
    )
    return 0 if stats["failed"] == 0 or stats["fetched"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
