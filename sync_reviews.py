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
import random
import re
import sys
import threading
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

stats = {
    "fetched": 0, "skipped": 0, "failed": 0, "no_id": 0,
    "retried": 0, "rate_limited": 0, "no_reviews": 0, "unavailable": 0,
}

STATUS_OK = "ok"
STATUS_NO_REVIEWS = "no_reviews"
STATUS_UNAVAILABLE = "unavailable"

_UNAVAILABLE_HINTS = (
    "not found", "not_found", "invalid", "unavailable", "does not exist",
    "cannot find", "no longer available", "removed from", "delisted",
    "deprecated", "entity not found", "application_not_found", "app_not_found",
    "must not be null", "non-null value",
)
_TRANSIENT_HINTS = ("rate limit", "too many", "timeout", "temporarily", "503", "502")


class AppUnavailableError(Exception):
    """应用已下架、不存在或 Meta 商店无此条目。"""


class TransientReviewError(Exception):
    """网络/限流等可重试错误。"""


class MetaRateLimiter:
    """全进程共享的 Meta API 限速器（线程安全）。"""

    def __init__(self, min_interval: float, max_interval: float = 25.0):
        self._base_interval = min_interval
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._lock = threading.Lock()
        self._last_at = 0.0
        self._cooldown_until = 0.0
        self._consecutive_429 = 0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._cooldown_until:
                sleep_for = self._cooldown_until - now
            else:
                sleep_for = max(0.0, self._min_interval - (now - self._last_at))
            jitter = random.uniform(0.0, self._min_interval * 0.2)
        sleep_for += jitter
        if sleep_for > 0:
            time.sleep(sleep_for)
        with self._lock:
            self._last_at = time.monotonic()

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._consecutive_429 += 1
            until = time.monotonic() + seconds
            self._cooldown_until = max(self._cooldown_until, until)
            self._min_interval = min(
                max(self._base_interval, self._min_interval * 1.5),
                self._max_interval,
            )
            interval = self._min_interval
            streak = self._consecutive_429
        stats["rate_limited"] += 1
        log.warning(
            "Meta 429 冷却 %.1fs（连续 %d 次），请求间隔 -> %.2fs",
            seconds, streak, interval,
        )

    def on_success(self) -> None:
        with self._lock:
            if self._consecutive_429 > 0:
                self._consecutive_429 -= 1
            if self._min_interval > self._base_interval:
                self._min_interval = max(
                    self._base_interval,
                    self._min_interval * 0.985,
                )


_rate_limiter: Optional["MetaRateLimiter"] = None


def get_rate_limiter() -> MetaRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        interval = float(os.environ.get("META_MIN_INTERVAL", "1.2"))
        _rate_limiter = MetaRateLimiter(interval)
    return _rate_limiter


def init_rate_limiter(min_interval: float, max_interval: float = 25.0) -> None:
    global _rate_limiter
    _rate_limiter = MetaRateLimiter(min_interval, max_interval)


def apply_chain_start_delay() -> None:
    """链式 workflow 启动前等待，避免连续批次触发 Meta 429。"""
    chain_depth = int(os.environ.get("CHAIN_DEPTH", "0") or "0")
    per_step = float(os.environ.get("CHAIN_START_DELAY", "45"))
    max_wait = float(os.environ.get("CHAIN_START_DELAY_MAX", "600"))
    if chain_depth <= 0 or per_step <= 0:
        return
    wait = min(chain_depth * per_step, max_wait)
    log.info(
        "链式批次 depth=%d，启动前等待 %.0fs（降低 Meta 429 风险）",
        chain_depth, wait,
    )
    time.sleep(wait)


def _graphql_error_blob(errors: Any) -> str:
    try:
        return json.dumps(errors, ensure_ascii=False).lower()
    except Exception:
        return str(errors).lower()


def classify_graphql_errors(errors: Any) -> str:
    blob = _graphql_error_blob(errors)
    if any(h in blob for h in _UNAVAILABLE_HINTS):
        return STATUS_UNAVAILABLE
    if any(h in blob for h in _TRANSIENT_HINTS):
        return "transient"
    return "error"


def _node_is_null(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("node", "app_store_item"):
        if key in data and data[key] is None:
            return True
    return False


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_review_payload(
    app_id: str,
    *,
    status: str,
    status_message: Optional[str] = None,
    package_name: Optional[str] = None,
    id_source: Optional[str] = None,
    name: Optional[str] = None,
    rating_average: Optional[float] = None,
    rating_count: Optional[int] = None,
    review_count: Optional[int] = None,
    reviews: Optional[List[dict]] = None,
    sort: Optional[str] = None,
    ordering: Optional[List[str]] = None,
    pages_fetched: int = 0,
) -> dict:
    review_list = reviews or []
    return {
        "app_id": app_id,
        "package_name": package_name,
        "id_source": id_source,
        "status": status,
        "status_message": status_message,
        "name": name,
        "rating_average": rating_average,
        "rating_count": rating_count,
        "review_count": review_count,
        "reviews": review_list,
        "total": len(review_list),
        "sort": sort,
        "ordering": ordering,
        "pages_fetched": pages_fetched,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _manifest_entry(result: dict, sort: str) -> dict:
    return {
        "app_id": result.get("app_id"),
        "status": result.get("status", STATUS_OK),
        "review_count": result.get("total", 0),
        "store_review_count": result.get("review_count"),
        "rating_average": result.get("rating_average"),
        "sort": sort,
        "pages_fetched": result.get("pages_fetched"),
        "updated_at": result.get("fetched_at"),
    }


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


def save_review_result(
    app: AppRef,
    out_dir: Path,
    manifest: dict,
    result: dict,
    sort: str,
    stat_key: str = "fetched",
) -> None:
    out_file = out_dir / f"{safe_name(app.package_name)}.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    manifest[app.package_name] = _manifest_entry(result, sort)
    stats[stat_key] += 1


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
    seen: set[str] = set()
    for rel in ("data/known_oculus_apps.json", "data/known_sidequest_apps.json"):
        data = fetch_json(f"{raw_base.rstrip('/')}/{rel}")
        if not isinstance(data, list):
            continue
        for item in data:
            pkg = item.get("packageName")
            app_id = item.get("id")
            if pkg and app_id and pkg not in seen:
                seen.add(pkg)
                apps.append(AppRef(pkg, str(app_id), item.get("appName", ""), "known"))
    return apps


def load_apps_from_icons(icons_dir: Path, known_apps: List[AppRef]) -> List[AppRef]:
    """仅同步仓库里已有图标的应用（与 icons/ 目录对齐）。"""
    if not icons_dir.is_dir():
        log.warning("图标目录不存在: %s", icons_dir)
        return known_apps
    packages = sorted(p.stem for p in icons_dir.glob("*.jpg"))
    by_pkg = {a.package_name: a for a in known_apps}
    apps = [by_pkg[p] for p in packages if p in by_pkg]
    missing = len(packages) - len(apps)
    log.info(
        "按 icons/ 过滤: %d 个包名, 匹配 known 列表 %d 个, 未匹配 %d 个",
        len(packages), len(apps), missing,
    )
    return apps


def filter_pending_apps(
    apps: List[AppRef],
    out_dir: Path,
    *,
    only_missing: bool,
    force: bool,
) -> List[AppRef]:
    if force or not only_missing:
        return apps
    return [
        app for app in apps
        if not (out_dir / f"{safe_name(app.package_name)}.json").exists()
    ]


def select_apps_for_batch(
    apps: List[AppRef],
    out_dir: Path,
    *,
    max_apps: int,
    only_missing: bool,
    force: bool,
    log_pending: bool = True,
) -> List[AppRef]:
    """选取本批次要同步的应用。默认跳过已有 JSON，逐批追平全库。"""
    pending = filter_pending_apps(apps, out_dir, only_missing=only_missing, force=force)
    if log_pending and only_missing and not force:
        log.info(
            "待同步 %d / 总计 %d 个应用（已有 JSON 的跳过）",
            len(pending), len(apps),
        )
    selected = pending if (only_missing and not force) else apps
    if max_apps > 0:
        selected = selected[:max_apps]
    return selected


def resolve_batch_app_pool(args: argparse.Namespace) -> List[AppRef]:
    apps = load_apps_from_known(args.metadata_raw)
    if not apps:
        return []
    if args.from_icons:
        apps = load_apps_from_icons(Path(args.icons_dir), apps)
    return apps


def emit_github_output(**kwargs: Any) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in kwargs.items():
            f.write(f"{key}={value}\n")


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


def _parse_retry_after(resp: requests.Response) -> float:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return 0.0
    try:
        return max(float(raw), 1.0)
    except ValueError:
        return 60.0


def _post_meta_graphql(
    doc_id: str,
    variables: dict,
    friendly_name: str,
    *,
    max_retries: Optional[int] = None,
) -> str:
    if max_retries is None:
        max_retries = int(os.environ.get("HTTP_RETRIES", "8"))

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
    limiter = get_rate_limiter()
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries):
        limiter.wait()
        try:
            r = session.post(META_OCAPI, data=data, headers=headers, timeout=60)
        except requests.RequestException as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30) + random.uniform(0, 1)
            log.warning(
                "Meta 网络错误 (%d/%d): %s，%.1fs 后重试",
                attempt + 1, max_retries, exc, wait,
            )
            time.sleep(wait)
            continue

        if r.status_code == 429:
            wait = max(
                _parse_retry_after(r),
                20 * (1.8 ** min(attempt, 6)),
            )
            wait += random.uniform(2, 6)
            wait = min(wait, 300.0)
            limiter.penalize(wait)
            time.sleep(wait)
            last_exc = requests.HTTPError("429 Too Many Requests", response=r)
            continue

        if r.status_code in (502, 503, 504):
            wait = min(2 ** attempt, 30) + random.uniform(0, 1)
            log.warning(
                "Meta %d (%d/%d)，%.1fs 后重试",
                r.status_code, attempt + 1, max_retries, wait,
            )
            time.sleep(wait)
            last_exc = requests.HTTPError(f"{r.status_code} Server Error", response=r)
            continue

        try:
            r.raise_for_status()
        except requests.HTTPError as exc:
            last_exc = exc
            if attempt + 1 >= max_retries:
                raise
            wait = min(2 ** attempt, 15) + random.uniform(0, 1)
            time.sleep(wait)
            continue

        limiter.on_success()
        return r.text

    if last_exc:
        if isinstance(last_exc, requests.HTTPError) and last_exc.response is not None:
            if last_exc.response.status_code == 429:
                raise TransientReviewError("Meta 429 Too Many Requests") from last_exc
        raise last_exc
    raise TransientReviewError("Meta GraphQL 请求重试耗尽")


def parse_pagination_response(text: str) -> tuple[Optional[dict], dict]:
    """返回 (node, raw_payload)。"""
    text = text.strip()
    if not text:
        return None, {}
    try:
        payload = json.loads(text.splitlines()[0])
    except json.JSONDecodeError:
        payload = parse_meta_json(text)
    data = payload.get("data") or {}
    if _node_is_null(data):
        return None, payload
    node = data.get("node")
    if isinstance(node, dict) and "user_reviews2" in node:
        return node, payload
    found = _find_node_with_reviews(data)
    return found, payload


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
    node, payload = parse_pagination_response(text)
    if not node:
        errors = payload.get("errors") or []
        if errors:
            kind = classify_graphql_errors(errors)
            if kind == STATUS_UNAVAILABLE:
                raise AppUnavailableError(_graphql_error_blob(errors)[:240])
            if kind == "transient":
                raise TransientReviewError(_graphql_error_blob(errors)[:240])
        if _node_is_null(payload.get("data") or {}):
            raise AppUnavailableError("商店分页 node 为空，应用可能已下架")
        raise AppUnavailableError("分页响应中未找到 user_reviews2")

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
    errors = payload.get("errors") or []
    data = payload.get("data") or {}

    if errors:
        kind = classify_graphql_errors(errors)
        if kind == STATUS_UNAVAILABLE or _node_is_null(data):
            raise AppUnavailableError(_graphql_error_blob(errors)[:240])
        if kind == "transient":
            raise TransientReviewError(_graphql_error_blob(errors)[:240])
        if not data:
            raise RuntimeError(_graphql_error_blob(errors)[:500])

    if _node_is_null(data):
        raise AppUnavailableError("商店摘要 node 为空，应用可能已下架")

    node = _find_node_with_reviews(data)
    if not node and isinstance(data, dict):
        node = data.get("app_store_item")
    if not node:
        return {
            "available": False,
            "name": None,
            "rating_average": None,
            "rating_count": None,
            "review_count": None,
        }

    store_review_count = _coerce_int(
        node.get("quality_review_count") or node.get("quality_rating_count")
    )
    return {
        "available": True,
        "name": node.get("display_name"),
        "rating_average": node.get("quality_rating_score") or node.get("quality_rating_aggregate"),
        "rating_count": _coerce_int(node.get("quality_rating_count")),
        "review_count": store_review_count,
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

    if summary.get("available") is False:
        raise AppUnavailableError("商店无此应用条目")

    store_review_count = summary.get("review_count")
    if store_review_count == 0:
        return build_review_payload(
            app_id,
            status=STATUS_NO_REVIEWS,
            status_message="商店评论数为 0",
            name=summary.get("name"),
            rating_average=summary.get("rating_average"),
            rating_count=summary.get("rating_count"),
            review_count=0,
            reviews=[],
            sort=sort,
            ordering=ordering,
            pages_fetched=0,
        )

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

    status = STATUS_OK if all_reviews else STATUS_NO_REVIEWS
    message = None if all_reviews else "未能拉取到评论正文"
    return build_review_payload(
        app_id,
        status=status,
        status_message=message,
        name=last_node.get("display_name") or summary.get("name"),
        rating_average=summary.get("rating_average"),
        rating_count=summary.get("rating_count"),
        review_count=last_node.get("quality_review_count") or summary.get("review_count"),
        reviews=all_reviews,
        sort=sort,
        ordering=ordering,
        pages_fetched=page_num,
    )


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
    record_failure: bool = True,
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

        status = result.get("status", STATUS_OK)
        if status == STATUS_NO_REVIEWS:
            log.info("无评论 %s (%s)", app.package_name, app.app_id)
            save_review_result(app, out_dir, manifest, result, sort, stat_key="no_reviews")
        else:
            save_review_result(app, out_dir, manifest, result, sort, stat_key="fetched")
        return True

    except AppUnavailableError as exc:
        log.info("不可用/已下架 %s (%s): %s", app.package_name, app.app_id, exc)
        result = build_review_payload(
            app.app_id,
            status=STATUS_UNAVAILABLE,
            status_message=str(exc)[:500],
            package_name=app.package_name,
            id_source=app.source,
            name=app.name or None,
            reviews=[],
            sort=sort,
            ordering=SORT_TO_ORDERING.get(sort.lower(), SORT_TO_ORDERING[DEFAULT_SORT]),
            pages_fetched=0,
        )
        save_review_result(app, out_dir, manifest, result, sort, stat_key="unavailable")
        return True

    except TransientReviewError as exc:
        log.warning("临时失败 %s (%s): %s", app.package_name, app.app_id, exc)
        if record_failure:
            stats["failed"] += 1
        return False

    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            log.warning("限流失败 %s (%s): %s", app.package_name, app.app_id, exc)
            if record_failure:
                stats["failed"] += 1
            return False
        log.warning("HTTP 失败 %s (%s): %s", app.package_name, app.app_id, exc)
        if record_failure:
            stats["failed"] += 1
        return False

    except Exception as exc:
        log.warning("评论拉取失败 %s (%s): %s", app.package_name, app.app_id, exc)
        if record_failure:
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
    p.add_argument("--max-workers", type=int, default=int(os.environ.get("MAX_WORKERS", "1")))
    p.add_argument("--max-apps", type=int, default=int(os.environ.get("MAX_APPS", "0")), help="0=不限制")
    p.add_argument(
        "--only-missing", action="store_true",
        default=os.environ.get("ONLY_MISSING", "").lower() in ("1", "true", "yes"),
        help="仅同步尚无 JSON 的应用（逐批追平，默认 CI 开启）",
    )
    p.add_argument(
        "--no-only-missing", action="store_false", dest="only_missing",
        help="关闭 only-missing，按列表前 N 个处理（可能大量 skip）",
    )
    p.add_argument(
        "--from-icons", action="store_true",
        default=os.environ.get("FROM_ICONS", "").lower() in ("1", "true", "yes"),
        help="仅处理 icons/ 里已有图标的包名",
    )
    p.add_argument(
        "--icons-dir",
        default=os.environ.get("ICON_DIR", "icons"),
        help="配合 --from-icons 使用",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--hmd-type", default=os.environ.get("HMD_TYPE", DEFAULT_HMD_TYPE))
    p.add_argument("--doc-id", default=os.environ.get("REVIEW_DOC_ID", DEFAULT_REVIEW_DOC_ID))
    p.add_argument(
        "--page-doc-id",
        default=os.environ.get("REVIEW_PAGE_DOC_ID", DEFAULT_REVIEW_PAGE_DOC_ID),
    )
    p.add_argument(
        "--delay", type=float,
        default=float(os.environ.get("REQUEST_DELAY", "2.5")),
        help="同一应用分页评论请求之间的额外间隔（秒）",
    )
    p.add_argument(
        "--inter-app-delay", type=float,
        default=float(os.environ.get("INTER_APP_DELAY", "2.0")),
        help="每个应用处理完成后的额外间隔（秒）",
    )
    p.add_argument(
        "--meta-min-interval", type=float,
        default=float(os.environ.get("META_MIN_INTERVAL", "3.5")),
        help="任意两次 Meta GraphQL 请求的最小间隔（秒）",
    )
    p.add_argument(
        "--meta-max-interval", type=float,
        default=float(os.environ.get("META_MAX_INTERVAL", "25")),
        help="429 后自适应间隔的上限（秒）",
    )
    p.add_argument(
        "--http-retries", type=int,
        default=int(os.environ.get("HTTP_RETRIES", "8")),
        help="429/5xx 最大重试次数",
    )
    p.add_argument(
        "--retry-failed", action="store_true",
        default=os.environ.get("RETRY_FAILED", "1").lower() in ("1", "true", "yes"),
        help="批次结束后对失败应用再试一轮",
    )
    p.add_argument(
        "--no-retry-failed", action="store_false", dest="retry_failed",
    )
    p.add_argument(
        "--retry-cooldown", type=float,
        default=float(os.environ.get("RETRY_COOLDOWN", "300")),
        help="失败重试前的冷却时间（秒）",
    )
    p.add_argument(
        "--rate-limit-extra-cooldown", type=float,
        default=float(os.environ.get("RATE_LIMIT_EXTRA_COOLDOWN", "180")),
        help="本批 429 次数较多时，重试前额外冷却（秒）",
    )
    p.add_argument(
        "--count-pending", action="store_true",
        help="仅输出待同步应用数量（供 CI 判断是否继续）",
    )
    return p.parse_args()


def _resolve_max_reviews(args: argparse.Namespace) -> int:
    if args.limit is not None:
        return args.limit
    return args.max_reviews


def main() -> int:
    args = parse_args()
    os.environ["HTTP_RETRIES"] = str(args.http_retries)
    init_rate_limiter(args.meta_min_interval, args.meta_max_interval)
    apply_chain_start_delay()

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

    app_pool = resolve_batch_app_pool(args)
    if not app_pool and not args.package:
        log.error("未能加载 known_oculus_apps / known_sidequest_apps")
        return 1

    if args.count_pending:
        pending = len(filter_pending_apps(
            app_pool, out_dir, only_missing=args.only_missing, force=args.force,
        ))
        print(pending)
        log.info("待同步应用: %d / %d", pending, len(app_pool))
        return 0

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
                "状态: %s | 评论: %d 条 | 商店总数: %s | 评分: %s",
                preview.get("status", STATUS_OK),
                len(preview.get("reviews", [])),
                preview.get("review_count"),
                preview.get("rating_average"),
            )
        return 0 if ok else 1

    # 批量：从 MetaMetadata known 列表
    apps = select_apps_for_batch(
        app_pool, out_dir,
        max_apps=args.max_apps,
        only_missing=args.only_missing,
        force=args.force,
    )
    if not apps:
        log.info("没有待同步的应用（可能已全部完成）")
        emit_github_output(pending=0, processed=0)
        return 0

    log.info(
        "本批次同步 %d 个应用（workers=%d, meta间隔=%.2fs, 分页+%.2fs, 应用间+%.2fs）",
        len(apps), args.max_workers, args.meta_min_interval,
        args.delay, args.inter_app_delay,
    )

    failed_apps: List[AppRef] = []
    failed_lock = threading.Lock()

    def process_app(app: AppRef) -> None:
        ok = sync_one_app(app, out_dir, manifest, force=args.force, **sync_kwargs)
        if not ok:
            with failed_lock:
                failed_apps.append(app)

    def pause_between_apps() -> None:
        if args.inter_app_delay <= 0:
            return
        time.sleep(args.inter_app_delay + random.uniform(0, args.inter_app_delay * 0.25))

    if args.max_workers <= 1:
        for i, app in enumerate(apps, 1):
            process_app(app)
            if i < len(apps):
                pause_between_apps()
            if i % 25 == 0:
                save_manifest(manifest_path, manifest)
                log.info("进度 %d/%d", i, len(apps))
    else:
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = [pool.submit(process_app, app) for app in apps]
            for i, fut in enumerate(as_completed(futures), 1):
                fut.result()
                if i % 25 == 0:
                    save_manifest(manifest_path, manifest)
                    log.info("进度 %d/%d", i, len(apps))

    if failed_apps and args.retry_failed:
        cooldown = args.retry_cooldown
        if stats["rate_limited"] >= 5 and args.rate_limit_extra_cooldown > 0:
            cooldown += args.rate_limit_extra_cooldown
            log.info(
                "本批触发 Meta 429 %d 次，额外冷却 %.0fs",
                stats["rate_limited"], args.rate_limit_extra_cooldown,
            )
        log.info(
            "失败 %d 个，冷却 %.0fs 后重试一轮（Meta 429 恢复）",
            len(failed_apps), cooldown,
        )
        time.sleep(cooldown)
        retry_list = list(failed_apps)
        for i, app in enumerate(retry_list, 1):
            stats["retried"] += 1
            if sync_one_app(
                app, out_dir, manifest, force=args.force,
                record_failure=False, **sync_kwargs,
            ):
                stats["failed"] -= 1
                log.info("重试成功 %s", app.package_name)
            if i < len(retry_list):
                pause_between_apps()

    save_manifest(manifest_path, manifest)
    processed = stats["fetched"] + stats["no_reviews"] + stats["unavailable"]
    remaining = len(filter_pending_apps(
        app_pool, out_dir, only_missing=args.only_missing, force=args.force,
    ))
    log.info(
        "完成 | 有评论 %d | 无评论 %d | 不可用 %d | 跳过 %d | 失败 %d | 重试 %d | 限流 %d",
        stats["fetched"], stats["no_reviews"], stats["unavailable"],
        stats["skipped"], stats["failed"], stats["retried"], stats["rate_limited"],
    )
    log.info("本批处理 %d 个 | 剩余待同步 %d / %d", processed, remaining, len(app_pool))
    emit_github_output(
        pending=remaining,
        processed=processed,
        failed=stats["failed"],
        rate_limited=stats["rate_limited"],
    )
    return 0 if stats["failed"] == 0 or processed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
