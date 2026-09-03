"""再漫画（zaimanhua.com）漫画投稿。

依据网页端“发布漫画”（https://manhua.zaimanhua.com/uploadShows）抓包得到的接口：
1. 登录态：Cookie token（值为 JWT），请求头带 Authorization: Bearer <token>
   与 Platform: pc；部分接口需要 X-Client-ID（Cookie clientId，可留空）。
2. 逐页上传：POST https://v4api.zaimanhua.com/api/v1/comic2/upload/upload/img
   multipart 字段 file + beginTime，成功返回 {"errno":0,"data":{"file": url}}。
3. 提交章节：POST https://v4api.zaimanhua.com/api/v1/comic2/upload/submit/chapter
   JSON {name, chapter, introduction, downloadUrl, cate, pageUrls}。

页面限制：单章最多 500 张图、单张不超过 10MB、简介最多 1000 字，
推荐 jpg。作品类型 cate：1 原创作品 / 2 原创汉化 / 3 个人扫漫 / 4 转载作品。
提交后进入平台人工审核，审核通过才会出现在原创频道。
"""

from __future__ import annotations

import json
import mimetypes
import time

from ..models import Chapter, CheckResult, PublishResult
from .base import BasePublisher, PublisherError

UPLOAD_IMG_URL = "https://v4api.zaimanhua.com/api/v1/comic2/upload/upload/img"
SUBMIT_CHAPTER_URL = "https://v4api.zaimanhua.com/api/v1/comic2/upload/submit/chapter"
USER_INFO_URL = "https://account-api.zaimanhua.com/v1/userInfo/get"
UPLOAD_PAGE_URL = "https://manhua.zaimanhua.com/uploadShows"

CATE_LABELS = {
    "1": "原创作品",
    "2": "原创汉化",
    "3": "个人扫漫",
    "4": "转载作品",
}


class ZaimanhuaPublisher(BasePublisher):
    key = "zaimanhua"
    display_name = "再漫画"

    @property
    def token(self) -> str:
        token = self.cfg.cookies.get("token", "")
        if not token:
            raise PublisherError("再漫画缺少 Cookie：token（登录后再漫画网站可获取）")
        return token.strip()

    @property
    def client_id(self) -> str:
        return str(self.cfg.cookies.get("clientId") or "").strip()

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Platform": "pc",
            "Origin": "https://manhua.zaimanhua.com",
            "Referer": UPLOAD_PAGE_URL,
        }
        if self.client_id:
            headers["X-Client-ID"] = self.client_id
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _cate(self, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        cate = str(meta.get("cate") or self.cfg.get("cate") or "1").strip()
        if cate not in CATE_LABELS:
            raise PublisherError(
                f"再漫画作品类型只能填 {'/'.join(CATE_LABELS)}，当前是 {cate}"
            )
        return cate

    def _work_name(self, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        name = (
            str(meta.get("work_name") or "").strip()
            or str(self.cfg.get("work_name") or "").strip()
            or str(chapter.raw.get("series_title") or "").strip()
            or str(chapter.raw.get("title") or "").strip()
        )
        if not name:
            name = chapter.title
        return name

    def _chapter_name(self, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        return str(meta.get("chapter_name") or chapter.title).strip() or chapter.title

    # ---------- 接口 ----------

    def check(self) -> CheckResult:
        missing = self.missing_cookies()
        if missing:
            return CheckResult(self.key, False, f"缺少 Cookie：{', '.join(missing)}")
        try:
            data = self.http.get_json(USER_INFO_URL, headers=self._headers())
        except Exception as exc:
            return CheckResult(self.key, False, f"网络请求失败：{exc}")
        if data.get("errno") == 0:
            info = (data.get("data") or {}).get("userInfo") or {}
            name = info.get("nickname") or info.get("userName") or ""
            uid = info.get("uid") or ""
            return CheckResult(self.key, True, f"已登录：{name} (uid {uid})")
        return CheckResult(self.key, False, f"登录失效：{data.get('errmsg') or 'token 无效'}")

    def plan(self, chapter: Chapter) -> list[str]:
        cate = self._cate(chapter)
        return [
            f"作品名称：{self._work_name(chapter)}",
            f"章节名称：{self._chapter_name(chapter)}",
            f"作品类型：{CATE_LABELS[cate]}",
            f"上传 {len(chapter.pages)} 张图片（压缩至单张 {self.common.max_bytes_mb:g}MB 内，"
            f"建议 jpg；最多 {self.cfg.get('max_pages_per_upload', 500)} 张）",
            f"简介：{(chapter.description[:80] + '…') if len(chapter.description) > 80 else chapter.description}",
            "提交后等待平台审核",
        ]

    def _upload_page(self, page) -> str:
        mime = mimetypes.guess_type(page.path.name)[0] or "image/jpeg"
        attempts = max(1, int(self.cfg.get("upload_attempts", 2) or 2))
        last_error = "未知错误"
        for attempt in range(1, attempts + 1):
            try:
                with open(page.path, "rb") as fh:
                    resp = self.http.post(
                        UPLOAD_IMG_URL,
                        files={"file": (page.path.name, fh, mime)},
                        data={"beginTime": ""},
                        headers=self._headers(),
                    )
                try:
                    payload = resp.json()
                except ValueError as exc:  # pragma: no cover
                    raise PublisherError(
                        f"再漫画 传图接口未返回 JSON：{resp.text[:200]}"
                    ) from exc
                if payload.get("errno") != 0:
                    last_error = str(payload.get("errmsg") or payload)
                    raise PublisherError(f"再漫画 传图失败：{last_error}")
                url = str(((payload.get("data") or {}).get("file") or "")).strip()
                if not url:
                    last_error = f"上传响应缺少图片地址：{payload}"
                    raise PublisherError(last_error)
                return url
            except PublisherError:
                if attempt < attempts:
                    wait = min(2.0 * attempt, 6.0)
                    self.log.warning(
                        "再漫画 图片 %s 上传失败（第 %d/%d 次），%s 秒后重试：%s",
                        page.path.name,
                        attempt,
                        attempts,
                        wait,
                        last_error,
                    )
                    time.sleep(wait)
                    continue
                raise
            except Exception as exc:  # 网络层失败也按次数重试
                last_error = str(exc)
                if attempt < attempts:
                    wait = min(2.0 * attempt, 6.0)
                    self.log.warning(
                        "再漫画 图片 %s 网络上传失败（第 %d/%d 次），%s 秒后重试：%s",
                        page.path.name,
                        attempt,
                        attempts,
                        wait,
                        exc,
                    )
                    time.sleep(wait)
                    continue
                raise PublisherError(
                    f"再漫画 图片 {page.path.name} 上传失败（已重试 {attempts} 次）：{last_error}"
                ) from exc
        raise PublisherError(
            f"再漫画 图片 {page.path.name} 上传失败（已重试 {attempts} 次）：{last_error}"
        )

    def _submit_chapter(self, body: dict) -> dict:
        resp = self.http.post(
            SUBMIT_CHAPTER_URL,
            data=json.dumps(body),
            headers=self._headers(json_body=True),
        )
        try:
            payload = resp.json()
        except ValueError as exc:  # pragma: no cover
            raise PublisherError(f"再漫画 提交接口未返回 JSON：{resp.text[:200]}") from exc
        if payload.get("errno") != 0:
            self.http._dump(resp, tag="zaimanhua-submit")
            raise PublisherError(f"再漫画 提交失败：{payload.get('errmsg') or payload}")
        return payload.get("data") or {}

    def publish(self, chapter: Chapter) -> PublishResult:
        self.require_cookies()
        if not chapter.pages:
            return PublishResult.skipped(self.key, chapter, "没有图片")
        cate = self._cate(chapter)
        work_name = self._work_name(chapter)
        chapter_name = self._chapter_name(chapter)
        max_pages = max(1, int(self.cfg.get("max_pages_per_upload", 500)))
        if len(chapter.pages) > max_pages:
            raise PublisherError(
                f"再漫画 单次最多提交 {max_pages} 张图片，当前章节有 {len(chapter.pages)} 张，"
                "请把章节拆小后再发布。"
            )

        pages = self.prepare_pages(chapter, allowed_exts={".jpg", ".jpeg", ".png", ".gif"})
        try:
            page_urls: list[str] = []
            for index, page in enumerate(pages, 1):
                self.log.info(
                    "上传图片 %d/%d：%s（%s）",
                    index,
                    len(pages),
                    page.path.name,
                    f"{page.size_bytes / 1024 / 1024:.2f}MB",
                )
                page_urls.append(self._upload_page(page))
                if self.common.interval_seconds:
                    time.sleep(float(self.common.interval_seconds))

            body = {
                "name": work_name,
                "chapter": chapter_name,
                "introduction": chapter.description[:1000],
                "downloadUrl": "",
                "cate": cate,
                "pageUrls": page_urls,
            }
            self.log.info("提交章节：%s - %s", body["name"], body["chapter"])
            self._submit_chapter(body)
            return PublishResult.ok(
                self.key,
                chapter,
                url="",
                message=f"已提交 {len(page_urls)} 页，等待再漫画审核",
                pages=len(page_urls),
                cate=CATE_LABELS.get(body["cate"]),
            )
        finally:
            self.cleanup_prepared(chapter)
