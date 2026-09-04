"""本地 Web 前端服务：起 HTTP 服务、JSON API、SSE 日志流，并自动拉起浏览器。

运行方式：
    python -m manga_uploader --web [--port N] [--no-browser]

后端引擎（Runner / publishers / config）原样复用，前端是 web/ 目录下的 Vue3 页面。
只绑定 127.0.0.1；写操作 API 需带启动时生成的 CSRF token。
"""

from __future__ import annotations

import copy
import json
import logging
import mimetypes
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from . import composer
from .comic import find_meta_file, load_chapters, read_meta
from .config import (
    AppConfig,
    CommonConfig,
    ConfigError,
    DEFAULT_SETTINGS,
    REQUIRED_COOKIES,
    load_config,
)
from .models import Chapter
from .runner import PLATFORM_CLASSES, Runner
from .util import IMAGE_EXTS, get_logger, human_size, setup_logging
from .webui import (
    PLATFORM_CARDS,
    build_app,
    delete_page,
    extract_zip,
    format_full_preview,
    import_staging_base,
    insert_page,
    looks_like_full_comic,
    rename_numeric,
    replace_page,
    read_staff_rows,
    save_config,
    stage_images,
    unwrap_single_dir,
    upsert_staff_page,
    write_page_order,
    write_quick_meta,
    write_staff_rows,
    bilibili_qr_new,
    bilibili_qr_poll,
)

LOGGER_NAME = "manga_uploader"
DEFAULT_PORT = 8970
MAX_PORT_TRIES = 20
LOG_CAPACITY = 2000

WEB_DIR = Path(__file__).resolve().parent / "web"


# ---------------------------------------------------------------- 日志环形缓冲

class LogRing:
    """带序号与条件变量的日志环形缓冲，供 SSE 增量推送。"""

    def __init__(self, capacity: int = LOG_CAPACITY) -> None:
        self.entries: list[dict[str, Any]] = []
        self.cond = threading.Condition()
        self.seq = 0
        self.capacity = capacity

    def append(self, level: str, msg: str) -> int:
        with self.cond:
            self.seq += 1
            self.entries.append({"seq": self.seq, "level": level, "msg": msg})
            if len(self.entries) > self.capacity:
                self.entries = self.entries[-self.capacity:]
            self.cond.notify_all()
            return self.seq

    def tail(self, since: int) -> tuple[list[dict[str, Any]], int]:
        with self.cond:
            entries = [e for e in self.entries if e["seq"] > since]
            last = self.entries[-1]["seq"] if self.entries else since
            return entries, last


class RingHandler(logging.Handler):
    """把 manga_uploader 的日志转发到环形缓冲。"""

    def __init__(self, ring: LogRing, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.ring = ring
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # pragma: no cover
            return
        self.ring.append(record.levelname, msg)


# ---------------------------------------------------------------- 全局状态

class ServerState:
    def __init__(self, config_path: Optional[str] = None) -> None:
        self.ring = LogRing()
        self.publish_lock = threading.Lock()
        self.running = False
        self.qr_lock = threading.Lock()
        self.qr_session: Any = None
        self.qr_key = ""
        self.config_path = config_path


# ---------------------------------------------------------------- 配置载荷

def _app_to_payload(app: AppConfig) -> dict[str, Any]:
    common = {}
    for field in CommonConfig.__dataclass_fields__:
        if field == "confirm":
            continue
        common[field] = getattr(app.common, field)
    platforms = {}
    for key, cfg in app.platforms.items():
        platforms[key] = {
            "enabled": cfg.enabled,
            "cookies": dict(cfg.cookies),
            "settings": dict(cfg.settings),
        }
    return {"common": common, "platforms": platforms}


def _default_payload() -> dict[str, Any]:
    from .config import PlatformConfig

    platforms = {
        key: PlatformConfig(
            name=key, enabled=True, cookies={}, settings=dict(DEFAULT_SETTINGS.get(key, {}))
        )
        for key in DEFAULT_SETTINGS
    }
    return _app_to_payload(AppConfig(common=CommonConfig(), platforms=platforms))


def _load_payload(config_path: Optional[str] = None) -> tuple[dict[str, Any], Optional[str]]:
    """加载 config.yaml 成前端载荷；找不到时返回默认配置 + 提示。"""
    try:
        app = load_config(config_path)
        return _app_to_payload(app), None
    except ConfigError as exc:
        return _default_payload(), str(exc)


def _enabled_with_cookie(payload: dict[str, Any]) -> list[str]:
    """与旧 GUI 语义一致：启用且填了必需 Cookie 的平台。"""
    result = []
    for key, cfg in build_app(payload).platforms.items():
        if key not in PLATFORM_CLASSES or not cfg.enabled:
            continue
        missing = [name for name in REQUIRED_COOKIES.get(key, []) if not cfg.cookies.get(name)]
        if missing:
            continue
        result.append(key)
    return result


# 漫画信息顶层字段（与 tkinter GUI BASE_FIELDS / composer.fields 对齐）
WEB_META_FIELDS: list[tuple[str, str]] = [
    ("event", "展会"),
    ("event_en", "展会罗马音"),
    ("author", "作者/画师"),
    ("author_en", "作者罗马音"),
    ("circle", "社团"),
    ("circle_en", "社团罗马音"),
    ("group", "汉化组"),
    ("title", "中文标题"),
    ("title_jp", "日文原标题"),
    ("title_en", "英文/罗马音标题"),
    ("series", "系列/tag 中文"),
    ("series_en", "系列英文"),
    ("series_jp", "系列日文"),
    ("language", "语言"),
    ("tags", "标签"),
    ("chapter_name", "章节名"),
    ("description", "简介"),
]


def _chapter_summary(comic_dir: str) -> dict[str, Any]:
    root = Path(comic_dir)
    chapters = load_chapters(comic_dir, strict=False)
    meta_file = find_meta_file(root)
    top = read_meta(meta_file) if meta_file else {}
    if not isinstance(top, dict):
        top = {}
    total_pages = sum(len(c.pages) for c in chapters)
    total_bytes = sum(p.stat().st_size for c in chapters for p in c.pages)
    over = sum(
        1
        for chapter in chapters
        for p in chapter.pages
        if p.stat().st_size > 10 * 1024 * 1024
    )
    meta: dict[str, str] = {}
    for key, _label in WEB_META_FIELDS:
        value = top.get(key)
        if key == "tags":
            meta[key] = ",".join(str(t) for t in value) if isinstance(value, list) else str(value or "")
        else:
            meta[key] = str(value or "")
    # 各平台发布内容（PLATFORM_SCHEMA 文字字段，来自 manga.json platforms）
    platforms_meta = top.get("platforms") if isinstance(top.get("platforms"), dict) else {}
    platforms_content: dict[str, dict[str, str]] = {}
    for plat, schema in composer.PLATFORM_SCHEMA.items():
        p = platforms_meta.get(plat) if isinstance(platforms_meta.get(plat), dict) else {}
        platforms_content[plat] = {f["key"]: str(p.get(f["key"]) or "") for f in schema}
    return {
        "meta": meta,
        "platforms_content": platforms_content,
        "chapters": len(chapters),
        "pages": total_pages,
        "size": human_size(total_bytes),
        "over_10mb": over,
        "dir": comic_dir,
        "has_meta_file": meta_file is not None,
    }


def _book_to_compose(comic_dir: str, book: dict[str, Any]) -> dict[str, Any]:
    """把漫画信息表单组合成各平台发布内容（纯计算，不写盘）。

    - 读取磁盘 manga.json 作为基底（保留已保存字段与 platforms 覆盖），
      仅用表单中出现的字段覆盖；
    - tags 表单为逗号分隔字符串，构造时转列表；
    - 用 composer 的平台函数生成「各平台发布内容」栏目展示值。
    """
    root = Path(comic_dir)
    meta_file = find_meta_file(root)
    top = read_meta(meta_file) if meta_file else {}
    if not isinstance(top, dict):
        top = {}
    data = copy.deepcopy(top)
    for key, value in (book or {}).items():
        if value is None:
            continue
        data[key] = value
    # 语言留空默认 Chinese（栏位默认值）
    if not str(data.get("language") or "").strip():
        data["language"] = "Chinese"
    # 罗马音留空时用本地引擎生成，供“填写后自动填入罗马音框”
    for source_key, target_key in (
        ("event", "event_en"),
        ("author", "author_en"),
        ("circle", "circle_en"),
    ):
        source = str(data.get(source_key) or "").strip()
        if source and not str(data.get(target_key) or "").strip():
            roma = composer.to_romaji_title_case(source)
            if roma and roma != source:
                data[target_key] = roma
    jp = str(data.get("title_jp") or "").strip()
    if jp and not str(data.get("title_en") or "").strip():
        roma = composer.to_romaji_title_case(jp)
        if roma and roma != jp:
            data["title_en"] = roma
    data.setdefault("title", root.name)

    # 章节直接用 root 单本语义：标题/简介取合并后的顶层值
    tags_raw = data.get("tags")
    if isinstance(tags_raw, str):
        tags = [p.strip() for p in tags_raw.split(",") if p.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    else:
        tags = []
    chapter = Chapter(
        key="root",
        title=str(data.get("title") or root.name),
        description=str(data.get("description") or "").strip(),
        tags=tags,
        author=str(data.get("author") or "").strip(),
        source_dir=root,
        raw=data,
    )

    romaji = {
        key: str(data.get(key) or "")
        for key in ("event_en", "author_en", "circle_en", "title_en")
    }

    composed: dict[str, dict[str, str]] = {}
    composed["ehentai"] = {
        "category": str(
            ((data.get("platforms") or {}).get("ehentai") or {}).get("category") or ""
        ),
        "language": str(
            ((data.get("platforms") or {}).get("ehentai") or {}).get("language")
            or "Chinese"
        ),
        "langtype": str(
            ((data.get("platforms") or {}).get("ehentai") or {}).get("langtype") or ""
        ),
        "gname_en": composer.ehentai_title_en(chapter),
        "gname_jp": composer.ehentai_title_jp(chapter),
        "comment": composer.ehentai_comment(chapter),
    }
    for plat in ("bilibili", "tieba"):
        composed[plat] = {
            "title": composer.platform_title(chapter, plat),
            "description": composer.platform_body(chapter, plat),
        }
    composed["zaimanhua"] = {
        "work_name": composer.zaim_work_name(chapter),
        "chapter_name": composer.zaim_chapter_name(chapter),
        "introduction": composer.zaim_introduction(chapter),
        "cate": str(
            ((data.get("platforms") or {}).get("zaimanhua") or {}).get("cate") or ""
        ),
    }
    composed["xiaoheihe"] = {
        "title": composer.xiaoheihe_title(chapter),
        "description": composer.xiaoheihe_body(chapter),
    }
    return {
        "platforms_content": composed,
        "romaji": romaji,
        "language": str(data.get("language") or "Chinese"),
    }


def _save_comic_meta(
    comic_dir: str,
    book: dict[str, Any],
    platforms: Optional[dict[str, dict[str, Any]]] = None,
) -> Path:
    """把漫画信息顶层字段与各平台发布内容写回漫画目录的 manga.json（不存在则创建）。

    book 允许 WEB_META_FIELDS 任一字段；tags 传逗号分隔字符串，写回时转数组。
    platforms 形如 {平台: {字段: 值}}，空值字段会被移除（不再覆盖）。
    """
    root = Path(comic_dir)
    meta_file = find_meta_file(root)
    data: dict[str, Any] = {}
    if meta_file:
        try:
            data = read_meta(meta_file)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    changed = False
    for key, _label in WEB_META_FIELDS:
        if key not in book:
            continue
        value = book.get(key)
        if key == "tags":
            raw = value if isinstance(value, list) else [
                t.strip() for t in str(value or "").split(",") if t.strip()
            ]
            if raw != data.get("tags"):
                if raw:
                    data["tags"] = raw
                else:
                    data.pop("tags", None)
                changed = True
            continue
        text = str(value or "").strip()
        if text != (data.get(key) or ""):
            if text:
                data[key] = text
            else:
                data.pop(key, None)
            changed = True
    if platforms:
        data.setdefault("platforms", {})
        existing = data["platforms"]
        if not isinstance(existing, dict):
            existing = {}
            data["platforms"] = existing
        for plat, fields in platforms.items():
            if not isinstance(fields, dict):
                continue
            store = existing.get(plat)
            if not isinstance(store, dict):
                store = {}
                existing[plat] = store
            touched = False
            for fkey, fval in fields.items():
                text = str(fval or "").strip()
                if text:
                    if store.get(fkey) != text:
                        store[fkey] = text
                        touched = True
                else:
                    if fkey in store:
                        store.pop(fkey, None)
                        touched = True
            if touched:
                changed = True
    # 单本导入生成的 chapters 里 folder="root" 条目同步标题/简介
    chapters = data.get("chapters")
    if isinstance(chapters, list):
        for entry in chapters:
            if isinstance(entry, dict) and str(entry.get("folder")) == "root":
                for k in ("title", "description"):
                    if k in book:
                        entry[k] = str(book.get(k) or "").strip()
    path = meta_file or (root / "manga.json")
    if not changed and path.exists():
        return path
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml

        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------- HTTP 服务

class WebHandler(BaseHTTPRequestHandler):
    server: "MangaServer"
    protocol_version = "HTTP/1.1"

    # ---------- 基础 ----------

    def log_message(self, fmt: str, *args: Any) -> None:
        get_logger("web").debug(fmt, *args)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _check_csrf(self) -> bool:
        token = self.headers.get("X-CSRF-Token", "")
        return bool(token) and secrets.compare_digest(token, self.server.csrf_token)

    def _check_qr_csrf(self, query: dict[str, list[str]]) -> bool:
        token = (query.get("token") or [""])[0]
        return bool(token) and secrets.compare_digest(token, self.server.csrf_token)

    # ---------- 入口 ----------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._route_get(parsed)
        else:
            self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        if not self._check_csrf():
            self._json(403, {"error": "CSRF token 无效，请刷新页面后重试"})
            return
        self._route_post(parsed)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
        self.end_headers()

    # ---------- 静态 ----------

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            self._serve_index()
            return
        rel = path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR not in target.parents and target != WEB_DIR:
            self._json(404, {"error": "not found"})
            return
        if not target.is_file():
            self._json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self) -> None:
        index = WEB_DIR / "index.html"
        if not index.is_file():
            self._json(500, {"error": "web/index.html 缺失"})
            return
        html = index.read_text(encoding="utf-8")
        html = html.replace("__CSRF_TOKEN__", self.server.csrf_token)
        html = html.replace("__VERSION__", __version__)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---------- GET API ----------

    def _route_get(self, parsed) -> None:
        path = parsed.path
        if path == "/api/state":
            payload, note = _load_payload(self.server.state.config_path)
            self._json(
                200,
                {
                    "version": __version__,
                    "config": payload,
                    "cards": PLATFORM_CARDS,
                    "platforms": list(PLATFORM_CLASSES),
                    "running": self.server.state.running,
                    "note": note,
                },
            )
        elif path == "/api/events":
            self._handle_events(parse_qs(parsed.query))
        elif path == "/api/proxy/detect":
            from .http_client import detect_system_proxy

            url = detect_system_proxy()
            self._json(200, {"url": url})
        elif path == "/api/ai":
            self._json(200, {"ai": _ai_read(self._config_path())})
        elif path == "/api/dict":
            rows = _dict_load()
            self._json(200, {"rows": rows})
        elif path == "/api/qr/start":
            self._qr_start()
        elif path == "/api/qr/status":
            self._qr_status(parse_qs(parsed.query))
        elif path == "/api/pick":
            self._pick_dir((parse_qs(parsed.query).get("kind") or ["dir"])[0])
        elif path == "/api/page":
            self._api_page(parse_qs(parsed.query))
        elif path == "/api/staff":
            self._api_staff_get(parse_qs(parsed.query))
        else:
            self._json(404, {"error": f"未知接口：{path}"})

    def _handle_events(self, query: dict[str, list[str]]) -> None:
        if not self._check_qr_csrf(query):
            self._json(403, {"error": "CSRF token 无效"})
            return
        try:
            since = int((query.get("since") or ["0"])[0] or 0)
        except ValueError:
            since = 0
        ring = self.server.state.ring
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                entries, _last = ring.tail(since)
                for entry in entries:
                    self.wfile.write(
                        f"data: {json.dumps(entry, ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                    since = entry["seq"]
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                with ring.cond:
                    ring.cond.wait(timeout=15.0)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            pass

    def _qr_start(self) -> None:
        try:
            import qrcode  # noqa: F401
        except ImportError:
            self._json(
                200,
                {
                    "ok": False,
                    "error": "扫码登录需要 qrcode 库：pip install qrcode[pil]；"
                    "或用浏览器登录后粘贴 Cookie。",
                },
            )
            return
        try:
            session, qr_url, qrcode_key = bilibili_qr_new()
        except Exception as exc:
            self._json(200, {"ok": False, "error": f"获取二维码失败：{exc}"})
            return
        with self.server.state.qr_lock:
            self.server.state.qr_session = session
            self.server.state.qr_key = qrcode_key
        import qrcode as _qr
        from io import BytesIO

        img = _qr.make(qr_url)
        buf = BytesIO()
        img.save(buf, format="PNG")
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _qr_status(self, query: dict[str, list[str]]) -> None:
        with self.server.state.qr_lock:
            session = self.server.state.qr_session
            key = self.server.state.qr_key
        if session is None or not key:
            self._json(200, {"ok": False, "status": "idle", "error": "没有进行中的扫码"})
            return
        result = bilibili_qr_poll(session, key)
        self._json(200, result)

    def _api_page(self, query: dict[str, list[str]]) -> None:
        """返回某章节的页图。name（文件名）优先于 index——URL 按文件而不是按页号，
        前端纯前端排序后同一文件 URL 不变，浏览器缓存天然命中；max<=0/缺省 → 原图直发。"""
        try:
            comic_dir = (query.get("dir") or [""])[0]
            chapter_key = (query.get("chapter") or [""])[0]
            page_name = (query.get("name") or [""])[0]
            index = int((query.get("index") or ["0"])[0])
            raw_max = (query.get("max") or [""])[0]
            max_px = int(raw_max) if str(raw_max).strip() else 0
        except ValueError:
            self._json(400, {"error": "参数错误"})
            return
        if not comic_dir or not chapter_key:
            self._json(400, {"error": "缺少参数"})
            return
        try:
            chapters = load_chapters(comic_dir, strict=False)
        except Exception as exc:
            self._json(404, {"error": str(exc)})
            return
        chapter = next((c for c in chapters if c.key == chapter_key), None)
        if chapter is None:
            self._json(404, {"error": "页面不存在"})
            return
        if page_name:
            path = next((p for p in chapter.pages if p.name == page_name), None)
            if path is None:
                self._json(404, {"error": f"页面不存在：{page_name}"})
                return
        else:
            if index < 0 or index >= len(chapter.pages):
                self._json(404, {"error": "页面不存在"})
                return
            path = chapter.pages[index]

        if max_px <= 0:
            # 原图直发：流式写回，不整读进内存、不做任何压缩
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            try:
                with open(path, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, 256 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        from io import BytesIO

        from PIL import Image

        max_px = min(720, max(120, max_px))
        try:
            with Image.open(path) as img:
                img.thumbnail((max_px, max_px))
                buf = BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=80)
        except Exception as exc:
            self._json(500, {"error": f"读取图片失败：{exc}"})
            return
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _pick_dir(self, kind: str = "dir") -> None:
        """kind=dir → 弹目录框；kind=file → 弹文件框（ZIP/CBZ/图片）并直接导入。"""
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            if kind == "file":
                path = filedialog.askopenfilename(
                    title="选择漫画压缩包或图片（ZIP / CBZ / JPG / PNG / GIF / WEBP）",
                    filetypes=[
                        ("压缩包 / 图片", "*.zip *.cbz *.jpg *.jpeg *.png *.gif *.webp"),
                        ("压缩包", "*.zip *.cbz"),
                        ("图片", "*.jpg *.jpeg *.png *.gif *.webp"),
                        ("所有文件", "*.*"),
                    ],
                )
            else:
                path = filedialog.askdirectory(
                    title="选择漫画文件夹（子目录=各话，或直接放图片）"
                )
            root.destroy()
        except Exception as exc:
            self._json(200, {"ok": False, "error": f"无法弹出选择框：{exc}"})
            return
        if not path:
            self._json(200, {"ok": False, "picked": ""})
            return
        path = str(Path(path).resolve())
        if kind == "file":
            suffix = Path(path).suffix.lower()
            try:
                if suffix in (".zip", ".cbz"):
                    comic = _import_archive_path(path)
                elif suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    comic = _import_single_image(path)
                else:
                    self._json(200, {"ok": False, "error": "请选择 ZIP / CBZ 压缩包或图片"})
                    return
            except Exception as exc:
                self._json(200, {"ok": False, "error": f"导入失败：{exc}"})
                return
            self._json(200, {"ok": True, "dir": str(comic)})
            return
        self._json(200, {"ok": True, "picked": path})

    # ---------- POST API ----------

    def _route_post(self, parsed) -> None:
        path = parsed.path
        if path == "/api/config":
            self._api_config()
        elif path == "/api/check":
            self._api_check()
        elif path == "/api/plan":
            self._api_plan()
        elif path == "/api/preview":
            self._api_preview()
        elif path == "/api/publish":
            self._api_publish()
        elif path == "/api/load":
            self._api_load()
        elif path == "/api/meta":
            self._api_meta()
        elif path == "/api/compose":
            self._api_compose()
        elif path == "/api/romaji":
            self._api_romaji()
        elif path == "/api/ai":
            self._api_ai_save()
        elif path == "/api/ai/test":
            self._api_ai_test()
        elif path == "/api/dict":
            self._api_dict_save()
        elif path == "/api/import-path":
            self._api_import_path()
        elif path == "/api/import":
            self._api_import()
        elif path == "/api/insert":
            self._api_page_upload("insert")
        elif path == "/api/replace":
            self._api_page_upload("replace")
        elif path == "/api/delete":
            self._api_page_delete()
        elif path == "/api/rename":
            self._api_rename()
        elif path == "/api/staff":
            self._api_staff_save()
        elif path == "/api/staff/render":
            self._api_staff_render()
        else:
            self._json(404, {"error": f"未知接口：{path}"})

    def _config_path(self) -> Path:
        explicit = self.server.state.config_path
        try:
            return load_config(explicit).path or (Path.cwd() / "config.yaml")
        except ConfigError:
            return Path.cwd() / "config.yaml"

    def _api_config(self) -> None:
        data = self._read_json()
        payload = data.get("config")
        if not isinstance(payload, dict) or not payload:
            self._json(400, {"error": "空请求体"})
            return
        try:
            path = save_config(self._config_path(), payload)
        except Exception as exc:
            self._json(500, {"error": f"保存失败：{exc}"})
            return
        self.server.state.ring.append("INFO", f"已保存配置：{path}")
        self._json(200, {"ok": True, "path": str(path)})

    def _app_from(self, payload: dict[str, Any]) -> AppConfig:
        try:
            return build_app(payload.get("config") or {})
        except Exception as exc:
            raise ConfigError(f"配置解析失败：{exc}") from exc

    def _api_check(self) -> None:
        data = self._read_json()
        try:
            app = self._app_from(data)
        except ConfigError as exc:
            self._json(400, {"error": str(exc)})
            return
        names = [str(n) for n in (data.get("platforms") or [])] or None
        runner = Runner(app)
        try:
            results = runner.check(names)
        except Exception as exc:
            self._json(500, {"error": f"检查失败：{exc}"})
            return
        for r in results:
            mark = "✓" if r.ok else "✗"
            self.server.state.ring.append("INFO", f"{mark} {r.platform}: {r.message}")
        self._json(200, {"results": [{"platform": r.platform, "ok": r.ok, "message": r.message} for r in results]})

    def _api_plan(self) -> None:
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        try:
            app = self._app_from(data)
        except ConfigError as exc:
            self._json(400, {"error": str(exc)})
            return
        names = [str(n) for n in (data.get("platforms") or [])] or None
        only = [str(c) for c in (data.get("chapters") or [])] or None
        runner = Runner(app)
        try:
            plan = runner.build_plan(comic_dir, names=names, only_chapters=only)
        except Exception as exc:
            self._json(500, {"error": f"生成计划失败：{exc}"})
            return
        text = _format_plan_text(plan)
        self._json(200, {"text": text})

    def _api_preview(self) -> None:
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        try:
            app = self._app_from(data)
        except ConfigError as exc:
            self._json(400, {"error": str(exc)})
            return
        names = [str(n) for n in (data.get("platforms") or [])] or None
        only = [str(c) for c in (data.get("chapters") or [])] or None
        runner = Runner(app)
        try:
            preview = runner.build_full_preview(comic_dir, names=names, only_chapters=only)
        except Exception as exc:
            self._json(500, {"error": f"全文预览失败：{exc}"})
            return
        self._json(200, {"text": format_full_preview(preview), "chapters": _preview_struct(preview)})

    def _api_publish(self) -> None:
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        state = self.server.state
        with state.publish_lock:
            if state.running:
                self._json(409, {"error": "已有发布任务在运行，请等待完成"})
                return
            state.running = True
        only = [str(c) for c in (data.get("chapters") or [])] or None
        names = _enabled_with_cookie(data.get("config") or {})
        if not names:
            with state.publish_lock:
                state.running = False
            self._json(400, {"error": "没有启用的平台或都未填 Cookie"})
            return
        state.ring.append("INFO", f"开始发布到：{', '.join(names)}（章节 {only or '全部'}）")

        def worker() -> None:
            try:
                app = build_app(data.get("config") or {})
                runner = Runner(app)
                results = runner.run_publish(
                    comic_dir, names=names, only_chapters=only, confirm=False
                )
                ok = sum(1 for r in results if r.status == "ok")
                partial = sum(1 for r in results if r.status == "partial")
                failed = sum(1 for r in results if r.status == "failed")
                skipped = sum(1 for r in results if r.status == "skipped")
                state.ring.append(
                    "INFO",
                    f"发布完成：成功 {ok}，部分成功 {partial}，失败 {failed}，跳过 {skipped}",
                )
                for r in results:
                    url = f" {r.url}" if r.url else ""
                    state.ring.append(
                        "INFO",
                        f"  [{r.status}] {r.platform} {r.title}: {r.message}{url}",
                    )
            except Exception as exc:
                state.ring.append("ERROR", f"发布任务异常：{exc}")
            finally:
                with state.publish_lock:
                    state.running = False

        threading.Thread(target=worker, daemon=True).start()
        self._json(200, {"ok": True, "running": True})

    def _api_load(self) -> None:
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        try:
            summary = _chapter_summary(comic_dir)
        except Exception as exc:
            self._json(500, {"error": f"加载失败：{exc}"})
            return
        self._json(200, summary)

    def _api_meta(self) -> None:
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        book = data.get("book")
        platforms = data.get("platforms")
        if not comic_dir or not isinstance(book, dict):
            self._json(400, {"error": "缺少参数"})
            return
        platforms = platforms if isinstance(platforms, dict) else None
        try:
            path = _save_comic_meta(comic_dir, book, platforms=platforms)
        except Exception as exc:
            self._json(500, {"error": f"保存失败：{exc}"})
            return
        self.server.state.ring.append("INFO", f"已保存漫画内容：{path}")
        self._json(200, {"ok": True, "path": str(path)})

    def _api_compose(self) -> None:
        """按当前漫画信息实时组合各平台发布内容（只计算不写盘）。"""
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        book = data.get("book")
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        book = book if isinstance(book, dict) else {}
        try:
            composed = _book_to_compose(comic_dir, book)
        except Exception as exc:
            logging.getLogger(LOGGER_NAME).error(
                "组合失败：%s", exc, exc_info=True,
            )
            self._json(500, {"error": f"组合失败：{exc}"})
            return
        self._json(200, {"ok": True, **composed})

    def _api_romaji(self) -> None:
        """事件/作者/社团→ *_en、日文标题→ title_en。配置了 AI(账号页) 则优先 AI，失败回退本地。"""
        data = self._read_json()
        values = data.get("values")
        if not isinstance(values, dict):
            self._json(400, {"error": "缺少 values"})
            return
        ai_cfg = _ai_read(self._config_path())
        use_ai = composer.ai_config_is_ready(ai_cfg)

        def _conv(text: str, kind: str) -> str:
            if use_ai:
                try:
                    return composer.ai_to_romaji(text, kind=kind, cfg=ai_cfg)
                except Exception:
                    pass
            return composer.to_romaji_title_case(text)

        out: dict[str, str] = {}
        for source_key, kind in (("event", "name"), ("author", "name"), ("circle", "name")):
            target = source_key + "_en"
            text = str(values.get(source_key) or "").strip()
            if text:
                out[target] = _conv(text, kind)
        jp = str(values.get("title_jp") or "").strip()
        if jp:
            out["title_en"] = _conv(jp, "title")
        self._json(200, {"ok": True, "romaji": out, "engine": "ai" if use_ai else "local"})

    def _api_ai_save(self) -> None:
        data = self._read_json()
        ai = data.get("ai")
        if not isinstance(ai, dict):
            self._json(400, {"error": "缺少 ai"})
            return
        try:
            _ai_write(self._config_path(), ai)
        except Exception as exc:
            self._json(500, {"error": f"保存失败：{exc}"})
            return
        self._json(200, {"ok": True})

    def _api_ai_test(self) -> None:
        data = self._read_json()
        ai = data.get("ai")
        if not isinstance(ai, dict) or not composer.ai_config_is_ready(ai):
            self._json(200, {"ok": False, "error": "AI 未配置完整（需开关+地址+Key+模型）"})
            return
        try:
            out = composer.ai_to_romaji("例大祭", kind="name", cfg=ai)
        except Exception as exc:
            self._json(200, {"ok": False, "error": f"AI 请求失败：{exc}"})
            return
        self._json(200, {"ok": True, "result": out})

    def _api_dict_save(self) -> None:
        data = self._read_json()
        rows = data.get("rows")
        if not isinstance(rows, list):
            self._json(400, {"error": "缺少 rows"})
            return
        try:
            _dict_write(rows)
        except Exception as exc:
            self._json(500, {"error": f"保存失败：{exc}"})
            return
        self._json(200, {"ok": True})

    def _api_import_path(self) -> None:
        """按本地 .zip/.cbz 路径直接导入（供路径输入框用）。"""
        data = self._read_json()
        raw_path = str(data.get("path") or "").strip()
        if not raw_path:
            self._json(400, {"error": "缺少路径"})
            return
        if Path(raw_path).suffix.lower() not in (".zip", ".cbz"):
            self._json(400, {"error": "路径不是 ZIP / CBZ 压缩包"})
            return
        if not Path(raw_path).is_file():
            self._json(404, {"error": f"文件不存在：{raw_path}"})
            return
        try:
            comic = _import_archive_path(raw_path)
        except Exception as exc:
            self._json(500, {"error": f"导入失败：{exc}"})
            return
        self.server.state.ring.append("INFO", f"已从压缩包导入：{raw_path}")
        self._json(200, {"ok": True, "dir": str(comic)})

    # ---------- 漫画导入（multipart 上传） ----------

    def _api_import(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        boundary_match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
        if not boundary_match:
            self._json(400, {"error": "缺少 multipart boundary"})
            return
        boundary = boundary_match.group(1) or boundary_match.group(2)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1024 * 1024 * 1024:
            self._json(400, {"error": "请求体大小异常"})
            return
        body = self.rfile.read(length)
        try:
            fields, files = _split_multipart(body, boundary)
        except ValueError as exc:
            self._json(400, {"error": f"解析上传失败：{exc}"})
            return
        if not files:
            self._json(400, {"error": "没有收到任何文件"})
            return
        try:
            comic_dir = _import_files(fields, files)
        except Exception as exc:
            self._json(500, {"error": f"导入失败：{exc}"})
            return
        self.server.state.ring.append("INFO", f"已导入漫画：{comic_dir}")
        self._json(200, {"ok": True, "dir": str(comic_dir)})

    # ---------- 页面插入 / 替换 / 删除 ----------

    def _api_page_upload(self, mode: str) -> None:
        """multipart 上传新图：insert → 插到第 index 页前；replace → 替换第 index 页。"""
        content_type = self.headers.get("Content-Type", "")
        boundary_match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
        if not boundary_match:
            self._json(400, {"error": "缺少 multipart boundary"})
            return
        boundary = boundary_match.group(1) or boundary_match.group(2)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 512 * 1024 * 1024:
            self._json(400, {"error": "请求体大小异常"})
            return
        body = self.rfile.read(length)
        try:
            fields, files = _split_multipart(body, boundary)
        except ValueError as exc:
            self._json(400, {"error": f"解析上传失败：{exc}"})
            return
        comic_dir = str(fields.get("dir") or "").strip()
        chapter_key = str(fields.get("chapter") or "").strip() or "root"
        try:
            index = int(str(fields.get("index") or "0").strip())
        except ValueError:
            index = 0
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        if not files:
            self._json(400, {"error": "没有收到图片文件"})
            return
        name, data = files[0]
        ext = Path(name).suffix.lower()
        if ext not in IMAGE_EXTS:
            self._json(400, {"error": f"不支持的图片格式：{ext or '(无扩展名)'}"})
            return
        try:
            if mode == "replace":
                count = replace_page(comic_dir, chapter_key, index, data, ext)
            else:
                count = insert_page(comic_dir, chapter_key, index, data, name)
        except Exception as exc:
            verb = "替换" if mode == "replace" else "插入"
            self._json(500, {"error": f"{verb}失败：{exc}"})
            return
        verb = "替换" if mode == "replace" else "插入"
        self.server.state.ring.append(
            "INFO", f"已{verb}页：{Path(comic_dir).name}（{chapter_key}，共 {count} 页）"
        )
        # 带回最终页名列表（插入重名会自动改名/替换语义 A 会换名），前端本地状态直接采用
        try:
            chapters = load_chapters(comic_dir, strict=False)
            chapter = next((c for c in chapters if c.key == chapter_key), None)
            names = [p.name for p in chapter.pages] if chapter else []
        except Exception:
            names = []
        self._json(200, {"ok": True, "pages": count, "names": names})

    def _api_page_delete(self) -> None:
        """删除漫画某章节第 index 页，并重排后续页码保持连续。"""
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        chapter_key = str(data.get("chapter") or "").strip() or "root"
        try:
            index = int(str(data.get("index") or "0"))
        except (TypeError, ValueError):
            index = 0
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        try:
            count = delete_page(comic_dir, chapter_key, index)
        except Exception as exc:
            self._json(500, {"error": f"删除失败：{exc}"})
            return
        self.server.state.ring.append(
            "INFO", f"已删除页：{Path(comic_dir).name}（{chapter_key}，剩 {count} 页）"
        )
        try:
            chapters = load_chapters(comic_dir, strict=False)
            chapter = next((c for c in chapters if c.key == chapter_key), None)
            names = [p.name for p in chapter.pages] if chapter else []
        except Exception:
            names = []
        self._json(200, {"ok": True, "pages": count, "names": names})

    def _apply_local_pages(self, comic_dir: str, chapter_key: str, pages) -> None:
        """把前端带来的本地页序落盘（页序的真值在前端，磁盘随操作前对齐）。

        pages 为该章节完整文件名列表；集合必须与磁盘章节页面完全一致。
        """
        if not isinstance(pages, list) or not pages:
            return
        chapters = load_chapters(comic_dir, strict=False)
        chapter = next((c for c in chapters if c.key == chapter_key), None)
        if chapter is None:
            raise ValueError(f"找不到章节：{chapter_key}")
        valid = {p.name for p in chapter.pages}
        names = [str(p) for p in pages]
        if set(names) != valid or len(names) != len(valid):
            raise ValueError("pages 必须恰好是当前章节的全部页面文件")
        write_page_order(comic_dir, chapter_key, names)

    def _apply_local_pages_map(self, comic_dir: str, pages_map) -> None:
        """批量落盘前端带来的各章节本地页序（/api/preview、/api/publish 用）。"""
        if not isinstance(pages_map, dict):
            return
        for key, names in pages_map.items():
            self._apply_local_pages(comic_dir, str(key), names)

    def _api_rename(self) -> None:
        """把章节按当前页序重命名为 001.ext / 002.ext…。"""
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        chapter_key = str(data.get("chapter") or "").strip() or "root"
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        try:
            count = rename_numeric(comic_dir, chapter_key)
        except Exception as exc:
            self._json(500, {"error": f"重命名失败：{exc}"})
            return
        self.server.state.ring.append(
            "INFO", f"已重命名为 001…：{Path(comic_dir).name}（{chapter_key}，{count} 页）"
        )
        self._json(200, {"ok": True, "pages": count})

    # ---------- Staff 页（后端零渲染：只管名单存取 + 成品 PNG 落页） ----------

    def _staff_target(self) -> tuple[Optional[str], Optional[str]]:
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        chapter_key = str(data.get("chapter") or "").strip() or "root"
        return comic_dir or None, chapter_key

    def _api_staff_get(self, query: dict[str, list[str]]) -> None:
        """读章节的 staff 数据（rows + 背景页选择；无保存过则 rows=None）。"""
        comic_dir = (query.get("dir") or [""])[0].strip()
        chapter_key = (query.get("chapter") or [""])[0].strip() or "root"
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        try:
            saved = read_staff_rows(comic_dir, chapter_key)
        except Exception as exc:
            self._json(500, {"error": f"读取 staff 名单失败：{exc}"})
            return
        rows = saved["rows"] if saved else None
        bg = saved["bg"] if saved else None
        self._json(200, {"ok": True, "rows": rows, "bg": bg})

    def _api_staff_save(self) -> None:
        """保存 staff 名单 + 背景页选择到 manga.json 章节条目 staff 字段。"""
        data = self._read_json()
        comic_dir = str(data.get("dir") or "").strip()
        chapter_key = str(data.get("chapter") or "").strip() or "root"
        rows = data.get("rows")
        bg = data.get("bg")
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        if not isinstance(rows, list):
            self._json(400, {"error": "rows 必须是 [[职位, 名字], …]"})
            return
        if not isinstance(bg, int):
            bg = None
        try:
            count = write_staff_rows(comic_dir, chapter_key, rows, bg)
        except Exception as exc:
            self._json(500, {"error": f"保存 staff 名单失败：{exc}"})
            return
        self.server.state.ring.append(
            "INFO", f"已保存 staff 名单：{Path(comic_dir).name}（{chapter_key}，{count} 行）"
        )
        self._json(200, {"ok": True, "rows": count})

    def _api_staff_render(self) -> None:
        """接收前端 canvas 渲染好的 staff 页 PNG，落成第 2 页（重复生成覆盖）。"""
        content_type = self.headers.get("Content-Type", "")
        boundary_match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
        if not boundary_match:
            self._json(400, {"error": "缺少 multipart boundary"})
            return
        boundary = boundary_match.group(1) or boundary_match.group(2)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 512 * 1024 * 1024:
            self._json(400, {"error": "请求体大小异常"})
            return
        body = self.rfile.read(length)
        try:
            fields, files = _split_multipart(body, boundary)
        except ValueError as exc:
            self._json(400, {"error": f"解析上传失败：{exc}"})
            return
        comic_dir = str(fields.get("dir") or "").strip()
        chapter_key = str(fields.get("chapter") or "").strip() or "root"
        if not comic_dir:
            self._json(400, {"error": "缺少漫画目录"})
            return
        try:  # 前端带来的本地页序先落盘，staff 页插回位置才符合当前所见
            self._apply_local_pages(
                comic_dir, chapter_key, json.loads(fields.get("pages") or "null"),
            )
        except Exception as exc:
            self._json(400, {"error": f"页序不一致：{exc}"})
            return
        if not files:
            self._json(400, {"error": "没有收到 staff 页图片"})
            return
        _name, data = files[0]
        try:
            count = upsert_staff_page(comic_dir, chapter_key, data)
        except Exception as exc:
            self._json(500, {"error": f"staff 页落页失败：{exc}"})
            return
        self.server.state.ring.append(
            "INFO", f"已生成 staff 页：{Path(comic_dir).name}（{chapter_key}，共 {count} 页）"
        )
        try:
            chapters = load_chapters(comic_dir, strict=False)
            chapter = next((c for c in chapters if c.key == chapter_key), None)
            names = [p.name for p in chapter.pages] if chapter else []
        except Exception:
            names = []
        self._json(200, {"ok": True, "pages": count, "names": names})


def _preview_struct(preview) -> list[dict[str, Any]]:
    """把全文预览结果转成前端通用的发布示意图结构(不解析平台专属内容)。"""
    out: list[dict[str, Any]] = []
    for chapter, rows in preview:
        out.append(
            {
                "key": chapter.key,
                "title": chapter.title,
                "author": chapter.author,
                "description": chapter.description,
                "page_count": len(chapter.pages),
                "pages": [p.name for p in chapter.pages],
                "platforms": [
                    {"key": name, "lines": [str(line) for line in lines]}
                    for name, lines in rows
                ],
            }
        )
    return out


def _format_plan_text(plan) -> str:
    lines = ["=" * 60, "【发布计划】", "=" * 60]
    for chapter, steps in plan:
        size = sum(p.stat().st_size for p in chapter.pages)
        lines.append(f"\n■ 章节：{chapter.title}（{chapter.key}）")
        lines.append(f"  页面：{len(chapter.pages)} 张 / {human_size(size)}")
        if chapter.description:
            desc = chapter.description if len(chapter.description) <= 60 else chapter.description[:57] + "…"
            lines.append(f"  简介：{desc}")
        if not steps:
            lines.append("  无可用平台（均未启用或配置缺失）")
        for name, rows in steps:
            lines.append(f"  ● {name}")
            for row in rows:
                lines.append(f"      - {row}")
    return "\n".join(lines)


def _import_archive_path(archive_path: str) -> Path:
    """本地 ZIP/CBZ 直接解压进导入缓存：完整漫画原样用，否则按单本暂存补 manga.json。"""
    from .util import is_image

    base = import_staging_base()
    archive = Path(archive_path)
    work = base / f"import_{int(time.time_ns() % 10**9)}"
    work.mkdir(parents=True, exist_ok=False)
    extracted = extract_zip(archive, work / "unpacked")
    extracted = unwrap_single_dir(extracted)
    if looks_like_full_comic(extracted):
        return extracted
    images = sorted(
        (p for p in extracted.iterdir() if p.is_file() and is_image(p)),
        key=lambda p: p.stem.lower(),
    )
    if not images:
        raise ValueError("压缩包里没有找到漫画图片（jpg/png/gif/webp）")
    staged = stage_images(images, title_hint=archive.stem)
    write_quick_meta(
        staged, {"title": archive.stem, "author": "", "description": ""}
    )
    return staged


def _import_single_image(image_path: str) -> Path:
    """单张图片当单本导入（暂存 + 补 manga.json，标题先取文件名，可再编辑）。"""
    src = Path(image_path)
    staged = stage_images([src], title_hint=src.stem)
    write_quick_meta(staged, {"title": src.stem, "author": "", "description": ""})
    return staged


def _overrides_path() -> Path:
    return Path(composer.__file__).resolve().parent / "data" / "romaji_overrides.json"


def _dict_load() -> list[list[str]]:
    path = _overrides_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [[str(k), str(v)] for k, v in (data.items() if isinstance(data, dict) else [])]


def _dict_write(rows: list[Any]) -> None:
    merged: dict[str, str] = {}
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            key = str(row[0]).strip()
            if key:
                merged[key] = str(row[1]).strip()
    path = _overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def _ai_read(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    ai = raw.get("ai") if isinstance(raw, dict) else None
    return ai if isinstance(ai, dict) else {}


def _ai_write(config_path: Path, ai: dict[str, Any]) -> None:
    import yaml

    raw: dict[str, Any] = {}
    if config_path.is_file():
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    cleaned = {k: v for k, v in ai.items() if k in ("enabled", "base_url", "api_key", "model", "timeout", "prompt")}
    raw["ai"] = cleaned
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _split_multipart(
    body: bytes, boundary: str
) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    """轻量 multipart 解析：按 boundary 切分，避开 email 全量建树的慢与内存膨胀。

    兼容中文文件名的 filename*=utf-8''… 编码。返回 (普通字段, [(文件名, 内容)])。
    """
    delim = f"--{boundary}".encode()
    parts = body.split(delim)
    fields: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []
    for seg in parts[1:]:
        if seg.startswith(b"--"):  # 结束分隔符 "--\r\n"
            break
        seg = seg.lstrip(b"\r\n")
        hi = seg.find(b"\r\n\r\n")
        if hi < 0:
            continue
        head = seg[:hi]
        content = seg[hi + 4:]
        if content.endswith(b"\r\n"):
            content = content[:-2]
        cd = ""
        for hline in head.decode("utf-8", "replace").split("\r\n"):
            hname, _, hval = hline.partition(":")
            if hname.strip().lower() == "content-disposition":
                cd = hval
                break
        name_m = re.search(r'name="([^"]*)"', cd)
        fn_m = re.search(r'filename="([^"]*)"', cd)
        fn_star = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;\r\n]*)", cd)
        filename = None
        if fn_star:
            filename = unquote(fn_star.group(1))
        elif fn_m:
            filename = fn_m.group(1)
        if filename is not None:
            files.append((filename, content))
        elif name_m:
            value = content.decode("utf-8", errors="replace").strip()
            fields[name_m.group(1)] = value
    return fields, files


def _import_files(fields: dict[str, str], files: list[tuple[str, bytes]]) -> Path:
    """按上传来源落地到导入缓存：ZIP/CBZ 解压；多图暂存 + 补 manga.json。"""
    from .util import is_image

    base = import_staging_base()
    meta = {
        "title": fields.get("meta_title", ""),
        "author": fields.get("meta_author", ""),
        "description": fields.get("meta_desc", ""),
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")

    archives = [
        (name, data)
        for name, data in files
        if Path(name).suffix.lower() in (".zip", ".cbz")
    ]
    if archives:
        # 只处理第一个压缩包，避免歧义
        name, data = archives[0]
        work = base / f"import_{int(time.time_ns() % 10**9)}"
        work.mkdir(parents=True, exist_ok=False)
        archive_path = work / name
        archive_path.write_bytes(data)
        extracted = extract_zip(archive_path, work / "unpacked")
        extracted = unwrap_single_dir(extracted)
        if looks_like_full_comic(extracted):
            return extracted
        images = sorted(
            (p for p in extracted.iterdir() if p.is_file() and is_image(p)),
            key=lambda p: p.stem.lower(),
        )
        if not images:
            raise ValueError("压缩包里没有找到漫画图片（jpg/png/gif/webp）")
        staged = stage_images(images, title_hint=meta.get("title") or name)
        write_quick_meta(staged, meta)
        return staged

    images = [
        (name, data)
        for name, data in files
        if Path(name).suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp")
    ]
    if not images:
        raise ValueError("没有找到图片或 ZIP/CBZ 压缩包")
    images.sort(key=lambda pair: pair[0].lower())
    work = base / f"import_{int(time.time_ns() % 10**9)}"
    work.mkdir(parents=True, exist_ok=False)
    temp_paths: list[Path] = []
    for name, data in images:
        p = work / name
        p.write_bytes(data)
        temp_paths.append(p)
    staged = stage_images(temp_paths, title_hint=meta.get("title") or "comic")
    write_quick_meta(staged, meta)
    return staged


# ---------------------------------------------------------------- 服务入口

class MangaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], state: ServerState) -> None:
        super().__init__(addr, WebHandler)
        self.state = state
        self.csrf_token = secrets.token_urlsafe(16)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """浏览器中途取消连接（拖拽/上传/SSE/刷新）是常态，不打整段 traceback。"""
        exc = sys.exc_info()[1]
        if isinstance(
            exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)
        ):
            get_logger("web").debug("客户端断开连接：%r", exc)
        else:
            super().handle_error(request, client_address)


def _find_free_port(
    start: int = DEFAULT_PORT, tries: int = MAX_PORT_TRIES, host: str = "127.0.0.1"
) -> int:
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"端口 {start}~{start + tries - 1} 均被占用")


def _lan_ip() -> str:
    """取一个局域网可达的本机 IP（不实际发包）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def run_server(
    port: Optional[int] = None,
    *,
    open_browser: bool = True,
    verbose: bool = False,
    config_path: Optional[str] = None,
    host: str = "127.0.0.1",
) -> None:
    setup_logging(verbose=verbose)
    state = ServerState(config_path=config_path)
    ring_handler = RingHandler(state.ring, level=logging.DEBUG if verbose else logging.INFO)
    logging.getLogger(LOGGER_NAME).addHandler(ring_handler)

    listen_port = port or _find_free_port(host=host)
    server = MangaServer((host, listen_port), state)
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{browse_host}:{listen_port}/"
    get_logger("web").info("manga-uploader Web 前端已启动：%s", url)
    if host in ("0.0.0.0", "::"):
        get_logger("web").info("已绑定 %s，局域网访问：http://%s:%d/", host, _lan_ip(), listen_port)
    if config_path:
        get_logger("web").info("配置文件：%s", Path(config_path).resolve())

    if open_browser:
        threading.Timer(0.3, lambda: _open_url(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        get_logger("web").info("已停止")
    finally:
        server.server_close()


def _open_url(url: str) -> None:
    """拉起系统默认浏览器；失败把网址复制到剪贴板兜底。"""
    try:
        if webbrowser.open(url, new=2):
            return
    except Exception:
        pass
    try:
        import subprocess

        if __import__("sys").platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "", url])
        else:
            subprocess.Popen(["xdg-open", url])
        return
    except Exception:
        pass
    print(f"请手动在浏览器打开：{url}")
