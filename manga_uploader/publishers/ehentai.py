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
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..models import Chapter, CheckResult, PublishResult
from .base import BasePublisher, PublisherError

UPLOAD_PAGE_URL = "https://upload.e-hentai.org/managegallery?act=new"
CHECK_PAGE_URL = "https://e-hentai.org/home.php"


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
    def __init__(self, name: str, type_: str, value: str = "", options: list[tuple[str, str]] | None = None):
        self.name = name
        self.type = type_
        self.value = value
        self.options = options or []  # [(value, 显示文本)]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Field {self.type} {self.name}={self.value!r} options={len(self.options)}>"


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
            self._field = _Field(attr.get("name", ""), attr.get("type", "text"), attr.get("value", ""))
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

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._in_option:
            self._in_option = False
            if self._field and self._field.type == "select":
                self._field.options.append((self._option_value, "".join(self._option_text).strip()))
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


class EhentaiPublisher(BasePublisher):
    key = "ehentai"
    display_name = "e-hentai"

    def check(self) -> CheckResult:
        missing = self.missing_cookies()
        if missing:
            return CheckResult(self.key, False, f"缺少 Cookie：{', '.join(missing)}")
        try:
            resp = self.http.get(UPLOAD_PAGE_URL)
        except Exception as exc:
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
            f"图库名：{chapter.title}",
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
        # 默认中文
        for value, label in field.options:
            text = label.lower()
            if "中文" in label or "chinese" in text or "zh" == value.lower():
                return value
        if field.options:
            return self._select_value(field, "", default_index=1)
        return ""

    def _fill(self, form: _Form, chapter: Chapter) -> dict[str, str]:
        meta = self._meta(chapter)
        data: dict[str, str] = {}
        for field in form.fields:
            if not field.name or field.type == "file":
                continue
            if field.type == "hidden":
                data[field.name] = field.value
            elif field.type in ("text", "textarea", ""):
                if field.name == "name":
                    data[field.name] = chapter.title[:255]
                elif field.name == "comment":
                    data[field.name] = str(
                        meta.get("comment") or self.cfg.get("comment") or chapter.description or ""
                    )
                elif field.name == "tags":
                    data[field.name] = " ".join(self._tags(chapter))
                else:
                    data[field.name] = field.value
            elif field.type == "select":
                if field.name == "category":
                    data[field.name] = self._select_value(
                        field, str(meta.get("category") or self.cfg.get("category_label") or "")
                    )
                elif field.name == "language":
                    data[field.name] = self._language_value(field, chapter)
                elif field.name == "rating":
                    data[field.name] = self._select_value(
                        field, str(meta.get("rating") or self.cfg.get("rating_label") or "")
                    )
                elif field.options:
                    data[field.name] = field.options[0][0]
                else:
                    data[field.name] = ""
        return data

    def publish(self, chapter: Chapter) -> PublishResult:
        self.require_cookies()
        if not chapter.pages:
            return PublishResult.skipped(self.key, chapter, "没有图片")
        try:
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
            pages = self.prepare_pages(chapter, max_bytes=0)
            action = urljoin(UPLOAD_PAGE_URL, form.action or UPLOAD_PAGE_URL)

            # 文件用元组列表以支持同名多文件字段（sfile[]）
            files: list[tuple[str, tuple[str, object, str]]] = []
            handles: list = []
            try:
                for index, page_item in enumerate(pages, 1):
                    mime = mimetypes.guess_type(page_item.path.name)[0] or "application/octet-stream"
                    handle = open(page_item.path, "rb")
                    handles.append(handle)
                    files.append(
                        (
                            file_name,
                            (f"{index:04d}_{page_item.path.name}", handle, mime),
                        )
                    )

                self.log.info("POST 上传 %d 个文件到 %s", len(files), action)
                resp = self.http.post(
                    action,
                    data=data,
                    files=files,
                    headers={"Referer": UPLOAD_PAGE_URL},
                    allow_redirects=True,
                )
            finally:
                for handle in handles:
                    handle.close()

            return self._interpret_response(resp, chapter, len(pages))
        finally:
            self.cleanup_prepared(chapter)

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
