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

关于登录态（踩过的坑）：
token 是一个 JWT，`iat`/`exp` 间隔固定 30 天，**上传/提交接口会严格校验 exp**，
过期后统一返回 `{"errno":99,"errmsg":"请先登录"}`；而账号接口
（account-api 的 userInfo）不校验 exp，过期 token 依然返回 `errno:0`。
结果就是「检查登录说没问题，一发布就报请先登录」，所以这里改成先本地解 JWT exp，
过期直接给出明确提示，不再浪费一次上传重试。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from typing import Optional

from ..models import Chapter, CheckResult, PublishResult
from .. import composer
from .base import BasePublisher, PublisherError

UPLOAD_IMG_URL = "https://v4api.zaimanhua.com/api/v1/comic2/upload/upload/img"
SUBMIT_CHAPTER_URL = "https://v4api.zaimanhua.com/api/v1/comic2/upload/submit/chapter"
USER_INFO_URL = "https://account-api.zaimanhua.com/v1/userInfo/get"
UPLOAD_PAGE_URL = "https://manhua.zaimanhua.com/uploadShows"
LOGIN_URL = "https://manhua.zaimanhua.com/"

# token（JWT）的签发/过期间隔：官网固定 30 天，过期后上传接口一律「请先登录」。
TOKEN_TTL_DAYS = 30
# 距离过期不足这个天数时提前提醒（但仍然允许发布）。
TOKEN_WARN_DAYS = 3

LOGIN_EXPIRED_HINT = (
    "再漫画 登录已失效（接口返回 errno 99「请先登录」）：再漫画的 token 有效期只有 "
    f"{TOKEN_TTL_DAYS} 天，过期后上传/提交接口会直接拒绝（账号接口不校验过期，"
    "所以「检查登录」仍会显示正常）。"
    f"请重新登录 {LOGIN_URL} 后复制 Cookie 里的新 token，"
    "在「平台账号 → 再漫画」保存后重试。"
)

CATE_LABELS = {
    "1": "原创作品",
    "2": "原创汉化",
    "3": "个人扫漫",
    "4": "转载作品",
}


class LoginExpiredError(PublisherError):
    """登录态失效：重试没有任何意义，直接终止。"""


def extract_token(raw: str) -> str:
    """容错解析 token 字段。

    允许三种填法：① 直接是 JWT；② 粘贴了整段 Cookie（`token=...; clientId=...`）；
    ③ 从开发者工具里连引号一起复制（`"eyJ..."`）。
    """
    text = str(raw or "").strip().strip('"').strip("'").strip()
    if not text:
        return ""
    # ① 裸 JWT：三段、不含 Cookie 分隔符
    if text.count(".") == 2 and ";" not in text:
        return text
    # ②/③ 整段 Cookie：挑出 token= 的值（大小写不敏感）
    for chunk in re.split(r"[;,\n]", text):
        key, sep, value = chunk.partition("=")
        if sep and key.strip().lower() == "token":
            value = value.strip().strip('"').strip("'")
            if value:
                return value
    return text


def decode_token_expiry(token: str) -> Optional[float]:
    """读出 JWT payload 里的 exp（不验签，只用于提前提示过期）。"""
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1].replace("-", "+").replace("_", "/")
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.b64decode(payload).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - 解析失败就当作未知
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if isinstance(exp, bool):
        return None
    if isinstance(exp, (int, float)):
        return float(exp)
    if isinstance(exp, str):
        try:
            return float(exp)
        except ValueError:
            return None
    return None


def is_login_error(payload: dict) -> bool:
    """errcode 99 / “请先登录” 都算登录态失效。"""
    errno = payload.get("errno")
    if not isinstance(errno, bool):
        try:
            if int(errno) == 99:  # type: ignore[arg-type]
                return True
        except (TypeError, ValueError):
            pass
    msg = str(payload.get("errmsg") or "")
    return any(word in msg for word in ("请先登录", "未登录", "登录失效", "登录已过期"))


class ZaimanhuaPublisher(BasePublisher):
    key = "zaimanhua"
    display_name = "再漫画"

    @property
    def token(self) -> str:
        token = extract_token(self.cfg.cookies.get("token", ""))
        if not token:
            raise PublisherError("再漫画缺少 Cookie：token（登录后再漫画网站可获取）")
        return token.strip()

    @property
    def client_id(self) -> str:
        return extract_token(str(self.cfg.cookies.get("clientId") or ""))

    # ---------- 登录态 ----------

    def token_expiry(self) -> Optional[float]:
        """token 的过期时间戳；不是 JWT / 解析不出来时返回 None。"""
        try:
            token = self.token
        except PublisherError:
            return None
        return decode_token_expiry(token)

    def is_token_expired(self) -> bool:
        exp = self.token_expiry()
        return exp is not None and exp <= time.time()

    def token_expiry_message(self) -> str:
        """过期/即将过期的提示；正常时返回空串。"""
        exp = self.token_expiry()
        if exp is None:
            return ""
        now = time.time()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
        if exp <= now:
            return (
                f"再漫画登录已过期：token（有效期 {TOKEN_TTL_DAYS} 天）已于 {when} 到期，"
                "上传/提交接口会直接返回「请先登录」。"
                f"请重新登录 {LOGIN_URL} 复制新的 token 后重试。"
            )
        left_days = (exp - now) / 86400.0
        if left_days < TOKEN_WARN_DAYS:
            return (
                f"提醒：再漫画 token 将于 {when} 到期（还有 {left_days:.1f} 天），"
                "到期后上传会报「请先登录」，建议提前更换。"
            )
        return ""

    def require_valid_token(self) -> None:
        """发布前先本地判断 token 是否过期：过期就没必要传图了。"""
        if self.is_token_expired():
            raise LoginExpiredError(self.token_expiry_message())

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
        return (
            str(meta.get("work_name") or "").strip()
            or str(self.cfg.get("work_name") or "").strip()
            or composer.zaim_work_name(chapter)
        )

    def _chapter_name(self, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        return (
            str(meta.get("chapter_name") or "").strip()
            or str(self.cfg.get("chapter_name") or "").strip()
            or composer.zaim_chapter_name(chapter)
        )

    def _introduction(self, chapter: Chapter) -> str:
        meta = self._meta(chapter)
        return (
            str(meta.get("introduction") or "").strip()
            or str(self.cfg.get("introduction") or "").strip()
            or composer.zaim_introduction(chapter)
        )

    # ---------- 接口 ----------

    def check(self) -> CheckResult:
        missing = self.missing_cookies()
        if missing:
            return CheckResult(self.key, False, f"缺少 Cookie：{', '.join(missing)}")
        # 先本地看 token 的 exp：过期的话账号接口仍会显示“已登录”，
        # 但上传接口一定报「请先登录」，这里直接说清楚。
        if self.is_token_expired():
            return CheckResult(
                self.key,
                False,
                f"{self.token_expiry_message()}"
                "（注意：官网账号接口不校验过期，过期 token 也显示已登录，"
                "所以以前看起来是好的）",
            )
        try:
            data = self.http.get_json(USER_INFO_URL, headers=self._headers())
        except Exception as exc:
            return CheckResult(self.key, False, f"网络请求失败：{exc}")
        if data.get("errno") == 0:
            info = (data.get("data") or {}).get("userInfo") or {}
            name = info.get("nickname") or info.get("userName") or ""
            uid = info.get("uid") or ""
            message = f"已登录：{name} (uid {uid})"
            warn = self.token_expiry_message()
            if warn:
                message = f"{message}；{warn}"
            return CheckResult(self.key, True, message)
        return CheckResult(self.key, False, f"登录失效：{data.get('errmsg') or 'token 无效'}")

    def plan(self, chapter: Chapter) -> list[str]:
        cate = self._cate(chapter)
        return [
            f"作品名称：{self._work_name(chapter)}",
            f"章节名称：{self._chapter_name(chapter)}",
            f"作品类型：{CATE_LABELS[cate]}",
            f"上传 {len(chapter.pages)} 张图片（压缩至单张 {self.common.max_bytes_mb:g}MB 内，"
            f"建议 jpg；最多 {self.cfg.get('max_pages_per_upload', 500)} 张）",
            f"简介：{(self._introduction(chapter)[:80] + '…') if len(self._introduction(chapter)) > 80 else self._introduction(chapter)}",
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
                    if is_login_error(payload):
                        # 登录失效：重试再多次也一样，直接终止
                        raise LoginExpiredError(
                            f"{LOGIN_EXPIRED_HINT}"
                            f"（接口原话：{payload.get('errmsg') or payload}）"
                        )
                    last_error = str(payload.get("errmsg") or payload)
                    raise PublisherError(f"再漫画 传图失败：{last_error}")
                url = str(((payload.get("data") or {}).get("file") or "")).strip()
                if not url:
                    last_error = f"上传响应缺少图片地址：{payload}"
                    raise PublisherError(last_error)
                return url
            except LoginExpiredError:
                # 登录态问题重试无意义，直接抛给上层
                raise
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
            if is_login_error(payload):
                raise LoginExpiredError(
                    f"{LOGIN_EXPIRED_HINT}（接口原话：{payload.get('errmsg') or payload}）"
                )
            raise PublisherError(f"再漫画 提交失败：{payload.get('errmsg') or payload}")
        return payload.get("data") or {}

    def publish(self, chapter: Chapter) -> PublishResult:
        self.require_cookies()
        # token 过期的话，上传接口必定返回「请先登录」，先拦住，别白传一遍图
        self.require_valid_token()
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
                self.progress(
                    "upload",
                    index - 1,
                    len(pages),
                    f"正在上传图片 {index}/{len(pages)}：{page.path.name}",
                    chapter_key=chapter.key,
                )
                self.log.info(
                    "上传图片 %d/%d：%s（%s）",
                    index,
                    len(pages),
                    page.path.name,
                    f"{page.size_bytes / 1024 / 1024:.2f}MB",
                )
                page_urls.append(self._upload_page(page))
                self.progress(
                    "upload",
                    index,
                    len(pages),
                    f"已上传图片 {index}/{len(pages)}",
                    chapter_key=chapter.key,
                )
                if self.common.interval_seconds:
                    time.sleep(float(self.common.interval_seconds))

            body = {
                "name": work_name,
                "chapter": chapter_name,
                "introduction": self._introduction(chapter)[:1000],
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
