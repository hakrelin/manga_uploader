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
# 个人中心接口：返回当前登录账号昵称（/dc/common/tbs 只有 is_login，没有昵称）
SYS_USER_URL = "https://tieba.baidu.com/i/sys/user_json"

# 网页端 PC 请求签名密钥（逆向自 tieba pc 前端 base.js）
TIEBA_PC_SIGN_SECRET = "36770b1f34c9bbf2e7d1a99d2b82fa9e"
# 网页端对 File 对象做 md5 时得到的是 md5("[object File]")，resourceId 恒为常量；
# 服务端按 chunk 实际内容去重/入库，因此同一用户连续传多图不会冲突。
TIEBA_FILE_STRING = "[object File]"

# ---------------------------------------------------------- 乱码自愈
# 贴吧响应历史上出现过两种“伪中文”：
#   1) 真 GBK 正文被按 UTF-8 解 → 锟斤拷 式的替换字符；
#   2) UTF-8 正文被按 GBK（或 Big5）解 → 闈為潪涔勶紙灏忎竴娈典匠璇濓級。
# 第 2 种在旧版本客户端（把 UTF-8 响应当 GBK 解）里很常见，账号昵称被搬来
# 搬去时也可能留下这种痕迹。这里做一次反向解码自愈：只有解出来的文本“更像
# 正常中文”（常用字明显更多）时才采用，解不回来或本来就是正常中文则原样返回，
# 不会把好端端的昵称改坏。
_MOJIBAKE_COMMON = frozenset(
    "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去能对小多然于心"
    "学么之都好看起发当没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动方期它头经长儿回位分"
    "爱老因很给名法间斯知世什两次使身者被高已亲其进此话常与活正感见明问力理尔点文几定本公特做外孩相西果走将月十实向"
    "声车全信重三机工物气每并别真打太新比才便夫再书部水像眼等体却加电主界门利海受听表德少克代员许先口由死安写性马光"
    "白或住难望教命花结乐色更拉东神记处让母父应直字场平报友关放至张认接告入笑内英军候民岁往何度山觉路带万男边风解叫"
    "任金快原吃妈变通师立象数四失满战远格士音轻目条呢病始达深完今提求清王化空业思切怎非找片罗钱吗语元喜曾离飞科言干"
    "流欢约各即指合反题必该论交终林请医晚制球决传画保读运及则房早院量苦火布品近坐产答星精视五连司巴奇管类未朋且婚台"
    "夜青北队久乎越观落尽形影红爸百令周吧识步希亚术留市半热送兴造谈容极随演收首根讲整式取照办强石古华拿计您装似足双"
    "妻尼转诉米称丽客南领节衣站黑刻统断福城故历惊脸选包紧争另建维绝树系伤示愿持千史谁准联妇纪基买志静阿诗独复痛消社"
    "算义竟确酒需单治卡幸兰念举仅钟怕共毛句息功官待究跟穿室易游程号居考突皮哪费倒价图具刚脑永歌响商礼细黄块脚味灵"
    "改据般破引食仍存众注笔甚某沉血备习校默务土微娘须试怀料调广苏显赛查密议底列富梦错座参八除跑亮假印设线温虽掉京初"
    "养香停际致阳纸李纳验助激够严证帝饭忘趣支春集丈木研班普导顿睡展跳获艺六波察群皇段急庭创区奥器谢弟店否害草排背止"
    "组州朝封睛板角况曲馆育忙质河续哥呼若推境遇雨标姐充围案伦护冷警贝著雪索剧啊船险烟依斗值帮汉慢佛肯闻唱沙局伯族低"
    "玩资屋击速顾泪洲团圣旁堂兵七露园牛哭旅街劳型烈姑陈莫鱼异抱宝权鲁简态级票怪寻杀律胜份汽右洋范床舞秘午登楼贵吸责"
    "例追较职属渐左录丝牙党继托赶章智冲叶胡吉卖坚喝肉遗救修松临藏担戏善卫药悲敢靠伊村戴词森耳差短祖云规窗散迷油旧适"
    "乡架恩投弹铁博雷府压超负勒杂醒洗采毫嘴毕九冰既状乱景席珍童顶派素脱农疑练野按犯拍征坏骨余承置臂彩灯巨琴免环姆暗"
    "换技翻束增忍餐洛塞缺忆判欧层付阵玛批岛项狗休懂武革良恶恋委拥娜妙探呀营退摇弄桌熟诺宣银势奖宫忽套康供优课鸟喊降"
    "夏困刘罪亡鞋健模败伴守挥鲜财孤枪禁恐伙杰迹妹遍盖副坦牌江顺秋萨菜划授归浪凡预奶雄升编典袋莱含盛济蒙棋端腿招释介"
    "烧误"
)


def _mojibake_score(text: str) -> int:
    """文本里常用汉字的个数：乱码越多，这个值越小。"""
    return sum(1 for ch in text if ch in _MOJIBAKE_COMMON)


def _repair_mojibake(text: object) -> str:
    """把“UTF-8 正文被按 GBK/Big5 解”的伪中文还原成正常中文。

    例：闈為潪涔勶紙灏忎竴娈典匠璇濓級 → 非非乄（小一段佳话）。
    还原结果不像正常中文时原样返回，因此对正常昵称是安全的（幂等）。
    """
    value = str(text or "")
    if not value:
        return value
    for codec in ("gbk", "big5"):
        try:
            fixed = value.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed and fixed != value and _mojibake_score(fixed) > _mojibake_score(value):
            return fixed
    return value


def _fmt_error(code: object, message: str) -> str:
    """把贴吧 error_code 转成用户能看懂的中文。"""
    code_str = str(code or "").strip()
    message = _repair_mojibake(message).strip()
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
        "2230204": "传图被百度拒绝（多为上传过快的限流/风控），稍后重试即可",
        "2230201": "图片格式或尺寸不被接受，请换 jpg/png 后重试",
        "210009": "系统繁忙，请稍后重试",
    }
    # 已知错误码优先用中文说明（服务端经常只回“上传失败”这种没信息量的文案）
    if code_str in table:
        return table[code_str]
    if message and not re.fullmatch(r"\d+", message):
        return message
    return f"发帖失败（error_code={code_str}）"


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

    def _forum_text(self, chapter: Chapter) -> str:
        """原始吧名配置（可为单个吧，也可多个，用逗号/分号/换行分隔）。"""
        meta = self._meta(chapter)
        value = meta.get("forum")
        if not value:
            value = chapter.raw.get("forum")
        if not value:
            value = self.cfg.get("forum")
        if isinstance(value, (list, tuple)):
            return ",".join(str(item) for item in value)
        return str(value or "").strip()

    def _forums(self, chapter: Chapter) -> list[str]:
        """解析目标吧列表：逗号/分号/换行分隔，去重、去空，保留配置顺序。"""
        text = self._forum_text(chapter)
        forums: list[str] = []
        for raw in re.split(r"[,，;；\r\n|]+", text):
            name = raw.strip()
            if not name:
                continue
            if name not in forums:
                forums.append(name)
        if not forums:
            raise PublisherError(
                "贴吧发帖需要吧名：在 manga.json 的 platforms.tieba.forum 或 "
                "config.yaml 的 tieba.settings.forum 填写。"
                "想同时发到多个吧时用逗号分隔，例如：东方吧,漫画吧"
            )
        return forums

    @staticmethod
    def _login_label(data: object) -> str:
        """从 /i/sys/user_json 响应里提取“当前登录昵称”文案。"""
        if not isinstance(data, dict):
            return "已登录"
        creator = data.get("creator")
        creator = creator if isinstance(creator, dict) else {}
        # 接口/旧客户端可能把 UTF-8 昵称按 GBK 解成伪中文，这里自愈一次
        raw_name = _repair_mojibake(data.get("raw_name")).strip()
        nick = _repair_mojibake(creator.get("show_nickname")).strip()
        if not nick:
            nick = _repair_mojibake(creator.get("name_show")).strip()
        if not nick:
            nick = _repair_mojibake(creator.get("name")).strip()
        if not nick:
            nick = raw_name
        if raw_name and raw_name != nick:
            return f"{nick}（{raw_name}）"
        return nick or "已登录"

    @staticmethod
    def _json_of(resp) -> object:
        """解析贴吧 JSON 响应。

        贴吧接口的 Content-Type 常声明 charset=GBK，但正文实际是 UTF-8；
        requests 会按 GBK 解码导致中文变乱码（如 紙月9 → 绱欐湀9）。
        这里先按字节强解 UTF-8；只有当正文里混进极个别坏字节（其余仍是
        UTF-8）时，才用“只替换坏字节”的方式兜底——绝不能整段退回 GBK，
        否则一个坏字节会把整篇中文都变成 闈為潪涔 式乱码；确认是真 GBK
        （坏字节很多）才交给 requests。
        """
        body = resp.content
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            pass
        try:
            text = body.decode("utf-8", errors="replace")
            data = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            return resp.json()
        if text.count("\ufffd") <= 2:  # 只有极个别坏字节，整体仍是 UTF-8
            return data
        return resp.json()

    def _json_request(self, url: str, **kwargs) -> object:
        resp = self.http.get(url, **kwargs)
        try:
            return self._json_of(resp)
        except ValueError as exc:
            self.http._dump(resp, tag="tieba-json")
            raise PublisherError(
                f"贴吧接口返回的不是 JSON：{url}\n{resp.text[:200]}"
            ) from exc

    def check(self) -> CheckResult:
        missing = self.missing_cookies()
        if missing:
            return CheckResult(self.key, False, f"缺少 Cookie：{', '.join(missing)}")
        try:
            data = self._json_request(TBS_URL)
        except Exception as exc:
            return CheckResult(self.key, False, f"网络请求失败：{exc}")
        if data.get("is_login") not in (1, "1", True):
            reason = _repair_mojibake(data.get("error")) or "Cookie 无效"
            return CheckResult(self.key, False, f"未登录（{reason}）")
        # tbs 接口不含昵称，另读个人中心接口拿账号昵称
        try:
            info = self._json_request(SYS_USER_URL)
            return CheckResult(self.key, True, f"已登录：{self._login_label(info)}")
        except Exception as exc:  # 昵称读取失败不影响登录检查结果
            return CheckResult(self.key, True, f"已登录（tbs 正常，昵称读取失败：{exc}）")

    def identity(self) -> str:
        """当前 BDUSS 对应的贴吧账号（昵称 + uid），失败返回空串。"""
        if self.missing_cookies():
            return ""
        try:
            tbs = self._json_request(TBS_URL)
        except Exception:
            return ""
        if tbs.get("is_login") not in (1, "1", True):
            return ""
        try:
            info = self._json_request(SYS_USER_URL)
        except Exception:
            return "已登录（昵称未知）"
        label = self._login_label(info)
        creator = info.get("creator") if isinstance(info, dict) else {}
        creator = creator if isinstance(creator, dict) else {}
        uid = creator.get("id") or (info.get("user_id") if isinstance(info, dict) else "")
        return f"{label}（uid {uid}）" if uid else label

    def plan(self, chapter: Chapter) -> list[str]:
        pages = len(chapter.pages)
        posts = 1 + max(0, -(-max(pages - 1, 0) // self.max_pages_per_post))
        forums = self._forums(chapter)
        return [
            f"发帖标题：{composer.platform_title(chapter, self.key)}",
            f"目标贴吧：{'、'.join(forums)}"
            + (f"（共 {len(forums)} 个吧，将依次串行发布）" if len(forums) > 1 else ""),
            f"上传 {pages} 张图片：第 1 楼放封面，其余每楼最多 {self.max_pages_per_post} 张，预计 1 帖 {posts} 楼",
            f"正文：{composer.platform_body(chapter, self.key)[:120]}",
        ]

    def full_preview(self, chapter: Chapter) -> list[str]:
        """贴吧发布前全文预览：展示真实标题、正文与分楼顺序。"""
        from ..util import human_size

        pages = len(chapter.pages)
        posts = 1 + max(0, -(-max(pages - 1, 0) // self.max_pages_per_post))
        forums = self._forums(chapter)
        lines = [
            "发布平台：百度贴吧",
            f"标题：{composer.platform_title(chapter, self.key)}",
            f"目标贴吧：{'、'.join(forums)}"
            + (f"（{len(forums)} 个吧，依次串行发布，每吧独立发 1 帖）" if len(forums) > 1 else ""),
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
        data = self._json_request(TBS_URL)
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
            data = self._json_request(NEW_MOINDEX_URL)
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
        # 贴吧传图偶发被拒（error_code=2230204「上传失败」，多是上传过快触发限流）。
        # 单张图失败不该让整层楼、整个吧白跑，这里自动重试若干轮再报错。
        attempts = max(1, int(self.cfg.get("upload_attempts", 3) or 3))
        retry_wait = float(self.cfg.get("upload_retry_wait", 3.0) or 0)
        last_error = ""
        for attempt in range(1, attempts + 1):
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
                payload = self._json_of(resp)
            except ValueError as exc:
                self.http._dump(resp, tag="tieba-upload")
                raise PublisherError(f"贴吧传图接口未返回 JSON：{resp.text[:200]}") from exc
            error_code = payload.get("error_code")
            if error_code in (None, "", 0, "0"):
                break
            message = str(payload.get("error_msg") or payload.get("error") or payload)[:300]
            last_error = _fmt_error(error_code, message)
            if attempt < attempts:
                self.log.warning(
                    "贴吧传图失败（%s，第 %d/%d 次），稍后重试：%s",
                    page.path.name,
                    attempt,
                    attempts,
                    last_error,
                )
                if retry_wait > 0:
                    time.sleep(retry_wait * attempt)
            else:
                self.http._dump(resp, tag="tieba-upload")
                raise PublisherError(f"贴吧传图失败（已重试 {attempts} 次）：{last_error}")
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
            payload = self._json_of(resp)
        except ValueError as exc:
            self.http._dump(resp, tag=tag)
            raise PublisherError(f"贴吧{kind}失败，响应不是 JSON：{resp.text[:200]}") from exc
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        nested_error = data.get("error_code") if isinstance(data, dict) else None
        # 错误响应经常是 data={} 而 error_code 在顶层，两层都看
        error_code = (
            nested_error
            if nested_error not in (None, "", 0, "0")
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

    def _publish_one_forum(
        self,
        chapter: Chapter,
        pages: list,
        forum: str,
        *,
        base_done: int,
        total_uploads: int,
    ) -> tuple[list[str], int, list[str], bool]:
        """把整本（封面 + 分楼）发布到单个吧。

        返回 (帖子 url 列表, 总楼层数, 错误列表, 是否触发验证码)。
        """
        tbs = self._tbs()
        fid = self._fid(forum, tbs)
        self.log.info("吧名=%s fid=%s tbs=%s", forum, fid, tbs[:6] + "…")

        published: list[str] = []
        errors: list[str] = []
        cover_group, rest_groups = self._floor_plan(pages)
        groups = [cover_group] + rest_groups
        thread_tid: str | None = None
        page_done = base_done
        upload_sleep = float(self.cfg.get("upload_sleep", 1.0) or 0)
        captcha = False
        for index, group in enumerate(groups, 1):
            try:
                images: list[dict] = []
                for page in group:
                    local_done = page_done - base_done
                    self.progress(
                        "upload",
                        page_done,
                        total_uploads,
                        f"{forum} 正在上传图片 {local_done + 1}/{len(pages)}："
                        f"{page.path.name}（{index}/{len(groups)} 楼）",
                        chapter_key=chapter.key,
                    )
                    self.log.info(
                        "上传图片 %s（%s，第 %d/%d 组）", page.path.name, forum, index, len(groups)
                    )
                    images.append(self._upload_image(page, tbs, forum))
                    page_done += 1
                    self.progress(
                        "upload",
                        page_done,
                        total_uploads,
                        f"{forum} 已上传图片 {local_done + 1}/{len(pages)}",
                        chapter_key=chapter.key,
                    )
                    time.sleep(upload_sleep)

                # 正文只放主题帖一楼，后续楼层只放图片，避免每楼重复
                description = composer.platform_body(chapter, self.key) if thread_tid is None else ""
                content = self._build_text(description, images)

                title = composer.platform_title(chapter, self.key)
                if thread_tid is None:
                    thread_tid = self._post_thread(forum, fid, tbs, title[:80], content)
                    url = f"https://tieba.baidu.com/p/{thread_tid}"
                    published.append(url)
                    self.log.info("主题帖发布成功（%s）：%s", forum, url)
                else:
                    self._reply_post(forum, fid, tbs, thread_tid, content)
                    self.log.info("已追加楼层 %d 到 %s（%s）", index, thread_tid, forum)
            except CaptchaRequiredError as exc:
                errors.append(f"第 {index} 楼：{exc}")
                self.log.error("%s 第 %d 组需要验证码：%s", forum, index, exc)
                captcha = True
                break  # 验证码需人工处理，停止该吧后续组避免反复触发
            except PublisherError as exc:
                errors.append(f"第 {index} 楼：{exc}")
                self.log.error("%s 第 %d 组发帖失败：%s", forum, index, exc)
                if thread_tid is None:
                    # 主题帖本身没发出去时，后续组没有可回复的帖子，直接放弃该吧
                    break
        return published, len(groups), errors, captcha

    def publish(self, chapter: Chapter) -> PublishResult:
        self.require_cookies()
        if not chapter.pages:
            return PublishResult.skipped(self.key, chapter, "没有图片")
        allowed = {".jpg", ".jpeg", ".png", ".gif"}
        pages = self.prepare_pages(chapter, allowed_exts=allowed)
        try:
            forums = self._forums(chapter)
            published: list[str] = []
            errors: list[str] = []
            total_floors = 0
            total_uploads = len(pages) * len(forums)
            for forum_index, forum in enumerate(forums, 1):
                if forum_index > 1:
                    wait = float(self.cfg.get("forum_interval", 3.0) or 0)
                    if wait > 0:
                        self.log.info(
                            "等待 %.1f 秒后再发到下一个吧 %s（防限流）…", wait, forum
                        )
                        time.sleep(wait)
                try:
                    urls, group_count, inner_errors, captcha = self._publish_one_forum(
                        chapter,
                        pages,
                        forum,
                        base_done=(forum_index - 1) * len(pages),
                        total_uploads=total_uploads,
                    )
                    published.extend(urls)
                    total_floors += group_count
                    if inner_errors:
                        errors.append(f"{forum}：{'；'.join(inner_errors[:2])}")
                    if captcha:
                        break  # 验证码需人工处理，不再尝试后续吧
                except CaptchaRequiredError as exc:
                    errors.append(f"{forum}：{exc}")
                    break  # 验证码需人工处理，不再尝试后续吧
                except PublisherError as exc:
                    errors.append(f"{forum}：{exc}")
                    self.log.error("发布到 %s 失败：%s", forum, exc)
                except Exception as exc:  # 网络等意外错误按单吧失败处理，不阻断后续吧
                    errors.append(f"{forum}：{exc}")
                    self.log.exception("发布到 %s 时出现未预期错误", forum)

            if not published:
                return PublishResult.failed(
                    self.key,
                    chapter,
                    "；".join(errors[:3]),
                    details={"count": len(errors), "forums": forums},
                )
            if errors:
                return PublishResult.partial(
                    self.key,
                    chapter,
                    url=published[0],
                    message=f"部分吧发布失败：{errors[0]}",
                    urls=published,
                    failed=len(errors),
                    pages=len(pages),
                    forums=forums,
                )
            message = (
                f"已发 1 帖共 {total_floors} 楼"
                if len(forums) == 1
                else f"已依次发布到 {len(forums)} 个吧（共 {total_floors} 楼）"
            )
            return PublishResult.ok(
                self.key,
                chapter,
                url=published[0],
                message=message,
                urls=published,
                pages=len(pages),
                forums=forums,
            )
        finally:
            self.cleanup_prepared(chapter)
