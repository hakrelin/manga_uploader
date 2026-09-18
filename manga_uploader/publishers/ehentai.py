"""e-hentai 图库上传。

e-hentai 没有公开的上传 API，本模块的策略是：
1. GET https://upload.e-hentai.org/managegallery?act=new 并用 HTMLParser
   解析表单字段；
2. 根据表单真实字段名填标题/简介/分类/评分/语言/标签；
3. multipart POST 上传全部页面。

这样站点改版时大多只要更新提示，不必改代码。上传需要 e-hentai
账号（ipb_member_id / ipb_pass_hash Cookie），且账号需满足站方
上传资格（通常要求注册满一段时间、无违规等）。
"""

from __future__ import annotations

import mimetypes
import os
import re
import tempfile
import threading
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..http_client import HttpClient, HttpError
from ..models import Chapter, CheckResult, PublishResult
from .. import composer
from .base import BasePublisher, PublisherError

UPLOAD_PAGE_URL = "https://upload.e-hentai.org/managegallery?act=new"
CHECK_PAGE_URL = "https://e-hentai.org/home.php"
# 上传进度查询：POST /api {"method":"uploadprogress","apiuid":…,"apikey":…,"progresskey":…}
# （网页端 update_progress() 用的就是这个接口，返回 {"progress":"<html>", "done":…}）
UPLOAD_API_URL = "https://upload.e-hentai.org/api"
PROGRESS_POLL_INTERVAL = 1.0

# 归档（zip）里站点能直接吃下的图片格式；其它格式（如 webp）才需要转换
ARCHIVE_OK_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
# upload.e-hentai.org 前面挡着 Cloudflare：单次请求体超过 ~100MB 直接 413
# （2026-09-18 实测：原始图片打包 300MB+ → `413 Payload Too Large`）。
# 所以归档按“原始图片合计”估个上限，超了就退回压缩管线再打包。
DEFAULT_ZIP_MAX_MB = 90.0


def page_path(page) -> Path:
    """统一取页面文件路径：PreparedPage 有 .path，原始页就是 Path/str。"""
    return Path(getattr(page, "path", page))


_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(text: str) -> str:
    """去掉站点进度文案里的 HTML 标签，压成一行。"""
    plain = _TAG_RE.sub(" ", text or "").replace("&nbsp;", " ")
    return " ".join(plain.split())


# e-hentai 是境外站点：upload.e-hentai.org 在国内直连基本必然超时。
# 报错时把“怎么办”直接写进提示，省得用户对着 ConnectTimeoutError 猜。
CONNECT_HINT = (
    "e-hentai 的上传域名（upload.e-hentai.org）在国内直连通常连不上。"
    "请任选其一后重试：① 在配置的 e-hentai 里勾选「使用系统代理」（或填写代理地址 "
    "proxy_url，例如 http://127.0.0.1:7890）；② 在 GUI「设置」里开启全局系统代理；"
    "③ 让本机能直连外网（换网络/开代理软件的系统代理）。"
    "注意贴吧/B站等国内站不需要代理，可以在 platforms.<平台>.settings 里单独给 "
    "e-hentai 配代理、国内站保持直连。"
)


def _is_connect_error(exc: BaseException) -> bool:
    """判断异常是否为“连不上/超时”这类网络不可达（而非站点的业务报错）。"""
    text = f"{exc.__class__.__name__}: {exc}".lower()
    for marker in ("timed out", "timeout", "max retries exceeded", "connection refused",
                   "connectionerror", "failed to establish", "connection reset",
                   "proxyerror", "sslerror", "name resolution", "getaddrinfo"):
        if marker in text:
            return True
    return False


def _is_upload_page_url(url: str) -> bool:
    """判断最终 URL 是否就是上传页本身（主机 + 路径都一致）。"""
    try:
        got = urlsplit(url)
        want = urlsplit(UPLOAD_PAGE_URL)
        got_host = got.netloc.lower()
        want_host = want.netloc.lower()
        got_path = (got.path or "/").rstrip("/") or "/"
        want_path = (want.path or "/").rstrip("/") or "/"
        return got_host == want_host and got_path == want_path
    except ValueError:  # pragma: no cover
        return False


class _Field:
    def __init__(
        self,
        name: str,
        type_: str,
        value: str = "",
        options: list[tuple[str, str]] | None = None,
        selected: str = "",
        checked: bool = False,
    ):
        self.name = name
        self.type = type_
        self.value = value
        self.options = options or []  # [(value, 显示文本)]
        self.selected = selected      # select 当前选中值（无 selected 属性则为 ""）
        self.checked = checked        # radio/checkbox 是否勾选

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Field {self.type} {self.name}={self.value!r} "
            f"selected={self.selected!r} checked={self.checked} options={len(self.options)}>"
        )


class _Form:
    def __init__(self) -> None:
        self.action = ""
        self.method = "post"
        self.fields: list[_Field] = []

    def by_name(self, name: str) -> _Field | None:
        for field in self.fields:
            if field.name == name:
                return field
        return None

    def has(self, name: str) -> bool:
        return self.by_name(name) is not None


class _FormParser(HTMLParser):
    """把上传页里第一个含 <input type=file> 且带文件名的表单解析出来。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self._form: _Form | None = None
        self._field: _Field | None = None
        self._in_option = False
        self._option_value = ""
        self._option_text: list[str] = []
        self._option_selected = False
        self._textarea_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): (value or "") for key, value in attrs}
        if tag == "form":
            self._form = _Form()
            self._form.action = attr.get("action", "")
            self._form.method = attr.get("method", "post").lower()
            self.forms.append(self._form)
            return
        if self._form is None:
            return
        if tag == "input":
            raw_keys = {key.lower() for key, _value in attrs}
            self._field = _Field(
                attr.get("name", ""),
                attr.get("type", "text"),
                attr.get("value", ""),
                checked="checked" in raw_keys,
            )
            self._form.fields.append(self._field)
        elif tag == "textarea":
            self._field = _Field(attr.get("name", ""), "textarea", attr.get("value", ""))
            self._form.fields.append(self._field)
            self._textarea_parts = []
        elif tag == "select":
            self._field = _Field(attr.get("name", ""), "select", attr.get("value", ""))
            self._form.fields.append(self._field)
        elif tag == "option" and self._field and self._field.type == "select":
            self._in_option = True
            self._option_value = attr.get("value", "")
            self._option_text = []
            raw_keys = {key.lower() for key, _value in attrs}
            self._option_selected = "selected" in raw_keys

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._in_option:
            self._in_option = False
            if self._field and self._field.type == "select":
                self._field.options.append((self._option_value, "".join(self._option_text).strip()))
                if self._option_selected:
                    self._field.selected = self._option_value
            self._option_selected = False
        elif tag == "select":
            self._field = None
        elif tag == "textarea" and self._field:
            self._field.value = "".join(self._textarea_parts).strip()
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._in_option:
            self._option_text.append(data)
        elif self._field and self._field.type == "textarea":
            self._textarea_parts.append(data)

    def upload_form(self) -> _Form | None:
        """优先取带 name 输入框的表单，退化到第一个文件上传表单。"""
        for form in self.forms:
            file_names = [f.name for f in form.fields if f.type == "file"]
            if file_names and form.has("name"):
                return form
        for form in self.forms:
            if any(f.type == "file" for f in form.fields):
                return form
        return None


def _option_labels(field: _Field) -> list[str]:
    return [label for _, label in field.options]


# 表单字段映射默认值（可在 config.yaml 的 platforms.ehentai.settings.field_map
# 覆盖，也可在 GUI“上传表单填写…”里改）。行格式：
#   label  = 用途说明（仅作展示）
#   field  = 页面输入框的 name（不写则按 match 关键词自动找）
#   source = 值来源：auto:title_en / auto:title_jp / auto:comment 会自动按
#            manga.json 元数据组合；也兼容旧值 title/description/meta:…。
# 以下字段名按 upload.e-hentai.org/managegallery?act=new 真实页面核对。
DEFAULT_FIELD_ROWS: list[dict] = [
    {"label": "英文/罗马字标题", "field": "gname_en", "source": "auto:title_en"},
    {
        "label": "日文原标题（可选）",
        "field": "gname_jp",
        "match": ["jpn", "japanese", "original", "日文"],
        "source": "auto:title_jp",
    },
    {
        "label": "上传者评论",
        "field": "ulcomment",
        "match": ["comment", "desc", "uploader"],
        "source": "auto:comment",
    },
    {"label": "同意服务条款（勾选）", "field": "tos", "source": "text:on"},
]


class EhentaiPublisher(BasePublisher):
    key = "ehentai"
    display_name = "e-hentai"

    def __init__(self, cfg, common, output_dir=None):
        super().__init__(cfg, common, output_dir)
        # 站点上传进度：apiuid/apikey/progresskey（每次 publish 时从上上传页里取）
        self._progress_ctx: dict[str, str] = {}
        self._progress_chapter_key = ""
        self._progress_volume = ""

    def _upload_mode(self) -> str:
        """zip（默认，站点稳定接受归档）或 files（逐张多文件）。"""
        mode = str(self.cfg.get("upload_mode") or "").strip().lower()
        return mode if mode in ("files", "individual") else "zip"

    def zip_max_bytes(self) -> int:
        """单次上传的归档大小上限（`zip_max_mb`，默认 90MB；填 0 = 不限制）。"""
        try:
            mb = float(self.cfg.get("zip_max_mb", DEFAULT_ZIP_MAX_MB))
        except (TypeError, ValueError):
            mb = DEFAULT_ZIP_MAX_MB
        return int(mb * 1024 * 1024) if mb > 0 else 0

    def zip_split_uploads(self) -> bool:
        """归档超过单次上传上限时是否自动分卷（每卷一次普通上传，画质不变）。"""
        value = self.cfg.get("zip_split_uploads", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off")
        return bool(value)

    @staticmethod
    def _split_pages_by_size(pages: list, limit: int) -> list[list]:
        """按原始体积把页面切卷（每卷 ≤ limit 字节）；limit=0 表示不切。"""
        if not limit:
            return [list(pages)]
        parts: list[list] = []
        current: list = []
        size = 0
        for page in pages:
            page_size = page_path(page).stat().st_size
            if current and size + page_size > limit:
                parts.append(current)
                current, size = [], 0
            current.append(page)
            size += page_size
        if current:
            parts.append(current)
        return parts

    def _gallery_ref(self, resp) -> str:
        """从响应里取画廊引用（ulgid=… 优先，其次 gid=…），用于继续追加分卷。"""
        text = f"{getattr(resp, 'url', '')}\n{getattr(resp, 'text', '')}"
        for key in ("ulgid", "gid"):
            match = re.search(rf"managegallery\?[^\"'\s<>]*?{key}=(\d+)", text)
            if match:
                return f"{key}={match.group(1)}"
        for key in ("ulgid", "gid"):
            match = re.search(rf"[?&]{key}=(\d+)", text)
            if match:
                return f"{key}={match.group(1)}"
        match = re.search(r"upload\.e-hentai\.org/(?:g|gallery)/(\d+)/", text)
        if match:
            return f"gid={match.group(1)}"
        return ""

    def _append_form(self, ref: str, chapter: Chapter) -> tuple[str, dict[str, str]]:
        """读画廊管理页，拿“追加文件”的表单（action + 字段），供分卷续传使用。"""
        page = self.http.get(
            urljoin(UPLOAD_PAGE_URL, f"/managegallery?{ref}"),
            headers={"Referer": UPLOAD_PAGE_URL},
        )
        form = _parse_upload_page(page.text)
        if not form:
            raise PublisherError(
                f"e-hentai 分卷上传：管理页（{ref}）没解析到上传表单，无法继续追加分卷"
            )
        # 管理页自带新的 progresskey/apiuid/apikey，进度查询要跟着换
        self._progress_ctx = self._progress_context(page.text, form)
        return urljoin(UPLOAD_PAGE_URL, form.action or UPLOAD_PAGE_URL), self._fill(
            form, chapter
        )

    # ---------- 站点自带的上传进度 ----------

    def _progress_context(self, page_html: str, form: "_Form") -> dict[str, str]:
        """进度查询需要的三个值：上传页里的 apiuid/apikey + 表单里的 progresskey。"""
        field = form.by_name("PHP_SESSION_UPLOAD_PROGRESS")
        key = str(field.value or "").strip() if field is not None else ""
        uid = re.search(r"\bapiuid\s*=\s*\"?(\d+)\"?", page_html)
        apikey = re.search(r"\bapikey\s*=\s*\"([^\"]+)\"", page_html)
        return {
            "progresskey": key,
            "apiuid": uid.group(1) if uid else "",
            "apikey": apikey.group(1) if apikey else "",
        }

    @staticmethod
    def _progress_numbers(text: str) -> tuple[int, int, str]:
        """从站点进度文案里抠出 (done, total, unit)：支持百分比与「12.3 MB / 45.6 MB」。"""
        percent = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
        if percent:
            return int(round(float(percent.group(1)))), 100, ""
        size = re.search(
            r"([\d.]+)\s*(B|KB|MB|GB)\s*/\s*([\d.]+)\s*(B|KB|MB|GB)",
            text or "",
            re.I,
        )
        if size:
            scale = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}
            done = float(size.group(1)) * scale[size.group(2).lower()]
            total = float(size.group(3)) * scale[size.group(4).lower()]
            return int(done), int(total), "bytes"
        return 0, 0, ""

    def _poll_upload_progress(
        self, ctx: dict, stop: threading.Event, chapter_key: str
    ) -> None:
        """后台线程：轮询站点自己的上传进度接口，转成前端进度事件。

        用独立的 HttpClient（新 session）：上传请求正占着主 session，
        同一个 session 被两个线程同时用会互相影响。
        """
        client = HttpClient(
            cookies=self.cfg.cookies,
            timeout=15.0,
            retries=0,
            log_prefix="ehentai-progress",
            proxy_url=str(self.cfg.get("proxy_url") or self.common.proxy_url or ""),
            use_system_proxy=bool(
                self.cfg.get("use_system_proxy", self.common.use_system_proxy)
            ),
        )
        payload = {
            "method": "uploadprogress",
            "apiuid": ctx.get("apiuid", ""),
            "apikey": ctx.get("apikey", ""),
            "progresskey": ctx.get("progresskey", ""),
        }
        try:
            first = True
            while not stop.is_set():
                if not first and stop.wait(PROGRESS_POLL_INTERVAL):
                    return
                first = False
                try:
                    resp = client.post(
                        UPLOAD_API_URL,
                        json=payload,
                        headers={
                            "Referer": UPLOAD_PAGE_URL,
                            "Content-Type": "application/json",
                        },
                        retry=False,
                        timeout=15.0,
                    )
                    data = resp.json()
                except Exception as exc:  # 进度只是锦上添花，失败不打断上传
                    self.log.debug("读取站点上传进度失败：%s", exc)
                    continue
                text = strip_tags(str(data.get("progress") or ""))
                if text:
                    done, total, unit = self._progress_numbers(text)
                    self.progress(
                        "upload",
                        done,
                        total,
                        f"{self._progress_volume}站点上传进度：{text}",
                        chapter_key=chapter_key,
                        unit=unit,
                    )
                if data.get("done") is not None:
                    return
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover
                pass

    def _post_with_progress(
        self, action: str, *, data: dict, files, volume: tuple[int, int] | None = None
    ):
        """整包上传，同时在后台轮询站点自己的上传进度。"""
        ctx = dict(self._progress_ctx or {})
        stop = threading.Event()
        thread = None
        if volume and volume[1] > 1:
            self._progress_volume = f"第 {volume[0]}/{volume[1]} 卷 · "
        else:
            self._progress_volume = ""
        if ctx.get("progresskey") and ctx.get("apiuid") and ctx.get("apikey"):
            thread = threading.Thread(
                target=self._poll_upload_progress,
                args=(ctx, stop, self._progress_chapter_key),
                daemon=True,
            )
            thread.start()
        try:
            return self.http.post(
                action,
                data=data,
                files=files,
                headers={"Referer": UPLOAD_PAGE_URL},
                allow_redirects=True,
                retry=False,
                timeout=float(self.cfg.get("upload_timeout", 600) or 600),
            )
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=2.0)

    def _upload_names(self, pages) -> list[str]:
        """预览与实际打包共用的上传文件名（顺序与打包完全一致）。"""
        mode = self._upload_mode()

        if mode in ("files", "individual"):
            return [
                f"{index:04d}_{page_path(p).name}" for index, p in enumerate(pages, 1)
            ]
        # 固定至少 3 位：001…999、1000…，字典序与自然序一致（防百页以上错序）
        width = max(3, len(str(len(pages))))
        return [
            f"{index:0{width}d}{page_path(p).suffix.lower()}"
            for index, p in enumerate(pages, 1)
        ]

    def _archive_pages(self, chapter: Chapter) -> list:
        """zip 上传用的页面：**直接用原始文件，不压缩**（不掉画质）。

        只有站点不认识的后缀（如 webp）才会先转成 jpg/png。
        整包体积过大时站点前面的 Cloudflare 会回 413，这种情况只提示、不自动压图
        （压图会掉画质，由用户自己决定是否调整压缩设置）。
        """
        pages = list(chapter.pages)
        odd = sorted(
            {
                page_path(p).suffix.lower()
                for p in pages
                if page_path(p).suffix.lower() not in ARCHIVE_OK_EXTS
            }
        )
        if not odd:
            return pages
        self.log.warning(
            "归档里含站点不支持的格式（%s）：这些页会先转成 jpg/png 再打包",
            "、".join(odd),
        )
        # 明确限定成站点接受的格式，让 webp 之类真正被转成 jpg/png
        return self.prepare_pages(
            chapter, allowed_exts=set(ARCHIVE_OK_EXTS), max_bytes=0
        )

    def full_preview(self, chapter: Chapter) -> list[str]:
        """e-hentai 全文预览：列出将写入 zip/上传的文件名（与实际上传一致）。"""
        from ..comic import page_sequence_warnings
        from ..util import human_size

        mode = self._upload_mode()
        lines = [
            f"发布平台：{self.display_name}",
            f"图库名：{composer.ehentai_title_en(chapter) or chapter.title}",
        ]
        meta = self._meta(chapter)
        category = str(
            meta.get("category") or self.cfg.get("category_label") or "（按上传页选项匹配）"
        )
        lines.append(f"分类：{category}")
        tags = "、".join(self._tags(chapter)) or "（无）"
        lines.append(f"标签：{tags}")
        if mode in ("files", "individual"):
            lines.append(f"上传 {len(chapter.pages)} 张图片（逐张）")
            size_note = "上传时自动压缩至 10MB 内"
        else:
            lines.append(
                f"打包为 ZIP 归档（{len(chapter.pages)} 页），归档内文件名如下，"
                "E 站将按归档内文件名生成页码："
            )
            raw_mb = sum(page_path(p).stat().st_size for p in chapter.pages) / 1048576
            limit = self.zip_max_bytes()
            if limit and raw_mb * 1048576 > limit:
                size_note = (
                    f"原始合计 {raw_mb:.1f} MB 超过单次上传上限 {limit / 1048576:.0f} MB"
                    "（站点 CDN 会 413）→ 这批会先压缩再打包"
                )
            else:
                size_note = f"原图直接打包，不压缩（合计 {raw_mb:.1f} MB）"
        names = self._upload_names(chapter.pages)
        for index, page in enumerate(chapter.pages, 1):
            lines.append(
                f"  [{index:>3}] {names[index - 1]}"
                f"（源文件 {page.name}，{human_size(page.stat().st_size)}；"
                f"{size_note}）"
            )
        warnings = page_sequence_warnings(chapter.pages)
        if warnings:
            lines.append("⚠ 检查发现：")
            for warning in warnings:
                lines.append("  - " + warning)
        else:
            lines.append("✓ 页面顺序连续，未发现重复或明显漏号")
        return lines

    def check(self) -> CheckResult:
        missing = self.missing_cookies()
        if missing:
            return CheckResult(self.key, False, f"缺少 Cookie：{', '.join(missing)}")
        try:
            resp = self.http.get(UPLOAD_PAGE_URL)
        except Exception as exc:
            if _is_connect_error(exc):
                return CheckResult(
                    self.key,
                    False,
                    f"网络请求失败（连不上 upload.e-hentai.org）：{exc}\n{CONNECT_HINT}",
                )
            return CheckResult(self.key, False, f"网络请求失败：{exc}")
        if not _is_upload_page_url(resp.url):
            self.http._dump(resp, tag="ehentai-check-page")
            return CheckResult(
                self.key,
                False,
                f"访问上传页时被跳转到了 {resp.url}（不是 upload.e-hentai.org）。"
                "通常是 Cookie 已失效/未登录，或代理把请求带到了错误站点；"
                "页面已保存到 output/debug（文件名含 ehentai-check-page），"
                "请先刷新 Cookie 后重试。",
            )
        form = _parse_upload_page(resp.text)
        if form:
            return CheckResult(self.key, True, "已登录，可上传（上传页表单解析成功）")
        text = _plain_text(resp.text)
        if re.search(r"log\s*in|sign\s*in|登录", text, re.I):
            return CheckResult(self.key, False, "Cookie 无效或未登录，请检查 ipb_member_id / ipb_pass_hash")
        # 结构与预期不符：把原始页面转存，方便排查/适配
        self.http._dump(resp, tag="ehentai-check-page")
        return CheckResult(
            self.key,
            False,
            "上传页结构与预期不同，无法自动识别表单"
            "（原始页面已保存到 output/debug，文件名含 ehentai-check-page，"
            "可直接把该文件发给我协助适配）",
        )

    def plan(self, chapter: Chapter) -> list[str]:
        meta = self._meta(chapter)
        return [
            f"图库名：{composer.ehentai_title_en(chapter) or chapter.title}",
            f"分类：{meta.get('category') or self.cfg.get('category_label') or '（按上传页选项匹配）'}",
            f"标签：{', '.join(self._tags(chapter)) or '（无）'}",
            f"上传 {len(chapter.pages)} 页图片",
        ]

    def _tags(self, chapter: Chapter) -> list[str]:
        meta = self._meta(chapter)
        tags = (
            list(chapter.tags)
            + list(meta.get("extra_tags") or meta.get("tags") or self.cfg.get("extra_tags") or [])
        )
        result: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            tag = str(tag).strip()
            if not tag or tag.lower() in seen:
                continue
            seen.add(tag.lower())
            result.append(tag)
        has_language = any(re.match(r"(language|日本語|中文|english):", t, re.I) for t in result)
        if not has_language:
            result.insert(0, "language:chinese")
        return result

    def _select_value(self, field: _Field, wanted: str, default_index: int = 1) -> str:
        """按选项文本模糊匹配 select 的 value；找不到则尝试默认项。"""
        wanted = (wanted or "").strip()
        if wanted:
            for value, label in field.options:
                if wanted.lower() in label.lower() or value.lower() == wanted.lower():
                    return value
        if field.options:
            # 常见第一项是占位/无分类，从第二项开始挑一个非空标签
            for value, label in field.options[default_index:]:
                if label.strip():
                    return value
            return field.options[0][0]
        return ""

    def _language_value(self, field: _Field, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        wanted = str(meta.get("language") or self.cfg.get("language_label") or "").strip()
        if wanted:
            for value, label in field.options:
                if wanted.lower() in label.lower() or value.lower() == wanted.lower():
                    return value
        # 未显式配置时，保留页面预选的语言（例如真实页默认 Japanese / No Text）
        if field.selected:
            return field.selected
        # 默认中文
        for value, label in field.options:
            text = label.lower()
            if "中文" in label or "chinese" in text or "zh" == value.lower():
                return value
        if field.options:
            return self._select_value(field, "", default_index=1)
        return ""

    def _mapping_rows(self) -> list[dict]:
        """标题/评论一律按新格式自动组合；旧 field_map 仅作兼容不再优先。"""
        return [dict(row) for row in DEFAULT_FIELD_ROWS]

    def _source_value(self, chapter: Chapter, source: str) -> str:
        """把 source 指令解析成要填进文本框的字符串。"""
        meta = self._meta(chapter)
        source = (source or "").strip()
        if not source:
            return ""
        if source == "title":
            return str(chapter.title or "").strip()
        if source == "series":
            return str(
                meta.get("work_name")
                or chapter.raw.get("series_title")
                or chapter.raw.get("title")
                or chapter.title
                or ""
            ).strip()
        if source == "author":
            return str(chapter.author or "").strip()
        if source == "description":
            return str(chapter.description or "").strip()
        if source == "tags":
            return " ".join(self._tags(chapter))
        if source == "auto:title_en":
            return composer.ehentai_title_en(chapter)
        if source == "auto:title_jp":
            return composer.ehentai_title_jp(chapter)
        if source == "auto:comment":
            return composer.ehentai_comment(chapter)
        if source.startswith("meta:"):
            key = source[len("meta:"):].strip()
            value = meta.get(key)
            if value is None:
                value = self.cfg.get(key)
            if value is None:
                value = chapter.raw.get(key)
            return "" if value is None else str(value).strip()
        if source.startswith("text:"):
            return source[len("text:"):].strip()
        return ""

    def _find_field_for_row(
        self, form: _Form, row: dict, used: set[str]
    ) -> _Field | None:
        """按 field 精确名找输入框；找不到再按 match 关键词在剩余文本框中找。"""
        wanted = str(row.get("field") or "").strip()
        if wanted:
            for field in form.fields:
                if field.type in ("file", "hidden"):
                    continue
                if field.name and field.name.lower() == wanted.lower():
                    return field
        patterns = [str(p).strip().lower() for p in row.get("match") or [] if str(p).strip()]
        if patterns:
            for field in form.fields:
                if field.type in ("file", "hidden"):
                    continue
                if not field.name or field.name in used:
                    continue
                hay = f"{field.name} {field.type}".lower()
                if any(p in hay for p in patterns):
                    return field
        return None

    def _select_for_row(self, field: _Field, chapter: Chapter, source: str) -> str:
        """下拉框按用户配置/元数据做选项匹配。"""
        meta = self._meta(chapter)
        if source == "category":
            return self._select_value(
                field, str(meta.get("category") or self.cfg.get("category_label") or "")
            )
        if source == "language":
            return self._language_value(field, chapter)
        if source == "rating":
            return self._select_value(
                field, str(meta.get("rating") or self.cfg.get("rating_label") or "")
            )
        return ""

    def _auto_fill_remaining(self, form: _Form, chapter: Chapter, data: dict[str, str]) -> None:
        """未被映射覆盖的字段做保守兜底：只填能明确识别的，绝不乱猜填默认值。"""
        meta = self._meta(chapter)
        for field in form.fields:
            if not field.name or field.type == "file":
                continue
            if field.name in data:
                continue
            if field.type == "hidden":
                data[field.name] = field.value
                continue
            name = field.name.lower()
            if field.type in ("text", "textarea", ""):
                # 真实上传页的 gname_en / gname_jp / ulcomment 已由默认映射处理，
                # 这里只兜底识别非常明确的旧字段名
                if name in ("name", "title", "gallery_name", "gname_en") or name.endswith("_title"):
                    data[field.name] = chapter.title[:255]
                elif "comment" in name or "desc" in name:
                    data[field.name] = str(
                        meta.get("comment")
                        or self.cfg.get("comment")
                        or chapter.description
                        or ""
                    )
                elif any(k in name for k in ("jpn", "japanese", "original", "jp_")):
                    value = str(
                        meta.get("title_jpn")
                        or meta.get("title_original")
                        or self.cfg.get("title_jpn")
                        or ""
                    ).strip()
                    if value:
                        data[field.name] = value
                # 其它未知文本框：不填，避免把默认值/占位符误当成内容提交
            elif field.type == "radio":
                wanted = str(meta.get("langtype") or self.cfg.get("langtype") or "").strip()
                if not wanted and field.name.lower() == "langtype":
                    # 汉化上传场景默认“Translated（汉化）”，可配置为 0/2
                    wanted = "1"
                if wanted:
                    if field.value == wanted:
                        data[field.name] = field.value
                elif field.checked:
                    # 浏览器会提交页面上默认勾选的那一项
                    data[field.name] = field.value
            elif field.type == "checkbox":
                # tos 必须勾选（无它无法上传）；langctl 在汉化(1)时自动勾选，
                # 表明“专业翻译者翻译”，避免被站点标成机翻/渣翻；页面本身勾上的也照常提交
                if name == "tos" or field.checked:
                    data[field.name] = field.value or "on"
                elif name == "langctl" and str(data.get("langtype", "")) == "1":
                    data[field.name] = field.value or "on"
            elif field.type == "select":
                if name in ("category", "cat"):
                    data[field.name] = self._select_value(
                        field, str(meta.get("category") or self.cfg.get("category_label") or "")
                    )
                elif name in ("language", "lang", "langtag"):
                    data[field.name] = self._language_value(field, chapter)
                elif "rating" in name:
                    data[field.name] = self._select_value(
                        field, str(meta.get("rating") or self.cfg.get("rating_label") or "")
                    )
                elif field.selected:
                    # 未配置的 select（如个人文件夹 folderid）保持页面当前选项
                    data[field.name] = field.selected
                elif field.options:
                    data[field.name] = field.options[0][0]
                else:
                    data[field.name] = ""

    def _fill(self, form: _Form, chapter: Chapter) -> dict[str, str]:
        data: dict[str, str] = {}
        used: set[str] = set()
        for row in self._mapping_rows():
            field = self._find_field_for_row(form, row, used)
            if field is None or field.name in data:
                continue
            source = str(row.get("source") or "")
            if field.type == "select":
                value = self._select_for_row(field, chapter, source)
            else:
                value = self._source_value(chapter, source)
            if value:
                # 文本框按 HTML 常见上限做截断保护
                if field.type in ("text", "") and len(value) > 1000:
                    value = value[:1000]
                data[field.name] = value
                used.add(field.name)
        self._auto_fill_remaining(form, chapter, data)
        return data

    def publish(self, chapter: Chapter) -> PublishResult:
        self.require_cookies()
        if not chapter.pages:
            return PublishResult.skipped(self.key, chapter, "没有图片")
        try:
            self.progress("prepare", 0, len(chapter.pages),
                          "开始准备 e-hentai 上传文件", chapter_key=chapter.key)
            page = self.http.get(UPLOAD_PAGE_URL)
            if not _is_upload_page_url(page.url):
                self.http._dump(page, tag="ehentai-upload-page")
                raise PublisherError(
                    "e-hentai 上传页请求被跳转到了 " + page.url
                    + "，通常是 Cookie 已失效/未登录；请先刷新 Cookie 后重试"
                    "（原始页面已保存到 output/debug，文件名含 ehentai-upload-page）"
                )
            form = _parse_upload_page(page.text)
            if not form:
                self.http._dump(page, tag="ehentai-upload-page")
                raise PublisherError(
                    "e-hentai 上传页表单解析失败：请检查 Cookie 是否有效、账号是否有上传资格。"
                    "（原始页面已保存到 output/debug，文件名含 ehentai-upload-page）"
                )
            file_fields = [f.name for f in form.fields if f.type == "file"]
            file_name = file_fields[0] if file_fields else "sfile[]"

            data = self._fill(form, chapter)
            # 站点自带的上传进度：uploadprogress 接口要的 apiuid/apikey/progresskey
            self._progress_ctx = self._progress_context(page.text, form)
            self._progress_chapter_key = chapter.key
            # zip 模式直接用原始文件（站点对归档没有单图大小限制，不必压缩）；
            # files 模式仍走压缩管线（站点对单张有大小限制）
            if self._upload_mode() in ("files", "individual"):
                pages = self.prepare_pages(chapter, max_bytes=0)
            else:
                pages = self._archive_pages(chapter)
            action = urljoin(UPLOAD_PAGE_URL, form.action or UPLOAD_PAGE_URL)
            resp = self._upload_files(action, data, file_name, pages, chapter)
            return self._interpret_response(resp, chapter, len(pages))
        except HttpError as exc:
            if _is_connect_error(exc):
                raise PublisherError(
                    f"e-hentai 请求连不上（连接超时/被拒绝）：{exc}\n{CONNECT_HINT}"
                ) from exc
            raise
        finally:
            self.cleanup_prepared(chapter)

    def _upload_files(
        self,
        action: str,
        data: dict[str, str],
        file_name: str,
        pages,
        chapter: Chapter,
    ):
        """按配置上传：zip（推荐，站点支持归档整包）或逐张多文件（旧行为）。"""
        mode = str(self.cfg.get("upload_mode") or "").strip().lower()
        if mode not in ("files", "individual"):
            return self._upload_zip(action, data, file_name, pages, chapter)
        # files：逐张多文件（兼容站点旧流程与本地测试）
        names = self._upload_names(pages)
        files: list[tuple[str, tuple[str, object, str]]] = []
        handles: list = []
        try:
            self.progress("upload", 0, len(pages),
                          f"正在逐张上传 {len(pages)} 个文件（可能较慢，请耐心等待）")
            for index, page_item in enumerate(pages, 1):
                mime = mimetypes.guess_type(page_item.path.name)[0] or "application/octet-stream"
                handle = open(page_item.path, "rb")
                handles.append(handle)
                files.append(
                    (
                        file_name,
                        (names[index - 1], handle, mime),
                    )
                )
            self.log.info("POST 上传 %d 个文件到 %s", len(files), action)
            return self._post_with_progress(action, data=data, files=files)
        finally:
            for handle in handles:
                handle.close()

    def _upload_zip(
        self,
        action: str,
        data: dict[str, str],
        file_name: str,
        pages,
        chapter: Chapter,
    ):
        """把页面打包成 ZIP 归档后整包上传（e-hentai 官方接受 Archive 格式）。

        规则（按 ehwiki）：单层无子目录、文件名全局唯一、Deflate/Store、不加密。
        图片一律用原始文件，**不压缩**（不掉画质）。

        归档超过单次上传上限（`zip_max_mb`，默认 90MB）时，站点前面的 Cloudflare
        会回 `413 Payload Too Large`；这时按体积自动分卷，每卷仍是一次普通的整包
        POST，依次追加到同一个画廊（画质不变，只是分成几个 zip）。
        """
        names = self._upload_names(pages)
        raw_bytes = sum(page_path(p).stat().st_size for p in pages)
        limit = self.zip_max_bytes()
        volumes = (
            self._split_pages_by_size(pages, limit)
            if (limit and self.zip_split_uploads())
            else [list(pages)]
        )
        total_volumes = len(volumes)
        if total_volumes > 1:
            self.log.warning(
                "图片合计 %.1f MB，超过单次上传上限 %.0f MB（站点 CDN 会回 413）："
                "自动分成 %d 卷依次上传到同一画廊，原图不压缩",
                raw_bytes / 1048576,
                limit / 1048576,
                total_volumes,
            )
        offset = 0
        resp = None
        for index, group in enumerate(volumes, 1):
            part_names = names[offset:offset + len(group)]
            offset += len(group)
            zip_path = self._build_zip(group, part_names)
            try:
                zip_mb = os.path.getsize(zip_path) / 1048576
                group_mb = sum(page_path(p).stat().st_size for p in group) / 1048576
                self.progress(
                    "zip",
                    index,
                    total_volumes,
                    f"第 {index}/{total_volumes} 卷打包完成"
                    f"（{len(group)} 页，原始 {group_mb:.1f} MB → 归档 {zip_mb:.1f} MB，未压缩）",
                )
                if index > 1:
                    ref = self._gallery_ref(resp)
                    if not ref:
                        raise PublisherError(
                            "e-hentai 分卷上传：没拿到画廊 id，无法追加后续分卷"
                        )
                    action, data = self._append_form(ref, chapter)
                    self.log.info(
                        "第 %d/%d 卷追加到 %s（%d 页，%.1f MB）",
                        index,
                        total_volumes,
                        ref,
                        len(group),
                        zip_mb,
                    )
                else:
                    self.log.info(
                        "POST 上传 zip（第 1/%d 卷，%d 页，%s）到 %s",
                        total_volumes,
                        len(group),
                        zip_path,
                        action,
                    )
                with open(zip_path, "rb") as fh:
                    resp = self._post_with_progress(
                        action,
                        data=data,
                        files=[(file_name, ("gallery.zip", fh, "application/zip"))],
                        volume=(index, total_volumes),
                    )
            finally:
                try:
                    Path(zip_path).unlink(missing_ok=True)
                except OSError:
                    pass
        return resp

    def _build_zip(self, pages: list, names: list[str]) -> str:
        """按“单层无子目录 + 页序文件名”打一个 zip，返回临时文件路径。"""
        fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="ehentai_")
        os.close(fd)  # zipfile 会用路径重新打开，fd 只占资源
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for page_item, arcname in zip(pages, names):
                zf.write(page_path(page_item), arcname=arcname)
        return zip_path

    def _interpret_response(self, resp, chapter: Chapter, page_count: int) -> PublishResult:
        final_url = resp.url
        text = resp.text

        # 1) 成功：跳转到画廊或上传管理页，或页面里有画廊链接
        gallery_match = re.search(
            r"https?://[^\"'\s<>]*?/(?:g|gallery)/[0-9a-f]+/\d+/?|https?://[^\"'\s<>]*?/uploader/[^\"'\s<>]+",
            final_url + "\n" + text,
            re.I,
        )
        if gallery_match:
            gallery_url = gallery_match.group(0).rstrip("/")
            return PublishResult.ok(self.key, chapter, url=gallery_url, message=f"上传完成，共 {page_count} 页")

        # e-hentai 是“先建草稿传文件 → 再发布”的两步流程：
        # 上传成功后的响应是 managegallery?ulgid=… 的草稿管理页（Unpublished），
        # 需要再访问 act=publish 才算正式发布。
        draft_result = self._publish_created_draft(resp, chapter, page_count)
        if draft_result is not None:
            return draft_result

        plain = _plain_text(text)
        # 2) 失败：页面中的错误块（.d 类）通常是提示
        errors = re.findall(r'<div class="d">(.*?)</div>', text, re.S | re.I)
        if errors:
            message = re.sub(r"<[^>]+>", " ", errors[0])
            message = re.sub(r"\s+", " ", message).strip()
            return PublishResult.failed(self.key, chapter, message or "e-hentai 拒绝了本次上传")

        if re.search(r"success|已上传|上传成功|received", plain, re.I):
            return PublishResult.ok(
                self.key,
                chapter,
                url=final_url,
                message="上传请求已被接收，稍后可在 My Uploads 页面确认",
            )
        # 3) 未知响应：保存调试信息并让用户检查
        self.http._dump(resp, tag="ehentai-unknown")
        return PublishResult.failed(
            self.key,
            chapter,
            "e-hentai 返回了无法自动识别的页面（已保存到 output/debug），请检查上传资格与图片合规性",
        )

    def _publish_created_draft(
        self, resp, chapter: Chapter, page_count: int
    ) -> PublishResult | None:
        """上传 POST 返回草稿管理页时：确认文件已入册，并可选继续正式发布。"""
        text = resp.text
        match = re.search(r"ulgid=(\d+)", resp.url + "\n" + text, re.I)
        if not match:
            return None
        ulgid = match.group(1)
        # e-hentai 文件入库是异步的：响应页可能先报 “too small/仍在处理”，
        # 稍后页数才出现。以“管理页实际在册页数”为准，而不是瞬时提示。
        manage_url = self._manage_url_for(resp, ulgid, text)
        if manage_url is None:
            return None
        # 在册页数 = 管理页上 pagesel_* 输入框数量
        count = self._count_registered_pages(manage_url, text)
        if count == 0:
            # 首页面里就有页码选择框也算已入册；完全没有则等待异步入库
            self.progress(
                "wait",
                0,
                page_count,
                "e-hentai 正在处理上传文件，等待草稿入库…",
                chapter_key=chapter.key,
            )
            deadline = time.time() + float(self.cfg.get("upload_wait", 90) or 90)
            while time.time() < deadline:
                time.sleep(4)
                count = self._count_registered_pages(manage_url)
                if count > 0:
                    self.progress(
                        "wait",
                        count,
                        page_count,
                        f"站点已确认在册 {count}/{page_count} 页",
                        chapter_key=chapter.key,
                    )
                    break
        if count == 0:
            return PublishResult.failed(
                self.key,
                chapter,
                "e-hentai 上传后草稿里仍没有图片（站点可能拒绝了文件，"
                "如 too small / 格式限制）。可改用 zip 整包上传，或手动在浏览器上传后重试。",
                draft_ulgid=ulgid,
            )
        if count < page_count:
            self.log.warning(
                "e-hentai 草稿 %s 在册 %d/%d 页（部分可能被站点拒绝）",
                ulgid,
                count,
                page_count,
            )
        self.progress(
            "wait",
            count,
            page_count,
            f"草稿已入库 {count}/{page_count} 页",
            chapter_key=chapter.key,
        )
        actual_pages = count
        if not self.cfg.get("publish_after_upload", True):
            return PublishResult.ok(
                self.key,
                chapter,
                url=manage_url,
                message=(
                    f"已创建草稿并上传 {actual_pages}/{page_count} 页（未发布）。"
                    "可在 My Uploads 中手动发布："
                    + manage_url
                ),
                pages=actual_pages,
                draft_ulgid=ulgid,
            )

        self.log.info("草稿 %s 在册 %d/%d 页，继续正式发布", ulgid, actual_pages, page_count)
        self.progress(
            "publish",
            actual_pages,
            page_count,
            "正在正式发布草稿…",
            chapter_key=chapter.key,
        )
        separator = "&" if "?" in manage_url else "?"
        publish_url = manage_url + separator + "act=publish&from=gallery"
        try:
            pub = self.http.get(
                publish_url,
                headers={"Referer": manage_url},
                allow_redirects=True,
            )
        except Exception as exc:
            return PublishResult.partial(
                self.key,
                chapter,
                url=manage_url,
                message=(
                    f"图片已上传到草稿（ulgid={ulgid}），但发布请求失败：{exc}。"
                    "请到 My Uploads 手动发布"
                ),
                pages=actual_pages,
                draft_ulgid=ulgid,
            )
        pub_text = pub.text
        gallery = re.search(
            r"https?://[^\"'\s<>]*?/(?:g|gallery)/[0-9a-f]+/\d+",
            pub.url + "\n" + pub_text,
            re.I,
        )
        if gallery:
            url = gallery.group(0).rstrip("/")
            return PublishResult.ok(
                self.key,
                chapter,
                url=url,
                message=f"上传并发布完成，共 {actual_pages} 页",
                pages=actual_pages,
                draft_ulgid=ulgid,
            )
        pub_plain = _plain_text(pub_text)
        if re.search(r"published|已发布|publish success|successfully published", pub_plain, re.I):
            return PublishResult.ok(
                self.key,
                chapter,
                url=manage_url,
                message=(
                    f"已上传并触发发布（ulgid={ulgid}），最终链接以 My Uploads 为准，"
                    f"在册 {actual_pages} 页"
                ),
                pages=actual_pages,
                draft_ulgid=ulgid,
            )
        # 发布后的页面形态未能识别：图片已上传成功，保留管理页链接
        self.http._dump(pub, tag="ehentai-publish")
        return PublishResult.partial(
            self.key,
            chapter,
            url=manage_url,
            message=(
                f"图片已上传到草稿（ulgid={ulgid}），发布确认页面未能自动识别"
                "（已保存 output/debug，文件名含 ehentai-publish）。"
                "请到 My Uploads 检查并手动发布"
            ),
            pages=actual_pages,
            draft_ulgid=ulgid,
        )

    def _manage_url_for(
        self, resp, ulgid: str, text: str
    ) -> str | None:
        """草稿管理地址：返回页 form action 优先，其次按 ulgid 拼接。"""
        action = re.search(r'<form[^>]*\saction="([^"]+)"', text, re.I)
        if action:
            manage_url = urljoin(resp.url, re.sub(r"&amp;", "&", action.group(1)))
        else:
            manage_url = urljoin(UPLOAD_PAGE_URL, f"managegallery?ulgid={ulgid}")
        if "ulgid=" not in manage_url:
            manage_url = urljoin(resp.url, f"managegallery?ulgid={ulgid}")
        return manage_url

    def _count_registered_pages(
        self, manage_url: str, first_text: str | None = None
    ) -> int:
        """统计管理页里已在册的页数（pagesel_* 输入框数量）。"""
        import html.parser

        def _count(text: str) -> int:
            return len(
                re.findall(
                    r'<input[^>]*\sid="pagesel_[^"]*"[^>]*>',
                    text,
                    re.I,
                )
            )

        count = _count(first_text) if first_text else 0
        if count:
            return count
        try:
            page = self.http.get(manage_url, headers={"Referer": manage_url})
        except Exception as exc:  # 网络抖动不算失败，返回 0 让上层继续轮询
            self.log.warning("查询草稿管理页失败：%s", exc)
            return 0
        return _count(page.text)


def _parse_upload_page(html_text: str) -> _Form | None:
    parser = _FormParser()
    try:
        parser.feed(html_text)
    except Exception:
        return None
    return parser.upload_form()


def _plain_text(html_text: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html_text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()
