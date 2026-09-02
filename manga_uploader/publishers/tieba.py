"""百度贴吧发图帖。

接口与参数基于社区长期维护的资料（aiotieba / 贴吧接口汇总）：
- tbs 令牌：GET https://tieba.baidu.com/dc/common/tbs
- 传图：POST https://tieba.baidu.com/cgi-bin/upload_image?from=tiebapc&tbs=xxx
- 发帖：POST https://tieba.baidu.com/f/commit/thread/add

说明：贴吧有反爬/风控，若遇到验证码会直接报错并提示手动处理，
本工具不做验证码绕过；频控由 interval 配置控制。
"""

from __future__ import annotations

import html
import json
import mimetypes
import re
import time
from urllib.parse import quote

from ..models import Chapter, CheckResult, PublishResult
from ..util import chunk_list
from .base import BasePublisher, PublisherError

TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
UPLOAD_URL = "https://tieba.baidu.com/cgi-bin/upload_image"
THREAD_ADD_URL = "https://tieba.baidu.com/f/commit/thread/add"
POST_ADD_URL = "https://tieba.baidu.com/f/commit/post/add"
FORUM_URL = "https://tieba.baidu.com/f"

BLOCK_PATTERN = re.compile(r"验证码|vcode|风控|秒删|太频繁|防恶意", re.I)


def _find_first(obj: object, keys: tuple[str, ...]) -> object | None:
    """在嵌套 JSON 里找第一个存在的键。"""
    if isinstance(obj, dict):
        for key in keys:
            if obj.get(key):
                return obj[key]
        for value in obj.values():
            found = _find_first(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_first(value, keys)
            if found:
                return found
    return None


class TiebaPublisher(BasePublisher):
    key = "tieba"
    display_name = "百度贴吧"

    @property
    def max_pages_per_post(self) -> int:
        return max(1, int(self.cfg.get("max_pages_per_post", 50)))

    def _forum(self, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        forum = str(meta.get("forum") or chapter.raw.get("forum") or self.cfg.get("forum") or "").strip()
        if not forum:
            raise PublisherError("贴吧发帖需要吧名：在 manga.json 的 platforms.tieba.forum 或 config.yaml 的 tieba.settings.forum 填写")
        return forum

    def check(self) -> CheckResult:
        missing = self.missing_cookies()
        if missing:
            return CheckResult(self.key, False, f"缺少 Cookie：{', '.join(missing)}")
        try:
            data = self.http.get_json(TBS_URL)
        except Exception as exc:
            return CheckResult(self.key, False, f"网络请求失败：{exc}")
        if data.get("is_login") in (1, "1", True):
            user = data.get("user_name") or data.get("user") or ""
            return CheckResult(self.key, True, f"已登录：{user}（tbs 正常）")
        return CheckResult(self.key, False, f"未登录（{data.get('error') or 'Cookie 无效'}）")

    def plan(self, chapter: Chapter) -> list[str]:
        pages = len(chapter.pages)
        posts = max(1, -(-pages // self.max_pages_per_post))
        return [
            f"发帖标题：{chapter.title}",
            f"目标贴吧：{self._forum(chapter)}",
            f"上传 {pages} 张图片（每楼最多 {self.max_pages_per_post} 张，预计 1 帖 {posts} 楼）",
            f"简介：{(chapter.description[:80] + '…') if len(chapter.description) > 80 else chapter.description}",
        ]

    def _tbs(self) -> str:
        data = self.http.get_json(TBS_URL)
        tbs = str(data.get("tbs") or "")
        if not tbs:
            raise PublisherError(f"获取 tbs 失败：{data}")
        return tbs

    def _fid(self, forum: str, tbs: str) -> str:
        cfg_fid = str(self.cfg.get("fid") or 0)
        if cfg_fid and cfg_fid != "0":
            return cfg_fid
        url = f"{FORUM_URL}?kw={quote(forum)}"
        resp = self.http.get(url)
        text = resp.text
        patterns = [
            r'"fid"\s*:\s*"?(\d+)',
            r'"fid"\s*=\s*"?(\d+)',
            r"fid[\"']?\s*[:=]\s*[\"']?(\d+)",
            r'forum_id["\']?\s*:\s*["\']?(\d+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        raise PublisherError(
            f"无法从贴吧页面解析 fid（{forum}）。"
            "请在 config.yaml 的 tieba.settings.fid 里手动填写（访问吧页后在网页源码中搜索 fid）。"
        )

    def _upload_image(self, page, tbs: str, forum: str) -> str:
        mime = mimetypes.guess_type(page.path.name)[0] or "image/jpeg"
        with open(page.path, "rb") as fh:
            resp = self.http.post(
                f"{UPLOAD_URL}?from=tiebapc&tbs={quote(tbs)}",
                files={"file": (page.path.name, fh, mime)},
                headers={"Referer": f"{FORUM_URL}?kw={quote(forum)}"},
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise PublisherError(f"贴吧传图接口未返回 JSON：{resp.text[:200]}") from exc
        url = _find_first(payload, ("imgurl", "img_url", "pic_url", "image_url", "url"))
        if not url:
            raise PublisherError(f"贴吧传图失败，响应中找不到图片地址：{str(payload)[:300]}")
        return str(url)

    def _post_thread(self, forum: str, fid: str, tbs: str, title: str, content_html: str) -> str:
        data = {
            "ie": "utf-8",
            "kw": forum,
            "fid": fid,
            "tbs": tbs,
            "title": title,
            "content": content_html,
        }
        resp = self.http.post(
            THREAD_ADD_URL,
            data=data,
            headers={
                "Referer": f"{FORUM_URL}?kw={quote(forum)}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        text = resp.text.strip()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if payload is not None:
            if payload.get("no") == 0 or payload.get("errno") == 0:
                tid = str((payload.get("data") or {}).get("tid") or payload.get("tid") or "")
                if tid:
                    return tid
            self.http._dump(resp, tag="tieba-thread")
            message = str(payload.get("error") or payload.get("errmsg") or payload.get("msg") or payload)
            if BLOCK_PATTERN.search(message):
                raise PublisherError(
                    "贴吧要求验证码或触发风控（无法自动绕过）。请先手动发一帖，过一会再试，"
                    "或在浏览器里完成发帖。"
                )
            raise PublisherError(f"贴吧发帖失败：{message[:300]}")
        # 偶发返回 HTML：找错误提示
        plain = re.sub(r"<[^>]+>", " ", text)
        plain = re.sub(r"\s+", " ", plain).strip()
        self.http._dump(resp, tag="tieba-thread")
        if BLOCK_PATTERN.search(plain):
            raise PublisherError(
                "贴吧要求验证码或触发风控（无法自动绕过）。请先手动发一帖，过一会再试。"
            )
        raise PublisherError(f"贴吧发帖失败，响应不是预期 JSON：{plain[:300]}")

    def _reply_post(self, forum: str, fid: str, tbs: str, tid: str, content_html: str) -> str:
        """给已发的主题帖追加楼层（图片较多时继续贴图）。"""
        data = {
            "ie": "utf-8",
            "kw": forum,
            "fid": fid,
            "tid": tid,
            "tbs": tbs,
            "vcode_md5": "",
            "rich_text": "1",
            "content": content_html,
        }
        resp = self.http.post(
            POST_ADD_URL,
            data=data,
            headers={
                "Referer": f"https://tieba.baidu.com/p/{tid}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        text = resp.text.strip()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if payload is not None:
            if payload.get("no") == 0 or payload.get("errno") == 0:
                pid = str((payload.get("data") or {}).get("pid") or payload.get("pid") or "")
                return pid
            self.http._dump(resp, tag="tieba-post")
            message = str(payload.get("error") or payload.get("errmsg") or payload.get("msg") or payload)
            if BLOCK_PATTERN.search(message):
                raise PublisherError(
                    "贴吧追加楼层要求验证码或触发风控（无法自动绕过），其余图片未继续发送。"
                )
            raise PublisherError(f"贴吧追加楼层失败：{message[:300]}")
        self.http._dump(resp, tag="tieba-post")
        plain = re.sub(r"<[^>]+>", " ", text)
        plain = re.sub(r"\s+", " ", plain).strip()
        if BLOCK_PATTERN.search(plain):
            raise PublisherError("贴吧追加楼层触发风控/验证码（无法自动绕过），其余图片未继续发送。")
        raise PublisherError(f"贴吧追加楼层失败，响应不是预期 JSON：{plain[:300]}")

    def publish(self, chapter: Chapter) -> PublishResult:
        self.require_cookies()
        if not chapter.pages:
            return PublishResult.skipped(self.key, chapter, "没有图片")
        forum = self._forum(chapter)
        allowed = {".jpg", ".jpeg", ".png", ".gif"}
        pages = self.prepare_pages(chapter, allowed_exts=allowed)
        try:
            tbs = self._tbs()
            fid = self._fid(forum, tbs)
            self.log.info("吧名=%s fid=%s tbs=%s", forum, fid, tbs[:6] + "…")

            published: list[str] = []
            errors: list[str] = []
            groups = chunk_list(pages, self.max_pages_per_post)
            thread_tid: str | None = None
            for index, group in enumerate(groups, 1):
                try:
                    image_urls = []
                    for page in group:
                        self.log.info("上传图片 %s（第 %d/%d 组）", page.path.name, index, len(groups))
                        image_urls.append(self._upload_image(page, tbs, forum))
                        time.sleep(float(self.cfg.get("upload_sleep", 1.0) or 0))

                    body = [chapter.description] if chapter.description else []
                    parts = []
                    if body:
                        parts.append("<p>" + html.escape("\n".join(body)).replace("\n", "<br>") + "</p>")
                    for url in image_urls:
                        parts.append(f'<img class="BDE_Image" src="{html.escape(url, quote=True)}" unselectable="on">')
                    content_html = "".join(parts)

                    suffix = str(self.cfg.get("title_suffix") or "")
                    title = f"{suffix}{chapter.title}" if suffix else chapter.title
                    if thread_tid is None:
                        thread_tid = self._post_thread(forum, fid, tbs, title[:100], content_html)
                        url = f"https://tieba.baidu.com/p/{thread_tid}"
                        published.append(url)
                        self.log.info("主题帖发布成功：%s", url)
                    else:
                        self._reply_post(forum, fid, tbs, thread_tid, content_html)
                        self.log.info("已追加楼层 %d 到 %s", index, thread_tid)
                except PublisherError as exc:
                    errors.append(str(exc))
                    self.log.error("第 %d 组发帖失败：%s", index, exc)
                    if BLOCK_PATTERN.search(str(exc)):
                        break  # 风控时继续只会重复触发，停止后续组

            if errors and not published:
                return PublishResult.failed(
                    self.key, chapter, "；".join(errors[:3]), details={"count": len(errors)}
                )
            if errors:
                return PublishResult.partial(
                    self.key,
                    chapter,
                    url=published[0],
                    message=f"部分帖子失败：{errors[0]}",
                    urls=published,
                    failed=len(errors),
                    pages=len(pages),
                )
            return PublishResult.ok(
                self.key,
                chapter,
                url=published[0],
                message=f"已发 1 帖共 {len(groups)} 楼",
                urls=published,
                pages=len(pages),
            )
        finally:
            self.cleanup_prepared(chapter)
