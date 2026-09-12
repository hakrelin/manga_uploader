"""B站（哔哩哔哩）发布。

默认发布“专栏文章”（publish_mode=article）：
1. 逐张上传正文图片（接口与字段各版本略有差异，程序按候选顺序尝试）：
   - POST https://api.bilibili.com/x/article/creative/article/upcover
   multipart 字段 binary（实测字段 file 返回 code=-400）+ csrf，
   成功返回 {code:0, data:{url}}。
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
import math
import mimetypes
import random
import time
from urllib.parse import quote

from ..models import Chapter, CheckResult, PublishResult
from .. import composer
from ..util import chunk_list
from .base import BasePublisher, CaptchaRequiredError, PublisherError

# 数值型设置的安全范围（防止手改配置填成 9999 把发布拖成几小时）
IMAGE_DELAY_MAX = 60.0        # image_delay：每张图上传后最多随机等多少秒
RETRY_WAIT_MAX = 600.0        # submit_retry_wait：退避基准最大 600 秒
RETRY_WAIT_CAP = 120.0        # 单次退避最多等 120 秒
ATTEMPTS_MAX = 10             # upload_attempts / submit_attempts 上限


def _float_setting(value, default, *, lo=0.0, hi=None) -> float:
    """安全解析数值设置：非法值回退默认，并按 lo/hi 夹住。

    配置可能被手改（image_delay: abc）或填得很大（image_delay: 9999），
    直接 float() 会抛异常，照单全收又会把发布拖成几小时。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(number) or math.isinf(number):
        return float(default)
    number = max(number, lo)
    if hi is not None:
        number = min(number, hi)
    return number


def _int_setting(value, default, *, lo=1, hi=ATTEMPTS_MAX) -> int:
    """整数设置：非法值回退默认，并夹在 lo~hi 之间。"""
    return int(_float_setting(value, default, lo=lo, hi=hi))


NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
# 设备指纹接口（无需登录）：返回 b_3/b_4，对应 Cookie buvid3/buvid4。
# B站 web 风控会参考这两个值，只有 SESSDATA/bili_jct 的裸请求容易被 -352 拦下。
FINGER_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

# 图文动态（旧）
DYNAMIC_UPLOAD_IMAGE_URL = "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs"
CREATE_DYN_URL = "https://api.bilibili.com/x/dynamic/feed/create/dyn"

# 专栏文章
ARTICLE_UPCOVER_URL = "https://api.bilibili.com/x/article/creative/article/upcover"
ARTICLE_DRAFT_URL = "https://api.bilibili.com/x/article/creative/draft/addupdate"
ARTICLE_SUBMIT_URL = "https://api.bilibili.com/x/article/creative/article/submit"

ARTICLE_REFERER = "https://member.bilibili.com/platform/upload/text"
ARTICLE_EDIT_REFERER = "https://member.bilibili.com/platform/upload/text/edit"
MEMBER_ORIGIN = "https://member.bilibili.com"
WEB_ORIGIN = "https://www.bilibili.com"
# 风控类错误码：-352 风控校验失败 / -412 请求被拦截 / -509 限流
RISK_CONTROL_CODES = (-352, -412, -509)
# 发布时会自动补全的风控相关 Cookie
DEVICE_COOKIES = ("buvid3", "buvid4", "b_nut", "DedeUserID")
# 单张正文图片限制 5MB，允许 jpg/png
ARTICLE_MAX_BYTES = 5 * 1024 * 1024
ARTICLE_ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}


class BilibiliRiskControlError(PublisherError):
    """B站风控拦截（-352 等）。此时草稿通常已建好，可引导用户手动发布。"""

    def __init__(self, message: str, *, code: int | None = None, aid: str = ""):
        super().__init__(message)
        self.code = code
        self.aid = str(aid or "")


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

    # ---------- 会话 / 风控 ----------

    @staticmethod
    def _api_headers(
        referer: str = ARTICLE_REFERER, origin: str = MEMBER_ORIGIN
    ) -> dict[str, str]:
        """接口请求头：补齐 Referer / Origin / Accept，模仿会员中心网页的 XHR。

        只有 User-Agent 的裸请求（缺 Referer/Origin）更容易被判成脚本行为，
        触发 -352 风控校验失败。
        """
        return {
            "Referer": referer,
            "Origin": origin,
            "Accept": "application/json, text/plain, */*",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

    def cookie_audit(self) -> list[str]:
        """返回缺失的风控相关 Cookie 名（自检提示用）。"""
        jar = self.http.session.cookies
        return [name for name in DEVICE_COOKIES if not jar.get(name)]

    def ensure_session(self) -> dict[str, str]:
        """补全 B站风控依赖的会话 Cookie，返回本次补上的键值。

        - buvid3 / buvid4：设备指纹，来自 /x/frontend/finger/spi（无需登录）
        - b_nut：首次访问时间戳
        - DedeUserID：登录 UID，Cookie 里没有时从 nav 接口取

        只作用于本次发布的会话，不写回配置文件。缺失这些值时，只有
        SESSDATA/bili_jct 的请求很容易被 -352 风控拦下。
        """
        added: dict[str, str] = {}
        jar = self.http.session.cookies
        if not jar.get("buvid3") or not jar.get("buvid4"):
            try:
                payload = self.http.get_json(
                    FINGER_SPI_URL,
                    headers=self._api_headers(WEB_ORIGIN + "/", WEB_ORIGIN),
                    retry=False,
                )
                data = payload.get("data") or {}
                for key, name in (("b_3", "buvid3"), ("b_4", "buvid4")):
                    value = str(data.get(key) or "").strip()
                    if value and not jar.get(name):
                        jar.set(name, value, domain=".bilibili.com")
                        added[name] = value
            except Exception as exc:
                self.log.warning("B站 获取设备指纹失败（继续发布）：%s", exc)
        if not jar.get("b_nut"):
            value = str(int(time.time()))
            jar.set("b_nut", value, domain=".bilibili.com")
            added["b_nut"] = value
        if not jar.get("DedeUserID") and not self.missing_cookies():
            try:
                payload = self.http.get_json(
                    NAV_URL,
                    headers=self._api_headers(WEB_ORIGIN + "/", WEB_ORIGIN),
                    retry=False,
                )
                mid = str((payload.get("data") or {}).get("mid") or "").strip()
                if payload.get("code") == 0 and mid and mid != "0":
                    jar.set("DedeUserID", mid, domain=".bilibili.com")
                    added["DedeUserID"] = mid
            except Exception as exc:
                self.log.warning("B站 获取 DedeUserID 失败（继续发布）：%s", exc)
        if added:
            self.log.info("B站 已自动补全会话 Cookie：%s", "、".join(sorted(added)))
        return added

    @staticmethod
    def _draft_link(aid: str) -> str:
        """草稿的手动发布入口（被风控拦下时引导去创作中心手动发）。"""
        aid = str(aid or "").strip()
        if not aid:
            return ""
        return f"https://member.bilibili.com/platform/upload/text/edit?aid={aid}"

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
            level = (info.get("level_info") or {}).get("current_level")
            extra = []
            if level is not None:
                extra.append(f"Lv{level}")
            if info.get("mobile_verified") in (0, False):
                extra.append("手机未绑定")
            text = f"已登录：{uname} (UID {mid})"
            if extra:
                text += "（" + "，".join(extra) + "）"
            if info.get("mobile_verified") in (0, False):
                # 未绑定手机的账号发专栏基本都会被风控拦（code=-352）
                text += "。B站要求绑定手机后才能投稿，否则专栏/动态容易被 -352 拦下"
            audit = self.cookie_audit()
            if audit:
                text += (
                    "。Cookie 里缺少 "
                    + "/".join(audit)
                    + "（发布时会自动补全；把这些一起填进 config.yaml 更稳）"
                )
            return CheckResult(self.key, True, text)
        message = data.get("message") or "未登录"
        return CheckResult(self.key, False, f"登录失败：{message}")

    def identity(self) -> str:
        """当前 Cookie 对应的 B 站账号（昵称 + UID），失败返回空串。"""
        if self.missing_cookies():
            return ""
        try:
            data = self.http.get_json(NAV_URL)
        except Exception:
            return ""
        info = data.get("data") or {}
        if data.get("code") != 0 or not info.get("isLogin"):
            return ""
        name = str(info.get("uname") or "").strip()
        mid = info.get("mid") or ""
        if mid:
            return f"{name}（UID {mid}）"
        return name

    def plan(self, chapter: Chapter) -> list[str]:
        if self._mode(chapter) == "article":
            return self._plan_article(chapter)
        return self._plan_dynamic(chapter)

    def publish(self, chapter: Chapter) -> PublishResult:
        self.require_cookies()
        # 上传/提交前补全设备指纹与会话 Cookie，降低 -352 概率
        self.ensure_session()
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
        # 实测（2026-09）：upcover 只接受字段 binary；file 会稳定返回
        # code=-400（请求错误）。保留 file 作为未来接口变更时的备用，
        # 但放在 binary 之后，避免每次上传先报一次错。
        candidates = (
            (ARTICLE_UPCOVER_URL, "binary"),
            (ARTICLE_UPCOVER_URL, "file"),
        )
        # B站偶发的单图失败：整轮接口都失败后整体重试（次数可配，默认 3）
        attempts = _int_setting(self.cfg.get("upload_attempts", 3), 3)
        last_error = "未知错误"
        for attempt in range(1, attempts + 1):
            for endpoint, field in candidates:
                try:
                    with open(page.path, "rb") as fh:
                        resp = self.http.post(
                            endpoint,
                            files={field: (page.path.name, fh, mime)},
                            data={"csrf": self.csrf},
                            headers=self._api_headers(),
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
                    if endpoint == candidates[-1][0] and field == candidates[-1][1]:
                        # 该轮最后一个候选也被拒，才值得提示
                        self.log.warning(
                            "B站 图片 %s 第 %d/%d 轮候选全部被拒（%s，字段 %s）：%s",
                            page.path.name,
                            attempt,
                            attempts,
                            endpoint,
                            field,
                            last_error,
                        )
                    else:
                        # 中间候选被拒是换组合的正常流程，调试日志即可
                        self.log.debug(
                            "B站 图片 %s 候选被拒（%s，字段 %s，第 %d/%d 轮）：%s",
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
                self.log.debug(
                    "B站 图片 %s 上传成功（%s，字段 %s）：%s",
                    page.path.name,
                    endpoint,
                    field,
                    url,
                )
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
    def _needs_captcha(payload: dict) -> bool:
        """响应里带 v_voucher / gaia_vtoken：要求人机验证，重试没用。"""
        data = payload.get("data")
        if not isinstance(data, dict):
            return False
        return bool(data.get("v_voucher") or data.get("gaia_vtoken"))

    def _raise_api_error(self, tag: str, payload: dict, aid: str = "") -> None:
        code = payload.get("code")
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {}
        tips = {
            -101: "账号未登录，Cookie 可能过期",
            -111: "CSRF 校验失败，请刷新 bili_jct",
            -400: "请求参数被平台拒绝（可能是标题/正文/分类格式问题）",
            -403: "账号权限不足（多为未绑定手机/未实名/新号等级过低）",
            -404: "草稿不存在，可能已被删除",
            -352: (
                "被B站风控拦截（服务端判定，不是程序 bug）。常见原因：账号未绑定手机/未实名、"
                "等级过低、短时间提交太多，或当前网络出口（机房 IP、代理/VPN）被B站标记。"
                "程序已自动补全设备指纹 Cookie 并按指数退避重试过；仍失败请按顺序处理："
                "① 直接去B站创作中心手动发布这篇草稿；"
                "② 把 image_delay 调到 1~2 秒（0~60 秒），隔几十分钟再发；"
                "③ 关代理/VPN 或换网络（手机热点）后重试；"
                "④ 用该账号在网页端手动发一篇专栏，确认账号本身有投稿权限"
            ),
            -412: "请求被B站拦截（风控/频率限制），请降低频率或换网络后重试",
            -509: "请求过于频繁，B站已限流，请等待一段时间再发",
        }
        message = payload.get("message") or payload
        text = f"B站 专栏{tag}失败（code={code}）：{message}"
        if code in tips:
            text += f"。{tips[code]}"
        if self._needs_captcha(payload):
            error = CaptchaRequiredError(
                text
                + "。B站下发了人机验证（v_voucher）：请先用浏览器登录会员中心完成验证，"
                "再回来重新发布"
            )
            error.aid = str(aid or "")  # 草稿可能已存在，交给上层保留草稿
            raise error
        if code in RISK_CONTROL_CODES:
            raise BilibiliRiskControlError(text, code=code, aid=aid)
        raise PublisherError(text)

    def _draft_article(self, data: dict) -> str:
        resp = self.http.post(
            ARTICLE_DRAFT_URL, data=data, headers=self._api_headers(ARTICLE_EDIT_REFERER)
        )
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

    def _final_article_id(self, payload: dict, aid: str) -> str:
        """提交响应里的正式文章 id；找不到时退回草稿 aid（旧逻辑）。"""
        info = payload.get("data")
        if not isinstance(info, dict):
            info = {}
        candidates = ("cvid", "cv_id", "article_id", "art_id", "id", "aid")
        for key in candidates:
            value = info.get(key)
            if value not in (None, "", 0, "0"):
                self.log.info("B站 发布成功：草稿 aid=%s → 正式 id=%s", aid, value)
                return str(value)
        self.log.warning(
            "B站 提交接口响应未带正式文章 id，退回草稿 aid；data=%s", str(info)[:300]
        )
        return aid

    def _submit_article(self, aid: str, data: dict) -> str:
        """正式提交专栏。

        被风控拦（-352/-412/-509）不是参数问题，等一会儿往往就能过：
        按 submit_retry_wait × 2^(n-1) + 随机抖动退避重试（单次最多等 120 秒），
        重试前刷新设备指纹。
        """
        data = dict(data)
        data["aid"] = aid
        attempts = _int_setting(self.cfg.get("submit_attempts", 3), 3)
        base_wait = _float_setting(
            self.cfg.get("submit_retry_wait", 5), 5, lo=0.0, hi=RETRY_WAIT_MAX
        )
        for attempt in range(1, attempts + 1):
            resp = self.http.post(
                ARTICLE_SUBMIT_URL, data=data, headers=self._api_headers(ARTICLE_EDIT_REFERER)
            )
            try:
                payload = resp.json()
            except ValueError:
                raise PublisherError(f"B站 提交接口未返回 JSON：{resp.text[:200]}")
            if payload.get("code") == 0:
                return self._final_article_id(payload, aid)
            self.http._dump(resp, tag="bilibili-article-submit")
            code = payload.get("code")
            if (
                attempt < attempts
                and code in RISK_CONTROL_CODES
                and not self._needs_captcha(payload)
            ):
                wait = min(
                    base_wait * (2 ** (attempt - 1)) + random.uniform(0.0, 1.5),
                    RETRY_WAIT_CAP,
                )
                self.log.warning(
                    "B站 提交被风控拦截（code=%s，第 %d/%d 次），%.1f 秒后重试",
                    code,
                    attempt,
                    attempts,
                    wait,
                )
                if wait > 0:
                    time.sleep(wait)
                self.ensure_session()
                continue
            self._raise_api_error("发布", payload, aid=aid)
        raise BilibiliRiskControlError(  # pragma: no cover - 循环内必然 return/raise
            f"B站 专栏发布失败：重试 {attempts} 次仍被风控拦截", aid=aid
        )

    def _publish_article(self, chapter: Chapter) -> PublishResult:
        pages = self.prepare_pages(
            chapter, allowed_exts=ARTICLE_ALLOWED_EXTS, max_bytes=ARTICLE_MAX_BYTES
        )
        try:
            cover_url = self._cover_from_settings(chapter, pages)
            groups = chunk_list(pages, self.article_max_pages)
            published: list[str] = []
            errors: list[str] = []
            drafts: list[str] = []
            page_done = 0
            for index, group in enumerate(groups, 1):
                try:
                    urls: list[str] = []
                    for page_index, page in enumerate(group, 1):
                        self.progress(
                            "upload",
                            page_done,
                            len(pages),
                            f"正在上传图片 {page_done + 1}/{len(pages)}"
                            f"（第 {index}/{len(groups)} 篇专栏）",
                            chapter_key=chapter.key,
                        )
                        self.log.info(
                            "上传专栏图片 %d/%d（第 %d/%d 篇）：%s",
                            page_index,
                            len(group),
                            index,
                            len(groups),
                            page.path.name,
                        )
                        urls.append(self._upload_article_image(page))
                        page_done += 1
                        self.progress(
                            "upload",
                            page_done,
                            len(pages),
                            f"已上传图片 {page_done}/{len(pages)}",
                            chapter_key=chapter.key,
                        )
                        # 固定间隔（common.interval_seconds）+ 随机抖动（image_delay），
                        # 让请求节奏更像人工操作；被 -352 拦过就把 image_delay 调到 1~2
                        # （image_delay 上限 60 秒，见 IMAGE_DELAY_MAX）
                        wait = _float_setting(
                            self.common.interval_seconds, 0.0, lo=0.0, hi=RETRY_WAIT_MAX
                        )
                        jitter = _float_setting(
                            self.cfg.get("image_delay", 0),
                            0.0,
                            lo=0.0,
                            hi=IMAGE_DELAY_MAX,
                        )
                        if jitter:
                            wait += random.uniform(0.0, jitter)
                        if wait:
                            time.sleep(wait)

                    content = self._article_content(chapter, urls)
                    data = self._article_post_data(
                        chapter, content, cover_url=cover_url or urls[0]
                    )
                    self.log.info(
                        "保存专栏草稿 %d/%d：%s", index, len(groups), data["title"]
                    )
                    aid = self._draft_article(data)
                    self.log.info("正式发布专栏 %d/%d（aid=%s）", index, len(groups), aid)
                    final_id = self._submit_article(aid, data)
                    url = f"https://www.bilibili.com/read/cv{final_id}"
                    published.append(url)
                    self.log.info(
                        "专栏发布成功：草稿 aid=%s → 文章 cv%s，%s",
                        aid,
                        final_id,
                        url,
                    )
                    self.progress(
                        "article",
                        index,
                        len(groups),
                        f"第 {index}/{len(groups)} 篇专栏已发布",
                        chapter_key=chapter.key,
                    )
                except (BilibiliRiskControlError, CaptchaRequiredError) as exc:
                    # 风控拦截/要求人机验证时草稿已经建好：留草稿并给出手动
                    # 发布入口，避免用户白传一遍图还得自己重做
                    draft_aid = str(getattr(exc, "aid", "") or "")
                    errors.append(f"第 {index} 篇专栏失败：{exc}")
                    link = self._draft_link(draft_aid)
                    if link:
                        drafts.append(link)
                    self.log.error("第 %d 篇专栏被风控拦截：%s", index, exc)
                    if link:
                        self.log.warning(
                            "第 %d 篇已保存为草稿（aid=%s），可在B站创作中心一键手动发布：%s",
                            index,
                            draft_aid,
                            link,
                        )
                    continue
                except PublisherError as exc:
                    errors.append(f"第 {index} 篇专栏失败：{exc}")
                    self.log.error("第 %d 篇专栏失败：%s", index, exc)
                    continue

            if errors:
                note = ""
                if drafts:
                    note = (
                        "。已保留草稿（B站创作中心 → 投稿管理 → 专栏草稿），"
                        "可在这里手动发布：" + "，".join(drafts)
                    )
                return PublishResult.partial(
                    self.key,
                    chapter,
                    url=published[0] if published else (drafts[0] if drafts else ""),
                    message=f"部分失败：{'; '.join(errors)}{note}",
                    urls=published,
                    drafts=drafts,
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

    def _cover_from_settings(self, chapter: Chapter, pages) -> str | None:
        """按 manga.json 的 bilibili.cover 设置生成/上传封面。

        默认返回 None（用正文第一张图当封面）；可手动选择第一页的截取范围，
        或使用自定义上传的封面文件。
        """
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from PIL import Image

        meta = self._meta(chapter)
        cover = meta.get("cover") if isinstance(meta.get("cover"), dict) else {}
        mode = str(cover.get("mode") or "first")
        src = None
        crop = None
        if mode == "custom":
            custom = chapter.source_dir / "_cover_custom.bin"
            if custom.is_file():
                src = custom
        else:
            raw_crop = cover.get("crop")
            if (
                isinstance(raw_crop, (list, tuple))
                and len(raw_crop) == 4
                and pages
            ):
                x, y, w, h = [float(v) for v in raw_crop]
                if not (x <= 0.001 and y <= 0.001 and w >= 0.999 and h >= 0.999):
                    crop = (x, y, w, h)
                    src = pages[0].path
        if src is None:
            return None
        fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="bcover_")
        import os

        os.close(fd)
        tmp = Path(tmp_path)
        try:
            with Image.open(src) as img:
                image = img.convert("RGB")
                if crop is not None:
                    iw, ih = image.size
                    x, y, w, h = crop
                    box = (
                        max(0, int(x * iw)),
                        max(0, int(y * ih)),
                        min(iw, int((x + w) * iw)),
                        min(ih, int((y + h) * ih)),
                    )
                    if box[2] > box[0] and box[3] > box[1]:
                        image = image.crop(box)
                image.save(tmp, "JPEG", quality=92)
            page = SimpleNamespace(path=tmp, name=tmp.name)
            return self._upload_article_image(page)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

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
            page_done = 0
            for index, group in enumerate(groups, 1):
                try:
                    pics = []
                    for page in group:
                        self.progress(
                            "upload",
                            page_done,
                            len(pages),
                            f"正在上传图片 {page_done + 1}/{len(pages)}"
                            f"（第 {index}/{len(groups)} 条动态）",
                            chapter_key=chapter.key,
                        )
                        self.log.info("上传图片 %s（第 %d/%d 组）", page.path.name, index, len(groups))
                        pics.append(self._upload_dynamic_image(page, category))
                        page_done += 1
                        self.progress(
                            "upload",
                            page_done,
                            len(pages),
                            f"已上传图片 {page_done}/{len(pages)}",
                            chapter_key=chapter.key,
                        )
                    part_caption = caption
                    if len(groups) > 1:
                        part_caption = f"{caption}\n（第 {index}/{len(groups)} 部分）"
                    dyn_id = self._create_dynamic(part_caption, pics)
                    url = f"https://t.bilibili.com/{dyn_id}"
                    published.append(url)
                    self.log.info("动态发布成功：%s", url)
                    self.progress(
                        "dynamic",
                        index,
                        len(groups),
                        f"第 {index}/{len(groups)} 条动态已发布",
                        chapter_key=chapter.key,
                    )
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
