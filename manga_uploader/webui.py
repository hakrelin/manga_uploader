"""浏览器前端的纯逻辑(不依赖 tkinter)。

从旧 tkinter gui.py 移植的可复用部分,供 web.py 服务与测试使用。
gui.py 保持完整不动(保留作 tkinter 备用界面)。
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

from .config import CommonConfig, PlatformConfig, AppConfig, DEFAULT_SETTINGS
from .comic import META_FILES
from .publishers.ehentai import DEFAULT_FIELD_ROWS
from .publishers.zaimanhua import CATE_LABELS
from .util import is_image, natural_sort_key

# ---------------------------------------------------------------- Cookie 解析

def parse_cookie_text(text: str) -> dict[str, str]:
    """把 'k1=v1; k2=v2' 或 JSON/引号变体解析为字典。"""
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except ValueError:
            pass
    result: dict[str, str] = {}
    for chunk in re.split(r"[;,\n]", text):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def join_cookie_text(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# ------------------------------------------------------------ 平台卡片元数据

PLATFORM_CARDS: list[dict[str, Any]] = [
    {
        "key": "bilibili",
        "label": "B站（专栏文章，可切图文动态）",
        "login_url": "https://passport.bilibili.com/login",
        "cookie_fields": [
            {"name": "SESSDATA", "required": True},
            {"name": "bili_jct", "required": True, "hint": "CSRF 令牌"},
            {"name": "buvid3", "required": False, "hint": "建议填写"},
        ],
        "hint": "默认把每话发成一篇专栏（正文带图）；要发图文动态可在 config 里把 publish_mode 改成 dynamic。",
        "qr": True,
    },
    {
        "key": "tieba",
        "label": "百度贴吧（图帖）",
        "login_url": "https://tieba.baidu.com",
        "cookie_fields": [{"name": "BDUSS", "required": True}],
        "hint": "登录百度后复制 Cookie 里的 BDUSS。发帖权限受账号与吧等级限制。",
        "extras": [("forum", "目标吧名（可留空，在 manga.json 里配置）")],
    },
    {
        "key": "ehentai",
        "label": "e-hentai（图库）",
        "login_url": "https://forums.e-hentai.org/index.php?act=Login&CODE=00",
        "cookie_fields": [
            {"name": "ipb_member_id", "required": True},
            {"name": "ipb_pass_hash", "required": True},
        ],
        "hint": "登录 e-hentai 后复制 Cookie 里的 ipb_member_id 与 ipb_pass_hash。"
        "上传入口：upload.e-hentai.org/managegallery?act=new；通常需要代理直连。",
        "extras": [
            ("category_label", "默认分类（如 Manga / Doujinshi）"),
            ("language_label", "语言（留空用页面默认 Japanese/No Text，如 Chinese）"),
            ("langtype", "0=官方/无字 1=汉化 2=改写（汉化上传默认 1）"),
            ("title_jpn", "默认日文原标题（可被每话 manga.json 覆盖）"),
        ],
        "field_map": True,
    },
    {
        "key": "zaimanhua",
        "label": "再漫画（投稿）",
        "login_url": "https://www.zaimanhua.com/",
        "cookie_fields": [
            {"name": "token", "required": True},
            {"name": "clientId", "required": False, "hint": "可选"},
        ],
        "hint": "登录再漫画后复制 Cookie 里的 token（JWT），可选 clientId。投稿页：manhua.zaimanhua.com/uploadShows",
        "extras": [("cate", "作品类型")],
    },
    {
        "key": "xiaoheihe",
        "label": "小黑盒（图文发布）",
        "login_url": "https://www.xiaoheihe.cn/creator/editor/draft/image_text",
        "cookie_fields": [
            {
                "name": "cookie",
                "required": True,
                "hint": "整段 Cookie（含 pkey/user_pkey/heybox_id 等）",
            }
        ],
        "hint": "打开 xiaoheihe.cn 并登录后，按 F12 → Network → 复制任意请求的 Cookie 头整段粘贴。"
        "每帖最多 30 张图，超出自动拆帖；发布到 PC游戏 社区。",
        "extras": [
            ("max_pages_per_post", "单帖图片上限（默认 30）"),
            ("publish_draft", "true=只存草稿（不公开）"),
            ("topic_id", "发布社区 id（默认 1=PC游戏）"),
        ],
    },
]

EXTRA_OPTIONS: dict[str, Any] = {
    "cate": ("1", "2", "3", "4"),
    "cate_label": None,
    "category_label": None,
    "language_label": None,
    "langtype": None,
    "title_jpn": None,
    "forum": None,
    "max_pages_per_post": None,
    "publish_draft": None,
    "topic_id": None,
}


def cate_label(value: object) -> str:
    return CATE_LABELS.get(str(value), str(value))


# ------------------------------------------------------------ 全文预览排版

def format_full_preview(
    preview: list[tuple[Any, list[tuple[str, list[str]]]]],
) -> str:
    """把全文预览结果排版成文本。"""
    lines = [
        "=" * 72,
        "【发布前全文预览】（只做本地处理与展示，不会上传/发布）",
        "=" * 72,
    ]
    for chapter, rows in preview:
        lines.append("")
        lines.append(f"■ 章节：{chapter.title}（{chapter.key}，{len(chapter.pages)} 张源图）")
        if not rows:
            lines.append("  无可预览平台（未启用或配置缺失）")
        for name, content in rows:
            lines.append("")
            lines.append(f"  ● {name}")
            for line in content:
                lines.append("      " + line)
    return "\n".join(lines)


# ------------------------------------------------------------ 漫画导入辅助

def import_staging_base() -> Path:
    base = Path(tempfile.gettempdir()) / "mangaupload_imports"
    base.mkdir(parents=True, exist_ok=True)
    return base


def looks_like_full_comic(folder: Path) -> bool:
    """目录是否自带元数据/多话结构（可原样直接使用）。"""
    if any((folder / name).is_file() for name in META_FILES):
        return True
    if any(p.is_file() and is_image(p) for p in folder.iterdir()):
        return False  # 根目录直接放图 = 单本
    for child in folder.iterdir():
        if child.is_dir() and any(
            p.is_file() and is_image(p) for p in child.iterdir()
        ):
            return True
    return False


def unwrap_single_dir(folder: Path) -> Path:
    """压缩包里常见的“外层套一个文件夹”：只套一层且无直接图片时往里走。"""
    current = folder
    while True:
        entries = list(current.iterdir())
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file()]
        if len(dirs) == 1 and not files:
            current = dirs[0]
        else:
            return current


def extract_zip(archive: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name:
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"压缩包内含不安全路径：{info.filename}")
            target = (dest / name).resolve()
            if dest not in target.parents:
                raise ValueError(f"压缩包路径越界：{info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return dest


def stage_images(images: list[Path], title_hint: str = "") -> Path:
    """把图片复制到导入缓存目录并按文件名重排号。"""
    base = import_staging_base()
    safe = re.sub(r"[^\w一-鿿 .()-]+", "_", str(title_hint or "comic"))
    safe = safe.strip(" .") or "comic"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    staged = base / f"{safe}_{stamp}_{int(time.time_ns() % 100000)}"
    staged.mkdir(parents=True, exist_ok=False)
    images = sorted(images, key=natural_sort_key)
    width = max(3, len(str(len(images))))
    for index, src in enumerate(images, 1):
        target = staged / f"{index:0{width}d}{src.suffix.lower()}"
        shutil.copy2(src, target)
    return staged


def write_quick_meta(folder: Path, meta: dict[str, str]) -> None:
    """给单本导入生成 manga.json（标题/作者/简介，root 章节）。"""
    title = (meta.get("title") or "").strip() or folder.name
    payload = {
        "title": title,
        "author": (meta.get("author") or "").strip(),
        "description": (meta.get("description") or "").strip(),
        "chapters": [
            {
                "folder": "root",
                "title": title,
                "description": (meta.get("description") or "").strip(),
            }
        ],
    }
    (folder / "manga.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ------------------------------------------------------------ 配置组装/保存

def build_app(payload: dict[str, Any]) -> AppConfig:
    """从前端提交的配置 payload 组装 AppConfig。

    payload 形态：
        {"common": {...}, "platforms": {"bilibili": {"enabled": bool,
         "cookies": {...}, "settings": {...}}, ...}}
    """
    common_raw = payload.get("common") or {}

    def _f(key: str, default: float) -> float:
        try:
            return float(common_raw.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _i(key: str, default: int) -> int:
        try:
            return int(float(common_raw.get(key, default)))
        except (TypeError, ValueError):
            return int(default)

    timeout = _f("timeout", 30.0)
    retries = max(0, _i("retries", 3))
    interval = max(0.0, _f("interval_seconds", 0.0))
    max_width = max(0, _i("max_width", 2400))
    max_height = max(0, _i("max_height", 0))
    quality = min(95, max(10, _i("quality", 88)))
    max_mb = max(0.0, _f("max_bytes_mb", 10.0))
    output_dir = str(common_raw.get("output_dir") or "output").strip() or "output"

    common = CommonConfig(
        timeout=timeout,
        retries=retries,
        interval_seconds=interval,
        max_width=max_width,
        max_height=max_height,
        quality=quality,
        max_bytes_mb=max_mb,
        output_dir=output_dir,
        confirm=False,
        parallel=bool(common_raw.get("parallel", False)),
        verbose=bool(common_raw.get("verbose", False)),
        proxy_url=str(common_raw.get("proxy_url") or "").strip(),
        use_system_proxy=bool(common_raw.get("use_system_proxy", False)),
    )

    platforms: dict[str, PlatformConfig] = {}
    platforms_raw = payload.get("platforms") or {}
    for key in DEFAULT_SETTINGS:
        item = platforms_raw.get(key)
        if not isinstance(item, dict):
            item = {}
        settings = dict(DEFAULT_SETTINGS.get(key, {}))
        raw_settings = item.get("settings")
        if isinstance(raw_settings, dict):
            settings.update(raw_settings)
        platforms[key] = PlatformConfig(
            name=key,
            enabled=bool(item.get("enabled", True)),
            cookies={str(k): str(v) for k, v in (item.get("cookies") or {}).items()},
            settings=settings,
        )
    return AppConfig(common=common, platforms=platforms, path=None)


def save_config(config_path: str | Path, payload: dict[str, Any]) -> Path:
    """把前端提交的配置写入 config.yaml，保留文件里未编辑的内容。

    仅覆盖 common 与各平台 enabled/cookies/settings；文件里其它内容原样保留。
    """
    import yaml

    path = Path(config_path)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("common", {})
    raw.setdefault("platforms", {})

    common = payload.get("common") or {}
    if isinstance(common, dict):
        allowed = set(CommonConfig().__dataclass_fields__)
        raw["common"] = {k: v for k, v in common.items() if k in allowed}

    platforms = payload.get("platforms") or {}
    if isinstance(platforms, dict):
        for key, item in platforms.items():
            if not isinstance(item, dict):
                continue
            raw["platforms"][key] = {
                "enabled": bool(item.get("enabled", True)),
                "cookies": {
                    str(k): str(v) for k, v in (item.get("cookies") or {}).items()
                },
                "settings": item.get("settings") if isinstance(item.get("settings"), dict) else {},
            }

    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


# ------------------------------------------------------------ B站扫码（分步）

_BILI_QR_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "Chrome/126.0 Safari/537.36"
)
_BILI_QR_WANTED = ["SESSDATA", "bili_jct", "buvid3", "DedeUserID"]


def bilibili_qr_new() -> tuple[Any, str, str]:
    """创建扫码会话并请求二维码，返回 (session, qr_url, qrcode_key)。"""
    import requests

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": _BILI_QR_UA,
            "Referer": "https://passport.bilibili.com/login",
        }
    )
    data = session.get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        timeout=15,
    ).json()
    return session, str(data["data"]["url"]), str(data["data"]["qrcode_key"])


def bilibili_qr_poll(session: Any, qrcode_key: str) -> dict[str, Any]:
    """轮询扫码状态一次。返回 {code, status, cookies?}。

    status 语义：pending / confirmed / expired / ok；cookies 仅 ok 时给出。
    """
    try:
        poll = session.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key},
            timeout=15,
        ).json()
    except Exception as exc:
        return {"code": -1, "status": "pending", "error": str(exc)}
    code = poll.get("code")
    if code == 0:
        cookies = {name: c.value for name, c in session.cookies.items()}
        picked = {k: v for k, v in cookies.items() if k in _BILI_QR_WANTED}
        return {"code": code, "status": "ok", "cookies": picked or cookies}
    if code == 86038:
        return {"code": code, "status": "expired"}
    if code == 86090:
        return {"code": code, "status": "confirmed"}
    return {"code": code, "status": "pending"}
