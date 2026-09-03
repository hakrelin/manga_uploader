"""B站（哔哩哔哩）发布。

默认发布“专栏文章”（publish_mode=article）：
1. 逐张上传正文图片（接口与字段各版本略有差异，程序按候选顺序尝试）：
   - POST https://api.bilibili.com/x/article/creative/article/upimage
   - POST https://api.bilibili.com/x/article/creative/article/upcover
   multipart 字段 file 或 binary + csrf，成功返回 {code:0, data:{url}}。
2. 先保存草稿：POST /x/article/creative/draft/addupdate（不传 aid），
   拿回 data.aid；
3. 再正式提交：POST /x/article/creative/article/submit（带 aid）。

单张正文图片限制 jpg/png、≤5MB；单篇专栏图片数默认上限
max_article_pages=100，超出自动拆成多篇专栏。

旧版“图文动态”（publish_mode=dynamic）逻辑保留：
- 图片上传：POST /x/dynamic/feed/draw/upload_bfs
- 图文动态：POST /x/dynamic/feed/create/dyn，单条最多 9 张，超限拆条。
"""

from __future__ import annotations

import html
import json
import mimetypes
import random
import time
from urllib.parse import quote

from ..models import Chapter, CheckResult, PublishResult
from .. import composer
from ..util import chunk_list
from .base import BasePublisher, PublisherError

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# 图文动态（旧）
DYNAMIC_UPLOAD_IMAGE_URL = "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs"
CREATE_DYN_URL = "https://api.bilibili.com/x/dynamic/feed/create/dyn"

# 专栏文章
ARTICLE_UPIMAGE_URL = "https://api.bilibili.com/x/article/creative/article/upimage"
ARTICLE_UPCOVER_URL = "https://api.bilibili.com/x/article/creative/article/upcover"
ARTICLE_DRAFT_URL = "https://api.bilibili.com/x/article/creative/draft/addupdate"
ARTICLE_SUBMIT_URL = "https://api.bilibili.com/x/article/creative/article/submit"

ARTICLE_REFERER = "https://member.bilibili.com/platform/upload/text"
# 单张正文图片限制 5MB，允许 jpg/png
ARTICLE_MAX_BYTES = 5 * 1024 * 1024
ARTICLE_ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}


class BilibiliPublisher(BasePublisher):
    key = "bilibili"
    display_name = "B站"

    @property
    def max_pages_per_post(self) -> int:
        # 图文动态单条上限（仅 publish_mode=dynamic 使用）
        return max(1, int(self.cfg.get("max_pages_per_post", 9)))

    @property
    def article_max_pages(self) -> int:
        # 单篇专栏图片数上限
        return max(1, int(self.cfg.get("max_article_pages", 100)))

    @property
    def csrf(self) -> str:
        token = self.cfg.cookies.get("bili_jct", "")
        if not token:
            raise PublisherError("缺少 Cookie：bili_jct")
        return token

    def _mode(self, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        return str(
            meta.get("publish_mode") or self.cfg.get("publish_mode") or "article"
        ).strip().lower()

    def _setting(self, chapter: Chapter, key: str, default=None):
        """平台设置：manga.json platforms.bilibili.<key> 优先，其次 config.yaml。"""
        meta = self._meta(chapter)
        if meta.get(key) is not None:
            return meta.get(key)
        return self.cfg.get(key, default)

    def _title(self, chapter: Chapter) -> str:
        """B站标题：【汉化组】中文标题（平台 meta.title 覆盖优先）。"""
        return composer.platform_title(chapter, self.key)

    def _body_text(self, chapter: Chapter) -> str:
        """B站正文：作者/社团/简介 组合（平台 meta.description 为整段覆盖）。"""
        return composer.platform_body(chapter, self.key)

    # ---------- 公共 ----------

    def check(self) -> CheckResult:
        missing = self.missing_cookies()
        if missing:
            return CheckResult(self.key, False, f"缺少 Cookie：{', '.join(missing)}")
        try:
            data = self.http.get_json(NAV_URL)
        except Exception as exc:
            return CheckResult(self.key, False, f"网络请求失败：{exc}")
        info = data.get("data") or {}
        if data.get("code") == 0 and info.get("isLogin"):
            uname = info.get("uname", "")
            mid = info.get("mid", "")
            return CheckResult(self.key, True, f"已登录：{uname} (UID {mid})")
        message = data.get("message") or "未登录"
        return CheckResult(self.key, False, f"登录失败：{message}")

    def plan(self, chapter: Chapter) -> list[str]:
        if self._mode(chapter) == "article":
            return self._plan_article(chapter)
        return self._plan_dynamic(chapter)

    def publish(self, chapter: Chapter) -> PublishResult:
        self.require_cookies()
        if not chapter.pages:
            return PublishResult.skipped(self.key, chapter, "没有图片")
        if self._mode(chapter) == "article":
            return self._publish_article(chapter)
        return self._publish_dynamic(chapter)

    def full_preview(self, chapter: Chapter) -> list[str]:
        """B站发布前全文预览：展示真实将提交的正文/HTML 结构与图片顺序。"""
        from ..comic import page_sequence_warnings
        from ..util import human_size

        mode = self._mode(chapter)
        lines = [
            "发布平台：B站（" + ("专栏文章" if mode == "article" else "图文动态") + "）",
            f"标题：{self._title(chapter)}",
        ]
        if mode == "article":
            original = int(self._setting(chapter, "original", 1))
            reprint = int(self._setting(chapter, "reprint", 0) or 0)
            tid = int(self._setting(chapter, "tid", 4) or 4)
            category = int(self._setting(chapter, "category", 0) or 0)
            lines.append(
                f"提交参数：tid={tid}（封面模板） category={category} "
                f"original={original} reprint={reprint}"
            )
            pages = len(chapter.pages)
            posts = max(1, -(-pages // self.article_max_pages))
            if posts > 1:
                lines.append(
                    f"⚠ 超过单篇上限 {self.article_max_pages} 张，将拆成 {posts} 篇专栏"
                )
            body = self._body_text(chapter)
            if body:
                lines.append("正文文本（会转成 <p>…</p>）：")
                for part in body.splitlines() or [body]:
                    lines.append("  " + part)
            else:
                lines.append("（无简介文本，正文只有插图）")
            # 预览只列索引与原文件信息，不真实跑图片压缩（发布时才处理，慢且改图）
            pages = chapter.pages
            lines.append(
                f"正文插图共 {len(pages)} 张（每张 1 个 figure，按此顺序插入）："
            )
            for index, page in enumerate(pages, 1):
                lines.append(
                    f"  [{index:>3}] {page.name}（{human_size(page.stat().st_size)}，"
                    "上传时自动压缩至 5MB 内）"
                )
            if pages:
                lines.append("HTML 结构示例（每页相同，仅 src 换成上传后地址）：")
                lines.append("  " + self._figure_html("…上传后返回的图片地址…"))
        else:
            lines.append("动态文案（单条正文，含话题）：")
            for part in str(self._caption(chapter)).splitlines() or [""]:
                lines.append("  " + part)
            groups = max(1, -(-len(chapter.pages) // self.max_pages_per_post))
            lines.append(f"共 {len(chapter.pages)} 张，按 {self.max_pages_per_post} 张/条拆为 {groups} 条")
            self._append_page_preview(lines, chapter)
            return lines

        warnings = page_sequence_warnings(chapter.pages)
        if warnings:
            lines.append("⚠ 源文件检查：")
            for warning in warnings:
                lines.append("  - " + warning)
        return lines

    # ---------- 专栏文章 ----------

    def _plan_article(self, chapter: Chapter) -> list[str]:
        pages = len(chapter.pages)
        posts = max(1, -(-pages // self.article_max_pages))
        rows = [
            f"发布方式：B站专栏文章（{pages} 张正文图片）",
            f"标题：{self._title(chapter)}",
        ]
        if posts > 1:
            rows.append(f"单篇上限 {self.article_max_pages} 张，将拆成 {posts} 篇专栏")
        rows.append(
            f"正文：先存草稿再正式发布；每张图压缩至 5MB 内（允许 jpg/png）"
        )
        body = self._body_text(chapter)
        if body:
            desc = body[:80]
            desc = desc + "…" if len(body) > 80 else desc
            rows.append(f"简介：{desc}")
        reprint = int(self._setting(chapter, "reprint", 0) or 0)
        original = int(self._setting(chapter, "original", 1))
        attr = "原创" if original and not reprint else ("转载" if reprint else "非原创")
        rows.append(f"作品属性：{attr}（original={original}，reprint={reprint}）")
        return rows

    def _upload_article_image(self, page) -> str:
        mime = mimetypes.guess_type(page.path.name)[0] or "image/jpeg"
        # 上传接口/字段随版本变化：优先 upcover（多个长期维护项目验证过），
        # 被拒时自动换备用组合，第一个成功即停
        candidates = (
            (ARTICLE_UPCOVER_URL, "file"),
            (ARTICLE_UPIMAGE_URL, "file"),
            (ARTICLE_UPCOVER_URL, "binary"),
            (ARTICLE_UPIMAGE_URL, "binary"),
        )
        # B站偶发的单图失败：整轮接口都失败后整体重试（次数可配，默认 3）
        attempts = max(1, int(self.cfg.get("upload_attempts", 3) or 3))
        last_error = "未知错误"
        for attempt in range(1, attempts + 1):
            for endpoint, field in candidates:
                try:
                    with open(page.path, "rb") as fh:
                        resp = self.http.post(
                            endpoint,
                            files={field: (page.path.name, fh, mime)},
                            data={"csrf": self.csrf},
                            headers={"Referer": ARTICLE_REFERER},
                        )
                    payload = resp.json()
                except Exception as exc:  # 网络层/非 JSON 失败，换下一候选
                    last_error = str(exc)
                    self.log.warning(
                        "B站 图片 %s 上传候选失败（%s，字段 %s，第 %d/%d 轮）：%s",
                        page.path.name,
                        endpoint,
                        field,
                        attempt,
                        attempts,
                        exc,
                    )
                    continue
                code = payload.get("code")
                if code != 0:
                    last_error = str(payload.get("message") or payload)
                    # 账号/CSRF 问题重试也没用，直接失败
                    if code in (-101, -111):
                        raise PublisherError(
                            f"B站 图片上传失败（code={code}）：{last_error}（请检查 Cookie）"
                        )
                    self.log.warning(
                        "B站 图片 %s 上传候选被拒（%s，字段 %s，第 %d/%d 轮）：%s",
                        page.path.name,
                        endpoint,
                        field,
                        attempt,
                        attempts,
                        last_error,
                    )
                    continue
                url = str(((payload.get("data") or {}).get("url") or "")).strip()
                if not url:
                    last_error = f"响应缺少 url：{payload}"
                    continue
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("http://"):
                    url = "https://" + url[len("http://"):]
                return url
            if attempt < attempts:
                wait = min(1.0 * attempt, 5.0)
                self.log.info(
                    "B站 图片 %s 第 %d/%d 轮全部上传候选失败，%s 秒后自动重试",
                    page.path.name,
                    attempt,
                    attempts,
                    wait,
                )
                time.sleep(wait)
        raise PublisherError(
            f"B站 图片 {page.path.name} 上传失败（已自动重试 {attempts} 轮）：{last_error}"
        )

    @staticmethod
    def _figure_html(url: str) -> str:
        return (
            '<figure contenteditable="false" class="img-box">'
            f'<img src="{url}"/>'
            '<figcaption class="caption" contenteditable=""></figcaption>'
            "</figure>"
        )

    def _article_content(self, chapter: Chapter, urls: list[str]) -> str:
        parts: list[str] = []
        body = self._body_text(chapter)
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            parts.append(f"<p>{html.escape(line)}</p>")
        if not parts:
            parts.append("<p><br/></p>")
        for url in urls:
            parts.append(self._figure_html(url))
        return "".join(parts)

    def _article_post_data(
        self, chapter: Chapter, content: str, cover_url: str = "", aid: int = 0
    ) -> dict:
        original = int(self._setting(chapter, "original", 1))
        reprint = int(self._setting(chapter, "reprint", 0) or 0)
        category = int(self._setting(chapter, "category", 0) or 0)
        tid = int(self._setting(chapter, "tid", 4) or 4)
        data = {
            "title": self._title(chapter)[:64],
            "content": content,
            "category": str(category),
            "list_id": 0,
            "tid": str(tid),
            "reprint": str(reprint),
            "original": str(original),
            "media_id": 0,
            "spoiler": 0,
            "csrf": self.csrf,
        }
        if cover_url:
            # 封面缩略图用正文第一张图（origin_image_urls 与 image_urls 需同时给出）
            data["origin_image_urls"] = cover_url
            data["image_urls"] = cover_url
        if aid:
            data["aid"] = str(aid)
        return data

    @staticmethod
    def _raise_api_error(tag: str, payload: dict) -> None:
        code = payload.get("code")
        tips = {
            -101: "账号未登录，Cookie 可能过期",
            -111: "CSRF 校验失败，请刷新 bili_jct",
            -400: "请求参数被平台拒绝（可能是标题/正文/分类格式问题）",
            -404: "草稿不存在，可能已被删除",
        }
        message = payload.get("message") or payload
        raise PublisherError(
            f"B站 专栏{tag}失败（code={code}）：{message}"
            + (f"。{tips[code]}" if code in tips else "")
        )

    def _draft_article(self, data: dict) -> str:
        resp = self.http.post(ARTICLE_DRAFT_URL, data=data)
        try:
            payload = resp.json()
        except ValueError:
            raise PublisherError(f"B站 草稿接口未返回 JSON：{resp.text[:200]}")
        if payload.get("code") != 0:
            self.http._dump(resp, tag="bilibili-article-draft")
            self._raise_api_error("草稿保存", payload)
        aid = str(((payload.get("data") or {}).get("aid") or "")).strip()
        if not aid:
            self.http._dump(resp, tag="bilibili-article-draft")
            raise PublisherError(f"B站 草稿响应缺少 aid：{payload}")
        return aid

    def _submit_article(self, aid: str, data: dict) -> str:
        data = dict(data)
        data["aid"] = aid
        resp = self.http.post(ARTICLE_SUBMIT_URL, data=data)
        try:
            payload = resp.json()
        except ValueError:
            raise PublisherError(f"B站 提交接口未返回 JSON：{resp.text[:200]}")
        if payload.get("code") != 0:
            self.http._dump(resp, tag="bilibili-article-submit")
            self._raise_api_error("发布", payload)
        # 提交成功也可能只回 code=0；aid 用回草稿 id
        return aid

    def _publish_article(self, chapter: Chapter) -> PublishResult:
        pages = self.prepare_pages(
            chapter, allowed_exts=ARTICLE_ALLOWED_EXTS, max_bytes=ARTICLE_MAX_BYTES
        )
        try:
            groups = chunk_list(pages, self.article_max_pages)
            published: list[str] = []
            errors: list[str] = []
            for index, group in enumerate(groups, 1):
                try:
                    urls: list[str] = []
                    for page_index, page in enumerate(group, 1):
                        self.log.info(
                            "上传专栏图片 %d/%d（第 %d/%d 篇）：%s",
                            page_index,
                            len(group),
                            index,
                            len(groups),
                            page.path.name,
                        )
                        urls.append(self._upload_article_image(page))
                        if self.common.interval_seconds:
                            time.sleep(float(self.common.interval_seconds))

                    content = self._article_content(chapter, urls)
                    data = self._article_post_data(chapter, content, cover_url=urls[0])
                    self.log.info(
                        "保存专栏草稿 %d/%d：%s", index, len(groups), data["title"]
                    )
                    aid = self._draft_article(data)
                    self.log.info("正式发布专栏 %d/%d（aid=%s）", index, len(groups), aid)
                    self._submit_article(aid, data)
                    url = f"https://www.bilibili.com/read/cv{aid}"
                    published.append(url)
                    self.log.info("专栏发布成功：%s", url)
                except PublisherError as exc:
                    errors.append(f"第 {index} 篇专栏失败：{exc}")
                    self.log.error("第 %d 篇专栏失败：%s", index, exc)
                    continue

            if errors:
                return PublishResult.partial(
                    self.key,
                    chapter,
                    url=published[0] if published else "",
                    message=f"部分失败：{'; '.join(errors)}",
                    urls=published,
                    mode="article",
                )
            note = f"已拆成 {len(published)} 篇专栏" if len(published) > 1 else "已发布为专栏文章"
            return PublishResult.ok(
                self.key,
                chapter,
                url=published[0],
                message=f"{note}，共 {len(pages)} 页",
                urls=published,
                mode="article",
                pages=len(pages),
            )
        finally:
            self.cleanup_prepared(chapter)

    # ---------- 图文动态（旧，可选） ----------

    def _plan_dynamic(self, chapter: Chapter) -> list[str]:
        pages = len(chapter.pages)
        posts = max(1, -(-pages // self.max_pages_per_post))
        body = self._body_text(chapter)
        return [
            f"发布方式：B站图文动态（publish_mode=dynamic）",
            f"上传 {pages} 张图片（jpg/png/gif，单条最多 {self.max_pages_per_post} 张）",
            f"预计发布 {posts} 条图文动态",
            f"标题：{self._title(chapter)}",
            f"正文：{(body[:80] + '…') if len(body) > 80 else body}",
        ]

    def _topics_text(self, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        topics = meta.get("topics") or self.cfg.get("topics", ["#原创漫画#"])
        if isinstance(topics, str):
            topics = [topics]
        result = []
        for topic in topics:
            topic = str(topic).strip()
            if topic and not topic.startswith("#"):
                topic = f"#{topic}#"
            result.append(topic)
        return "\n".join(result)

    def _caption(self, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        lines = [self._title(chapter)]
        body = str(meta.get("caption") or self._body_text(chapter)).strip()
        if body:
            lines.append("")
            lines.append(body)
        topics = self._topics_text(chapter)
        if topics:
            lines.append("")
            lines.append(topics)
        text = "\n".join(lines).strip()
        if len(text) > 900:
            text = text[:897] + "…"
        return text

    def _upload_dynamic_image(self, page, category: str) -> dict:
        with open(page.path, "rb") as fh:
            data = {
                "file_up": (page.path.name, fh, "application/octet-stream"),
                "category": category,
                "biz": "new_dyn",
                "csrf": self.csrf,
            }
            resp = self.http.post(DYNAMIC_UPLOAD_IMAGE_URL, files=data)
        payload = resp.json()
        if payload.get("code") != 0:
            self.http._dump(resp, tag="bilibili-upload")
            raise PublisherError(f"B站 图片上传失败：{payload.get('message') or payload}")
        info = payload.get("data") or {}
        if not info.get("image_url"):
            raise PublisherError(f"B站 上传响应缺少 image_url：{payload}")
        return {
            "img_src": info["image_url"],
            "img_width": int(info.get("image_width", page.width) or page.width),
            "img_height": int(info.get("image_height", page.height) or page.height),
            "img_size": float(info.get("img_size") or page.size_kb),
        }

    def _create_dynamic(self, caption: str, pics: list[dict]) -> str:
        upload_id = f"0_{int(time.time())}_{random.randint(1000, 9999)}"
        body = {
            "dyn_req": {
                "content": {"contents": [{"raw_text": caption, "type": 1, "biz_id": ""}]},
                "pics": pics,
                "scene": 2,
                "upload_id": upload_id,
            }
        }
        resp = self.http.post(
            f"{CREATE_DYN_URL}?csrf={quote(self.csrf)}",
            data=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        payload = resp.json()
        if payload.get("code") != 0:
            self.http._dump(resp, tag="bilibili-dyn")
            code = payload.get("code")
            tips = {
                -101: "账号未登录，Cookie 可能过期",
                -111: "CSRF 校验失败，请刷新 bili_jct",
                4126021: "账号未绑定手机，无法发布动态",
            }
            raise PublisherError(
                f"B站 动态发布失败（code={code}）：{payload.get('message') or payload}"
                + ("。" + tips[code] if code in tips else "")
            )
        dyn_id = (payload.get("data") or {}).get("dyn_id_str") or ""
        if not dyn_id:
            raise PublisherError(f"B站 响应缺少 dyn_id_str：{payload}")
        return dyn_id

    def _publish_dynamic(self, chapter: Chapter) -> PublishResult:
        meta = self._meta(chapter)
        category = str(meta.get("image_category") or self.cfg.get("image_category", "draw"))
        allowed = {".jpg", ".jpeg", ".png", ".gif"}
        pages = self.prepare_pages(chapter, allowed_exts=allowed)
        try:
            caption = self._caption(chapter)
            groups = chunk_list(pages, self.max_pages_per_post)
            published: list[str] = []
            errors: list[str] = []
            for index, group in enumerate(groups, 1):
                try:
                    pics = []
                    for page in group:
                        self.log.info("上传图片 %s（第 %d/%d 组）", page.path.name, index, len(groups))
                        pics.append(self._upload_dynamic_image(page, category))
                    part_caption = caption
                    if len(groups) > 1:
                        part_caption = f"{caption}\n（第 {index}/{len(groups)} 部分）"
                    dyn_id = self._create_dynamic(part_caption, pics)
                    url = f"https://t.bilibili.com/{dyn_id}"
                    published.append(url)
                    self.log.info("动态发布成功：%s", url)
                except PublisherError as exc:
                    errors.append(f"第 {index} 条动态失败：{exc}")
                    self.log.error("第 %d 条动态失败：%s", index, exc)
                    continue

            if errors:
                return PublishResult.partial(
                    self.key,
                    chapter,
                    url=published[0] if published else "",
                    message=f"部分失败：{'; '.join(errors)}",
                    urls=published,
                    mode="dynamic",
                )
            note = "已拆分为多条动态" if len(published) > 1 else ""
            return PublishResult.ok(
                self.key,
                chapter,
                url=published[0],
                message=note or f"共 {len(pages)} 页",
                urls=published,
                mode="dynamic",
                pages=len(pages),
            )
        finally:
            self.cleanup_prepared(chapter)
