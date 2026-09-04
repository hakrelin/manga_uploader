"""百度贴吧发图帖（2026-09 实测新版 /c/ 接口）。

贴吧网页端在 2026 年已迁移到新的 /c/ 系列接口，旧发帖 CGI
（/f/commit/thread/add）即使把表单按 GBK 编码发送、中文不乱码，也会拒绝
<img class="BDE_Image"> 式 HTML 正文并返回 232000（内容不合法）。

本发布器现在完整复刻网页编辑器：
- tbs：GET https://tieba.baidu.com/dc/common/tbs
- 传图：POST /c/s/uploadPicture_pc（multipart，成功返回 picId + 图床 URL）
- 发帖：POST /c/c/thread/add_pc
- 回帖：POST /c/c/post/add_pc
- 正文：纯文本 + 图片标记 #(pic,<picId>,<宽>,<高>)，
  请求带 is_pictxt=1 与 ext={"needImage":"1"}。

/c/ 系列请求要按网页端算法签名：参数（去掉空值）按 key 升序拼接成
k=v 连续字符串，末尾追加 PC 密钥再取 MD5 hex 作为 sign；请求带
tbs/subapp_type=pc/_client_type=20。BDUSS 必须以 host-only 方式挂在本域。

风控说明：新版接口在普通状态下无需 Acs-Token 也能发帖；只有当响应里
info.need_vcode=1 时才需要人机验证（滑动验证码，需用户在浏览器中完成，
本工具不做自动化绕过）。其余错误按 error_code 给出明确中文，不再笼统报
“验证码/风控”。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
from urllib.parse import quote

from ..models import Chapter, CheckResult, PublishResult
from .. import composer
from ..util import chunk_list
from .base import BasePublisher, CaptchaRequiredError, PublisherError

TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
UPLOAD_URL = "https://tieba.baidu.com/c/s/uploadPicture_pc"
THREAD_ADD_URL = "https://tieba.baidu.com/c/c/thread/add_pc"
POST_ADD_URL = "https://tieba.baidu.com/c/c/post/add_pc"
FORUM_URL = "https://tieba.baidu.com/f"
NEW_MOINDEX_URL = "https://tieba.baidu.com/mo/q/newmoindex"

# 网页端 PC 请求签名密钥（逆向自 tieba pc 前端 base.js）
TIEBA_PC_SIGN_SECRET = "36770b1f34c9bbf2e7d1a99d2b82fa9e"
# 网页端对 File 对象做 md5 时得到的是 md5("[object File]")，resourceId 恒为常量；
# 服务端按 chunk 实际内容去重/入库，因此同一用户连续传多图不会冲突。
TIEBA_FILE_STRING = "[object File]"

def _fmt_error(code: object, message: str) -> str:
    """把贴吧 error_code 转成用户能看懂的中文。"""
    code_str = str(code or "").strip()
    message = (message or "").strip()
    # 已有关键信息时直接返回服务端文案
    if message and not re.fullmatch(r"\d+", message):
        return message
    table = {
        "230274": "该吧已被关闭或不存在，无法发帖",
        "230004": "未登录或登录状态失效，请更新 Cookie",
        "230265": "未登录或登录状态失效，请更新 Cookie",
        "230308": "没有发帖权限（等级/会员/吧规限制或表单校验未通过）",
        "230808": "每层楼插入的视频不能超过 1 个",
        "230809": "每层楼插入的图片不能超过 9 张，请调小 max_pages_per_post",
        "230814": "每层楼插入的表情不能超过 10 个",
        "230815": "每层楼插入的音乐不能超过 10 个",
        "230871": "发贴太频繁，请等待一段时间再试",
        "220034": "发言太快，请放慢节奏再试",
        "230020": "标题或正文包含太少的文字",
        "220011": "帖子标题和内容太长",
        "230046": "帖子过长，无法提交，请拆成多个楼层",
        "230902": "输入的内容过长，请修改后重新提交",
        "230961": "图片地址有错误，请检查后重新发布",
        "232000": "正文格式不被接受（内容不合法），请升级程序后重试",
        "232001": "内容不合法，请检查正文后重试",
        "232007": "内容不合法，请检查正文后重试",
        "230962": "内容不合法，请检查正文后重试",
        "230963": "内容不合法，请检查正文后重试",
        "224010": "账号存在安全风险暂不能发帖，请先在贴吧完成手机绑定",
        "4010": "账号存在安全风险暂不能发帖，请先在贴吧完成手机绑定",
        "230013": "账号因违规操作被封禁，无法发帖",
        "230705": "本吧当前只能浏览，不能发帖",
        "230889": "账号已被加入小黑屋，无法发帖",
        "230901": "该楼回复已达上限，请改用新的楼层",
        "230273": "操作失败，该帖子已不存在",
        "230008": "内容已提交成功，正在审核，请耐心等待",
        "210009": "系统繁忙，请稍后重试",
    }
    return table.get(code_str, f"发帖失败（error_code={code_str}）")


def _need_vcode(payload: object) -> bool:
    """递归判断响应是否真的要求人机验证（need_vcode 显式为 1）。"""
    if isinstance(payload, dict):
        for key in ("need_vcode", "vcode"):
            value = payload.get(key)
            if isinstance(value, dict):
                if _need_vcode(value):
                    return True
            elif str(value).strip() in ("1",):
                return True
        for value in payload.values():
            if isinstance(value, (dict, list)) and _need_vcode(value):
                return True
    elif isinstance(payload, list):
        return any(_need_vcode(item) for item in payload)
    return False


def _pc_sign(params: dict[str, object]) -> dict[str, str]:
    """按网页端算法给 /c/ 接口参数加 sign（key 升序拼接 + PC 密钥 + MD5）。"""
    clean = {str(k): v for k, v in params.items() if v is not None}
    raw = "".join(f"{k}={clean[k]}" for k in sorted(clean))
    raw += TIEBA_PC_SIGN_SECRET
    signed = {k: str(v) for k, v in clean.items()}
    signed["sign"] = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return signed


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
        # 贴吧网页端每楼最多 9 张，配置文件里更大的值会被截断
        return max(1, min(9, int(self.cfg.get("max_pages_per_post", 9))))

    def _floor_plan(self, pages: list) -> tuple[list, list[list]]:
        """分楼：第一楼只放封面（第一张），其余页面每楼最多 max_pages_per_post 张。"""
        if not pages:
            return [], []
        cover = pages[:1]
        rest = pages[1:]
        return cover, chunk_list(rest, self.max_pages_per_post)

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
        posts = 1 + max(0, -(-max(pages - 1, 0) // self.max_pages_per_post))
        return [
            f"发帖标题：{composer.platform_title(chapter, self.key)}",
            f"目标贴吧：{self._forum(chapter)}",
            f"上传 {pages} 张图片：第 1 楼放封面，其余每楼最多 {self.max_pages_per_post} 张，预计 1 帖 {posts} 楼",
            f"正文：{composer.platform_body(chapter, self.key)[:120]}",
        ]

    def full_preview(self, chapter: Chapter) -> list[str]:
        """贴吧发布前全文预览：展示真实标题、正文与分楼顺序。"""
        from ..util import human_size

        pages = len(chapter.pages)
        posts = 1 + max(0, -(-max(pages - 1, 0) // self.max_pages_per_post))
        lines = [
            "发布平台：百度贴吧",
            f"标题：{composer.platform_title(chapter, self.key)}",
            f"目标贴吧：{self._forum(chapter)}",
            f"第 1 楼：简介 + 封面（1 张）",
            f"后续楼层：其余 {max(pages - 1, 0)} 张，每楼最多 {self.max_pages_per_post} 张，共 {posts} 楼",
        ]
        body = composer.platform_body(chapter, self.key)
        if body:
            lines.append("一楼正文：")
            for part in body.splitlines():
                lines.append("  " + part)
        self._append_page_preview(lines, chapter)
        return lines

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

        # 优先用“我的吧”接口（返回关注列表 forum_name + forum_id），
        # 避免吧页 HTML 被百度登录墙反复重定向
        wanted = re.sub(r"\s+", "", forum).rstrip("吧").lower()
        try:
            data = self.http.get_json(NEW_MOINDEX_URL)
            items = ((data.get("data") or {}).get("like_forum")) or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = re.sub(r"\s+", "", str(item.get("forum_name") or "")).rstrip("吧").lower()
                fid = item.get("forum_id")
                if name == wanted and fid:
                    return str(fid)
        except Exception as exc:
            self.log.warning("读取贴吧关注列表失败，尝试解析吧页：%s", exc)

        url = f"{FORUM_URL}?kw={quote(forum)}"
        resp = self.http.get(url, allow_redirects=False)
        text = resp.text
        if resp.status_code in (301, 302, 303, 307, 308) or "passport.baidu.com" in text:
            raise PublisherError(
                f"无法自动获取 {forum} 的 fid：百度把吧页重定向到了登录页。"
                "请在 config.yaml 的 tieba.settings.fid 手动填写"
                "（浏览器打开该吧后查看源码中的 fid），或先关注该吧后重试。"
            )
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
            f"无法从贴吧解析 fid（{forum}）。"
            "请在 config.yaml 的 tieba.settings.fid 里手动填写"
            "（浏览器打开该吧后查看源码中的 fid），或先关注该吧后重试。"
        )

    def _upload_image(self, page, tbs: str, forum: str) -> dict:
        """用 /c/s/uploadPicture_pc 上传单张图片，返回 picId/宽/高/URL。"""
        mime = mimetypes.guess_type(page.path.name)[0] or "image/jpeg"
        payload = _pc_sign(
            {
                "resourceId": hashlib.md5(TIEBA_FILE_STRING.encode("utf-8")).hexdigest(),
                "isFinish": "1",
                "saveOrigin": "1",
                "size": page.path.stat().st_size,
                "width": "120",
                "height": "120",
                "chunkNo": "1",
                "pic_water_type": "3",
                # JS 里 chunk 是 File，字符串化后参与签名
                "chunk": TIEBA_FILE_STRING,
                "tbs": tbs,
                "subapp_type": "pc",
                "_client_type": 20,
            }
        )
        fields = {key: value for key, value in payload.items() if key != "chunk"}
        with open(page.path, "rb") as fh:
            resp = self.http.post(
                UPLOAD_URL,
                data=fields,
                files={"chunk": (page.path.name, fh, mime)},
                headers={
                    "Referer": f"{FORUM_URL}?kw={quote(forum)}&ie=utf-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Origin": "https://tieba.baidu.com",
                },
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            self.http._dump(resp, tag="tieba-upload")
            raise PublisherError(f"贴吧传图接口未返回 JSON：{resp.text[:200]}") from exc
        error_code = payload.get("error_code")
        if error_code not in (None, "", 0, "0"):
            self.http._dump(resp, tag="tieba-upload")
            message = str(payload.get("error_msg") or payload.get("error") or payload)[:300]
            raise PublisherError(f"贴吧传图失败：{_fmt_error(error_code, message)}")
        pic_info = payload.get("picInfo") or {}
        origin = pic_info.get("originPic") or {}
        big = pic_info.get("bigPic") or {}
        url = origin.get("picUrl") or big.get("picUrl")
        if not url:
            url = _find_first(payload, ("picUrl", "imgurl", "img_url", "pic_url", "url"))
        if not url:
            self.http._dump(resp, tag="tieba-upload")
            raise PublisherError(f"贴吧传图失败，响应中找不到图片地址：{str(payload)[:300]}")
        pic_id = str(payload.get("picId") or _find_first(payload, ("pic_id", "picId")) or "")
        return {
            "pic_id": pic_id,
            "url": str(url),
            "width": str(origin.get("width") or big.get("width") or page.width or ""),
            "height": str(origin.get("height") or big.get("height") or page.height or ""),
        }

    def _build_text(self, description: str, images: list[dict]) -> str:
        """按网页编辑器格式构造纯文本正文：简介行 + #(pic,picId,宽,高)。"""
        lines: list[str] = []
        if description:
            lines.extend(line for line in description.splitlines() if line)
        for image in images:
            lines.append(
                "#(pic,{},{},{})".format(
                    image["pic_id"], image["width"], image["height"]
                )
            )
        return "\r\n".join(lines)

    def _parse_add_response(self, resp, tag: str, kind: str) -> dict:
        """解析 /c/c/.../add_pc 响应；成功返回 data 字段，失败抛明确错误。"""
        try:
            payload = resp.json()
        except ValueError as exc:
            self.http._dump(resp, tag=tag)
            raise PublisherError(f"贴吧{kind}失败，响应不是 JSON：{resp.text[:200]}") from exc
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        error_code = (
            data.get("error_code")
            if isinstance(data, dict)
            else payload.get("error_code")
        )
        if error_code not in (None, "", 0, "0"):
            self.http._dump(resp, tag=tag)
            info = payload.get("info") if isinstance(payload, dict) else {}
            message = str(
                payload.get("error_msg")
                or payload.get("msg")
                or payload.get("errmsg")
                or info.get("error_msg")
                or ""
            )
            reason = _fmt_error(error_code, message)
            if _need_vcode(payload):
                raise CaptchaRequiredError(
                    f"贴吧{kind}需要人机验证（验证码）。请在浏览器中打开贴吧完成一次验证后重试。"
                )
            raise PublisherError(f"贴吧{kind}失败：{reason}")
        if payload.get("msg") == "发送成功":
            return {"tid": str(payload.get("tid") or ""), "pid": str(payload.get("pid") or "")}
        tid = str(
            payload.get("tid")
            or (data.get("tid") if isinstance(data, dict) else "")
            or ""
        )
        pid = str(
            payload.get("pid")
            or (data.get("pid") if isinstance(data, dict) else "")
            or ""
        )
        if not (tid or pid):
            self.http._dump(resp, tag=tag)
            raise PublisherError(f"贴吧{kind}成功但响应中没有帖子编号：{resp.text[:200]}")
        return {"tid": tid, "pid": pid}

    def _post_thread(self, forum: str, fid: str, tbs: str, title: str, content: str) -> str:
        data = _pc_sign(
            {
                "kw": forum,
                "fid": fid,
                "title": title,
                "content": content,
                "is_pictxt": "1",
                "ext": json.dumps({"is_hide": None, "needImage": "1"}, ensure_ascii=False),
                "post_prefix": "",
                "jt": "",
                "tbs": tbs,
                "subapp_type": "pc",
                "_client_type": 20,
            }
        )
        resp = self.http.post(
            THREAD_ADD_URL,
            data=data,
            headers={
                "Referer": f"{FORUM_URL}?kw={quote(forum)}&ie=utf-8",
                "Origin": "https://tieba.baidu.com",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        result = self._parse_add_response(resp, "tieba-thread", "发帖")
        if not result["tid"]:
            raise PublisherError(f"贴吧发帖成功但未返回 tid：{resp.text[:200]}")
        return result["tid"]

    def _reply_post(self, forum: str, fid: str, tbs: str, tid: str, content: str) -> str:
        """用 /c/c/post/add_pc 给主题帖追加楼层。"""
        data = _pc_sign(
            {
                "kw": forum,
                "fid": fid,
                "tid": tid,
                "name_show": "",
                "content": content,
                "quote_id": "",
                "repostid": "",
                "sub_post_id": "",
                "jt": "",
                "tbs": tbs,
                "subapp_type": "pc",
                "_client_type": 20,
            }
        )
        resp = self.http.post(
            POST_ADD_URL,
            data=data,
            headers={
                "Referer": f"https://tieba.baidu.com/p/{tid}",
                "Origin": "https://tieba.baidu.com",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        result = self._parse_add_response(resp, "tieba-post", "追加楼层")
        return result["pid"]

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
            cover_group, rest_groups = self._floor_plan(pages)
            groups = [cover_group] + rest_groups
            thread_tid: str | None = None
            page_done = 0
            for index, group in enumerate(groups, 1):
                try:
                    images: list[dict] = []
                    for page in group:
                        self.progress(
                            "upload",
                            page_done,
                            len(pages),
                            f"正在上传图片 {page_done + 1}/{len(pages)}：{page.path.name}"
                            f"（第 {index}/{len(groups)} 楼）",
                            chapter_key=chapter.key,
                        )
                        self.log.info("上传图片 %s（第 %d/%d 组）", page.path.name, index, len(groups))
                        images.append(self._upload_image(page, tbs, forum))
                        page_done += 1
                        self.progress(
                            "upload",
                            page_done,
                            len(pages),
                            f"已上传图片 {page_done}/{len(pages)}",
                            chapter_key=chapter.key,
                        )
                        time.sleep(float(self.cfg.get("upload_sleep", 1.0) or 0))

                    # 正文只放主题帖一楼，后续楼层只放图片，避免每楼重复
                    description = composer.platform_body(chapter, self.key) if thread_tid is None else ""
                    content = self._build_text(description, images)

                    title = composer.platform_title(chapter, self.key)
                    if thread_tid is None:
                        thread_tid = self._post_thread(forum, fid, tbs, title[:80], content)
                        url = f"https://tieba.baidu.com/p/{thread_tid}"
                        published.append(url)
                        self.log.info("主题帖发布成功：%s", url)
                    else:
                        self._reply_post(forum, fid, tbs, thread_tid, content)
                        self.log.info("已追加楼层 %d 到 %s", index, thread_tid)
                except PublisherError as exc:
                    errors.append(str(exc))
                    self.log.error("第 %d 组发帖失败：%s", index, exc)
                    if isinstance(exc, CaptchaRequiredError):
                        break  # 验证码需人工处理，停止后续组避免反复触发

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
