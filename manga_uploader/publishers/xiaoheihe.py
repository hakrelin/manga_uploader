"""小黑盒（xiaoheihe.cn）图文发布。

小黑盒没有公开的发帖接口，网页“创作中心”走带签名的 api.xiaoheihe.cn。
本实现按网页端行为复刻（2026-09 实测）：

1. 签名：每个请求带 hkey/_time/nonce + 平台参数。hkey 由路径、时间戳、
   nonce 经站点字符映射/MD5 混淆生成，纯 Python 实现（无 Node 依赖）。
2. 传图：POST /bbs/app/api/qcloud/cos/upload/info/v2 预占位拿
   bucket/key → POST token/v2 拿 COS 临时密钥 → 用腾讯 COS SDK 直传
   → POST callback/v2 确认并拿到 CDN 地址。
3. 发文：POST /bbs/app/api/link/post。正文结构与网页编辑器一致：
   text 字段是 JSON 数组 [{"type":"text","text":"<p>…</p>"},
   {"type":"img","url":…,"width":…,"height":…}]；
   link_tag=27 图文 / 11 文章；draft=1 只存草稿（页面
   /creator/editor 可见），不传即正式发布。

页面限制（/bbs/app/profile/post/limits 实测）：图文单帖最多 30 张图、
文章单帖最多 100 张图、标题 ≤30 字、正文 ≤30000 字；新号发帖有频控
（10006 发帖频率过快，需等待）。漫画页数超过单帖上限时自动拆成多帖。

Cookie 说明：直接粘贴访问 xiaoheihe.cn 时的整段 Cookie 即可
（含 pkey/user_pkey/heybox_id/session_token 等）。配置键为
platforms.xiaoheihe.cookies.cookie（整段文本），heybox_id 也直接
从这段文本中解析，无需单独填写。
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import random
import time
from urllib.parse import urlencode

from ..models import Chapter, CheckResult, PublishResult
from .. import composer
from ..util import chunk_list, human_size, mask_secret
from .base import BasePublisher, PublisherError

API = "https://api.xiaoheihe.cn"
WEB = "https://www.xiaoheihe.cn"
EDITOR_URL = f"{WEB}/creator/editor/draft/image_text"

INFO_URL = "/bbs/app/api/qcloud/cos/upload/info/v2"
TOKEN_URL = "/bbs/app/api/qcloud/cos/upload/token/v2"
CALLBACK_URL = "/bbs/app/api/qcloud/cos/upload/callback/v2"
POST_URL = "/bbs/app/api/link/post"
DELETE_URL = "/bbs/app/link/delete"
EDIT_INFO_URL = "/bbs/app/link/edit/info"
TOPIC_SELECT_URL = "/bbs/app/api/post_editor/topic_selection/index"

_CHARSET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
_DEFAULT_DEVICE_ID = "2c2fef8385ccef915e3b3caf94e3aa06"

# 图文单帖上限（网页实测 pic_link_limit.pic_limit = 30）
DEFAULT_MAX_PAGES_PER_POST = 30


# ---------------------------------------------------------------- 签名

def _f3(e: int) -> int:
    return ((e << 1) ^ 27) & 255 if e & 128 else (e << 1) & 255


def _pc(e: int) -> int:
    return _f3(e) ^ e


def _sf(e: int) -> int:
    return _pc(_f3(e))


def _lh(e: int) -> int:
    return _sf(_pc(_f3(e)))


def _ig(e: int) -> int:
    return _lh(e) ^ _sf(e) ^ _pc(e)


def _km(arr: list[int]) -> list[int]:
    t = [
        _ig(arr[0]) ^ _lh(arr[1]) ^ _sf(arr[2]) ^ _pc(arr[3]),
        _pc(arr[0]) ^ _ig(arr[1]) ^ _lh(arr[2]) ^ _sf(arr[3]),
        _sf(arr[0]) ^ _pc(arr[1]) ^ _ig(arr[2]) ^ _lh(arr[3]),
        _lh(arr[0]) ^ _sf(arr[1]) ^ _pc(arr[2]) ^ _ig(arr[3]),
    ]
    # 与站点实现一致：只替换前 4 位，返回完整数组（后 2 位参与求和）
    arr[0], arr[1], arr[2], arr[3] = t
    return arr


def _map_char(text: str, charset: str, end: int) -> str:
    head = charset[:end]
    return "".join(head[ord(ch) % len(head)] for ch in text)


def _map_str(text: str, charset: str) -> str:
    return "".join(charset[ord(ch) % len(charset)] for ch in text)


def _interleave(parts: list[str]) -> str:
    out: list[str] = []
    for i in range(max(len(p) for p in parts)):
        for part in parts:
            if i < len(part):
                out.append(part[i])
    return "".join(out)


def _hkey(path: str, timestamp: int, nonce: str) -> str:
    """小黑盒 hkey：字符映射 + 交错 + MD5 + 字节混淆校验和。"""
    path = "/" + "/".join(p for p in path.split("/") if p) + "/"
    comp1 = _map_char(str(timestamp), _CHARSET, -2)
    comp2 = _map_str(path, _CHARSET)
    comp3 = _map_str(nonce, _CHARSET)
    inter = _interleave([comp1, comp2, comp3])[:20]
    digest = hashlib.md5(inter.encode("utf-8")).hexdigest()
    checksum = sum(_km([ord(c) for c in digest[-6:]])) % 100
    prefix = _map_char(digest[:5], _CHARSET, -4)
    return prefix + str(checksum).zfill(2)


def _nonce() -> str:
    return hashlib.md5(
        f"{time.time()}{random.random()}{os.urandom(8).hex()}".encode()
    ).hexdigest().upper()


def _signed_url(
    path: str,
    *,
    user_id: str = "",
    device_id: str = "",
    extra: dict | None = None,
) -> str:
    """给相对路径加上完整签名查询参数。"""
    ts = int(time.time())
    nonce = _nonce()
    params: dict[str, object] = {
        "os_type": "web",
        "app": "heybox",
        "client_type": "web",
        "version": "999.0.4",
        "web_version": "2.5",
        "x_client_type": "web",
        "x_app": "heybox_website",
        "heybox_id": user_id,
        "x_os_type": "Windows",
        "device_info": "Chrome",
        "device_id": device_id or _DEFAULT_DEVICE_ID,
        "hkey": _hkey(path, ts, nonce),
        "_time": ts,
        "nonce": nonce,
    }
    if extra:
        for key, value in extra.items():
            if value is not None:
                params[key] = value
    return f"{API}{path}?{urlencode(params)}"


def _api_error(payload: dict) -> str:
    msg = str(payload.get("msg") or "")
    return msg or json.dumps(payload, ensure_ascii=False)[:300]


def _text_html(plain: str) -> str:
    """正文纯文本 → 网页编辑器的 text 块内容（每行一个 <p>）。"""
    if not plain.strip():
        return "<p></p>"
    lines = []
    for line in plain.splitlines() or [plain]:
        line = line.strip()
        if not line:
            continue
        escaped = (
            line.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        lines.append(f"<p>{escaped}</p>")
    return "".join(lines)


def _content_json(description: str, pages: list) -> str:
    """组装图文帖 text 字段（网页编辑器同一 JSON 结构）。"""
    blocks: list[dict] = []
    if description.strip():
        blocks.append({"type": "text", "text": _text_html(description)})
    for page in pages:
        blocks.append(
            {
                "type": "img",
                "url": page.url,
                "width": page.width or 0,
                "height": page.height or 0,
            }
        )
    return json.dumps(blocks, ensure_ascii=False)


# ---------------------------------------------------------------- 发布器


class XiaoheihePublisher(BasePublisher):
    key = "xiaoheihe"
    display_name = "小黑盒"

    def __init__(self, cfg, common, output_dir=None):
        super().__init__(cfg, common, output_dir=output_dir)
        # 小黑盒需要把用户粘贴的整段 Cookie 原样作为请求头
        cookie_text = str(cfg.cookies.get("cookie") or "").strip()
        if cookie_text:
            self.http.session.headers["Cookie"] = cookie_text
            self.log.info(
                "已加载小黑盒 Cookie（长度 %s，前段 %s）",
                len(cookie_text),
                mask_secret(cookie_text[:32]),
            )

    @property
    def max_pages_per_post(self) -> int:
        return max(1, int(self.cfg.get("max_pages_per_post", DEFAULT_MAX_PAGES_PER_POST)))

    @property
    def user_id(self) -> str:
        return str(self.cfg.cookies.get("heybox_id") or "").strip()

    @property
    def cookie_text(self) -> str:
        return str(self.cfg.cookies.get("cookie") or "").strip()

    @property
    def nickname(self) -> str:
        return str(self.cfg.cookies.get("nickname") or "").strip()

    def _user_id_from_cookie(self) -> str:
        """从整段 Cookie 中解析 heybox_id（未单独填时用）。"""
        if self.user_id:
            return self.user_id
        for part in self.cookie_text.split(";"):
            key, _, value = part.strip().partition("=")
            if key.strip() == "heybox_id":
                return value.strip()
        return ""

    def _device_id(self) -> str:
        return str(self.cfg.get("device_id") or "").strip() or _DEFAULT_DEVICE_ID

    def _title(self, chapter: Chapter) -> str:
        """小黑盒标题：中文标题（平台覆盖优先），截断到 30 字。"""
        meta = self._meta(chapter)
        if str(meta.get("title") or "").strip():
            title = str(meta.get("title") or "").strip()
        else:
            title = str(chapter.title or "").strip()
        if not title:
            title = composer.platform_title(chapter, self.key)
        return title[:30]

    def _description(self, chapter: Chapter) -> str:
        """图文正文：作者/社团/简介（平台整段覆盖优先）。"""
        meta = self._meta(chapter)
        if str(meta.get("description") or "").strip():
            return str(meta.get("description") or "").strip()
        fields = composer.fields(chapter, self.key)
        return composer.build_credit_lines(
            fields["author"], fields["circle"], fields["description"]
        )

    def _signed(self, path: str, extra: dict | None = None) -> str:
        user_id = self._user_id_from_cookie()
        return _signed_url(
            path,
            user_id=user_id,
            device_id=self._device_id(),
            extra=extra,
        )

    # ---------- 接口 ----------

    def check(self) -> CheckResult:
        missing = self.missing_cookies()
        if missing:
            return CheckResult(
                self.key,
                False,
                f"缺少 Cookie：{', '.join(missing)}（请粘贴访问 xiaoheihe.cn 时的整段 Cookie）",
            )
        try:
            # topic_selection 是编辑器“选社区/话题”接口：登录态可用即视为检查通过
            payload = self.http.get_json(
                self._signed(TOPIC_SELECT_URL),
                headers={"Referer": EDITOR_URL},
            )
        except Exception as exc:
            return CheckResult(self.key, False, f"网络请求失败：{exc}")
        if payload.get("status") != "ok":
            msg = _api_error(payload)
            if msg in ("请登录后使用该功能", "登录已失效", "非法的请求") or "登录" in msg:
                return CheckResult(self.key, False, f"登录失效：{msg}")
            return CheckResult(self.key, False, f"接口返回异常：{msg}")
        result = payload.get("result") or {}
        topics = result.get("topic_list") or []
        if topics:
            name = self.nickname or self._user_id_from_cookie() or "未知用户"
            uid = self._user_id_from_cookie() or "未知"
            max_pic = self.max_pages_per_post
            return CheckResult(
                self.key,
                True,
                f"已登录：{name}（uid {uid}），"
                f"图文单帖上限 {max_pic} 张",
            )
        return CheckResult(self.key, False, "登录态可用但未取到可发布社区（可能是新号/风控）")

    def plan(self, chapter: Chapter) -> list[str]:
        pages = len(chapter.pages)
        limit = self.max_pages_per_post
        posts = max(1, math.ceil(pages / limit)) if pages else 0
        rows = [
            f"发布方式：小黑盒图文（image_text，每帖最多 {limit} 张图）",
            f"标题：{self._title(chapter)}",
            f"共 {pages} 张图，预计拆成 {posts} 条图文",
            f"正文：{(self._description(chapter)[:80] + '…') if len(self._description(chapter)) > 80 else self._description(chapter)}",
        ]
        if not self.cfg.get("publish_draft", False):
            rows.append("发布后为公开内容（可在小黑盒删除）；如需先存草稿请设置 publish_draft=true")
        return rows

    def full_preview(self, chapter: Chapter) -> list[str]:
        """发布前全文预览：展示真正提交的正文 JSON 与图片顺序。"""
        from ..comic import page_sequence_warnings

        description = self._description(chapter)
        pages = chapter.pages
        limit = self.max_pages_per_post
        groups = chunk_list(pages, limit)
        lines = [
            "发布平台：小黑盒（图文 image_text）",
            f"标题：{self._title(chapter)}",
            f"正文：{(description[:80] + '…') if len(description) > 80 else description}",
            f"将提交的 text 字段（JSON 数组，正文转 <p>，图片带 url/宽高）：",
            f"  {_content_json(description, [])[:200] + '…' if description else '  （无正文文本，纯图帖）'}",
            f"共 {len(pages)} 张图，拆成 {len(groups)} 帖（每帖最多 {limit} 张）：",
        ]
        for index, group in enumerate(groups, 1):
            lines.append(f"  ── 第 {index} 帖（{len(group)} 张）──")
            for page_index, page in enumerate(group, 1):
                lines.append(
                    f"    [{page_index:>3}] {page.name}（{human_size(page.stat().st_size)}）"
                )
        warnings = page_sequence_warnings(pages)
        if warnings:
            lines.append("⚠ 检查发现：")
            for warning in warnings:
                lines.append("  - " + warning)
        else:
            lines.append("✓ 页面顺序连续，未发现重复或明显漏号")
        return lines

    def publish(self, chapter: Chapter) -> PublishResult:
        self.require_cookies()
        if not chapter.pages:
            return PublishResult.skipped(self.key, chapter, "没有图片")

        pages = self.prepare_pages(chapter)
        try:
            published: list[str] = []
            errors: list[str] = []
            groups = chunk_list(pages, self.max_pages_per_post)
            description = self._description(chapter)
            title = self._title(chapter)
            total = len(groups)
            for index, group in enumerate(groups, 1):
                try:
                    self.log.info(
                        "小黑盒 上传第 %d/%d 帖图片（%d 张）",
                        index,
                        total,
                        len(group),
                    )
                    uploaded = []
                    for page_index, page in enumerate(group, 1):
                        item = self._upload_page(page)
                        uploaded.append(item)
                        self.log.info(
                            "小黑盒 第 %d/%d 帖图片 %d/%d 上传完成：%s",
                            index,
                            total,
                            page_index,
                            len(group),
                            item.url,
                        )
                        if self.common.interval_seconds:
                            time.sleep(float(self.common.interval_seconds))

                    post_title = title
                    post_desc = description
                    if total > 1:
                        post_desc = f"{description}\n（第 {index}/{total} 部分）".strip()
                    body = {
                        "text": _content_json(post_desc, uploaded),
                        "title": post_title,
                        "desc": "",
                        "words_count": len(post_desc),
                        "post_card_ids": "",
                        "link_tag": 27,
                        "view_limit": 1,
                        "topic_ids": str(self.cfg.get("topic_id", 1) or 1),
                        "original_info": json.dumps(
                            {"original": 1}, ensure_ascii=False
                        ),
                    }
                    if self.cfg.get("publish_draft", False):
                        body["draft"] = 1
                    resp = self.http.post(
                        self._signed(POST_URL),
                        data=body,
                        headers={"Referer": EDITOR_URL},
                    )
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise PublisherError(
                            f"小黑盒 发布接口未返回 JSON：{resp.text[:200]}"
                        ) from exc
                    if payload.get("status") != "ok":
                        msg = _api_error(payload)
                        if "10006" in str(payload.get("msg") or ""):
                            msg = (
                                f"{msg}（发帖频率过快，请间隔几分钟再试；"
                                "可在 config 里调大 common.interval_seconds）"
                            )
                        raise PublisherError(f"小黑盒 发布失败：{msg}")
                    link_id = str(payload.get("link_id") or "").strip()
                    if not link_id:
                        raise PublisherError(
                            f"小黑盒 发布响应缺少 link_id：{payload}"
                        )
                    url = f"{WEB}/app/bbs/link/{link_id}"
                    published.append(url)
                    self.log.info("小黑盒 发布成功：%s", url)
                except PublisherError as exc:
                    errors.append(f"第 {index} 帖失败：{exc}")
                    self.log.error("小黑盒 第 %d 帖失败：%s", index, exc)
                    continue

            if errors:
                return PublishResult.partial(
                    self.key,
                    chapter,
                    url=published[0] if published else "",
                    message=f"部分失败：{'; '.join(errors)}",
                    urls=published,
                    mode="image_text",
                    pages=len(pages),
                    draft=bool(self.cfg.get("publish_draft", False)),
                )
            note = (
                f"已存草稿 {len(published)} 条"
                if self.cfg.get("publish_draft", False)
                else (f"已拆成 {len(published)} 条图文" if len(published) > 1 else "已发布图文")
            )
            return PublishResult.ok(
                self.key,
                chapter,
                url=published[0],
                message=f"{note}，共 {len(pages)} 页",
                urls=published,
                mode="image_text",
                pages=len(pages),
            )
        finally:
            self.cleanup_prepared(chapter)

    # ---------- 图片上传 ----------

    def _upload_page(self, page):
        """单张图片：预占位 → COS 直传 → 回调，返回带 url/宽高的记录。"""
        mime = mimetypes.guess_type(page.path.name)[0] or "image/jpeg"
        file_info = {
            "name": page.path.name,
            "mimetype": mime,
            "fsize": page.size_bytes,
            "width": page.width,
            "height": page.height,
            "duration": 0,
        }
        # 1) 预占位：拿 bucket/key/region
        resp = self.http.post(
            self._signed(INFO_URL),
            data={
                "scope": "any",
                "need_cache": "0",
                "file_infos": json.dumps([file_info], ensure_ascii=False),
            },
            headers={"Referer": EDITOR_URL},
        )
        payload = resp.json()
        if payload.get("status") != "ok":
            raise PublisherError(
                f"小黑盒 获取上传信息失败：{_api_error(payload)}"
            )
        result = payload.get("result") or {}
        key = (result.get("keys") or [""])[0]
        bucket = str(result.get("bucket") or "")
        region = str(result.get("region") or "")
        if not key or not bucket or not region:
            raise PublisherError(f"小黑盒 上传信息不完整：{payload}")

        # 2) COS 临时密钥
        resp = self.http.post(
            self._signed(TOKEN_URL),
            data={
                "bucket": bucket,
                "keys": json.dumps([key]),
                "mimetypes": json.dumps([mime]),
                "is_multipart_upload": 0,
            },
            headers={"Referer": EDITOR_URL},
        )
        payload = resp.json()
        if payload.get("status") != "ok":
            raise PublisherError(
                f"小黑盒 获取上传凭证失败：{_api_error(payload)}"
            )
        cred = (payload.get("result") or {}).get("credentials") or {}
        secret_id = str(cred.get("tmpSecretId") or "")
        secret_key = str(cred.get("tmpSecretKey") or "")
        session_token = str(cred.get("sessionToken") or "")
        if not all((secret_id, secret_key, session_token)):
            raise PublisherError(f"小黑盒 上传凭证不完整：{payload}")

        # 3) COS 直传（必须真实落盘，回调不校验文件是否存在）
        from qcloud_cos import CosConfig, CosS3Client
        from qcloud_cos.cos_exception import CosServiceError

        cos_client = CosS3Client(
            CosConfig(
                Region=region,
                SecretId=secret_id,
                SecretKey=secret_key,
                Token=session_token,
                Scheme="https",
            )
        )
        try:
            with open(page.path, "rb") as fh:
                cos_client.put_object(
                    Bucket=bucket,
                    Key=key.lstrip("/"),
                    Body=fh,
                    ContentType=mime,
                )
        except CosServiceError as exc:
            raise PublisherError(
                f"小黑盒 COS 上传失败：{getattr(exc, 'get_message', None) or exc}"
            ) from exc

        # 4) 回调：确认上传，拿 CDN 地址
        resp = self.http.post(
            self._signed(CALLBACK_URL),
            data={"is_finished": "true", "keys": json.dumps([key])},
            headers={"Referer": EDITOR_URL},
        )
        payload = resp.json()
        if payload.get("status") != "ok":
            raise PublisherError(f"小黑盒 上传确认失败：{_api_error(payload)}")
        result = payload.get("result") or {}
        urls = result.get("preview_urls") or []
        url = str(urls[0]) if urls else ""
        if not url:
            raise PublisherError(f"小黑盒 上传确认缺少图片地址：{payload}")
        return _UploadedPage(url, page.width, page.height)


class _UploadedPage:
    """上传成功后的页面记录（发布正文用）。"""

    def __init__(self, url: str, width: int, height: int) -> None:
        self.url = url
        self.width = width
        self.height = height


# 兼容 plan 里对 pages 的引用（仅类型提示）
