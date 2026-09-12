"""浏览器前端的纯逻辑(不依赖 tkinter)。

从旧 tkinter gui.py 移植的可复用部分,供 web.py 服务与测试使用。
gui.py 保持完整不动(保留作 tkinter 备用界面)。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import (
    AppConfig,
    CommonConfig,
    ConfigError,
    DEFAULT_SETTINGS,
    PlatformConfig,
)
from .comic import META_FILES, find_meta_file, load_chapters, read_meta
from .publishers.ehentai import DEFAULT_FIELD_ROWS
from .publishers.zaimanhua import CATE_LABELS
from .util import IMAGE_EXTS, is_image, natural_sort_key, sort_images

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
        # 必需项之外，下面几个是 B站风控（-352）依赖的会话 Cookie：
        # 建议点“粘贴整段 Cookie”一次填全；留空也能发（发布前自动补全），
        # 但填上更稳，尤其是账号已经被 -352 拦过的时候。
        "cookie_fields": [
            {"name": "SESSDATA", "required": True},
            {"name": "bili_jct", "required": True, "hint": "CSRF 令牌"},
            {"name": "buvid3", "required": False, "hint": "设备指纹，建议填写"},
            {"name": "buvid4", "required": False, "hint": "设备指纹，建议填写（-352 风控看它）"},
            {"name": "b_nut", "required": False, "hint": "首次访问时间戳（10 位秒）"},
            {"name": "DedeUserID", "required": False, "hint": "登录 UID，建议填写"},
        ],
        "hint": "默认把每话发成一篇专栏（正文带图）；要发图文动态可在 config 里把 publish_mode 改成 dynamic。"
        "建议点“粘贴整段 Cookie”把 buvid3/buvid4/b_nut/DedeUserID 一起填全（整段粘贴会自动识别）；"
        "留空也能发布——程序会在发布前自动补全，但填上能明显减少 -352 风控。",
        "qr": True,
        "controls": {
            "publish_mode": {
                "kind": "select",
                "label": "发布方式",
                "options": [
                    ("article", "专栏文章（推荐）"),
                    ("dynamic", "图文动态（旧）"),
                ],
            },
            "max_article_pages": {"kind": "number", "label": "单篇专栏最多图片数"},
            "submit_attempts": {"kind": "number", "label": "风控(-352)自动重试次数"},
            "submit_retry_wait": {"kind": "number", "label": "风控重试间隔基准(秒)"},
            "image_delay": {"kind": "number", "label": "每张图随机延时上限(秒)"},
            "original": {"kind": "select", "label": "原创声明", "options": [("1", "原创"), ("0", "非原创")]},
            "reprint": {"kind": "select", "label": "转载属性", "options": [("0", "原创/未标转载"), ("1", "转载")]},
            "topics": {"kind": "text", "label": "图文动态话题（逗号分隔）"},
            "image_category": {"kind": "text", "label": "动态图片分类 daily/draw/cos"},
        },
    },
    {
        "key": "tieba",
        "label": "百度贴吧（图帖，可多吧）",
        "login_url": "https://tieba.baidu.com",
        "cookie_fields": [{"name": "BDUSS", "required": True}],
        "hint": "登录百度后复制 Cookie 里的 BDUSS。发帖权限受账号与吧等级限制。"
        "吧名可填多个（逗号分隔），发布时会依次串行发到每个吧，避免同时多发被限流。",
        "extras": [
            (
                "forum",
                "如 东方吧,漫画吧（多个吧用逗号分隔，依次串行发布；可留空在 manga.json 里配置）",
            )
        ],
        "controls": {
            "max_pages_per_post": {"kind": "number", "label": "每楼最多图片数（默认 9）"},
            "upload_sleep": {"kind": "number", "label": "每张图上传间隔（秒）"},
            "forum_interval": {"kind": "number", "label": "每吧间隔（秒，防限流）"},
        },
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
        "controls": {
            "upload_mode": {
                "kind": "select",
                "label": "上传方式",
                "options": [("zip", "整包 zip（推荐）"), ("files", "逐张多文件")],
            },
            "publish_after_upload": {"kind": "switch", "label": "上传后自动发布"},
            "extra_tags": {"kind": "text", "label": "附加标签（逗号分隔）"},
        },
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
        "controls": {
            "max_pages_per_upload": {"kind": "number", "label": "单章最多图片数"},
            "upload_attempts": {"kind": "number", "label": "传图失败重试次数"},
        },
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
        "≤30 页发图文，＞30 页发文章（文章单帖上限 100，超出继续拆帖）；"
        "默认关联 东方夜雀食堂 + 东方冰之勇者记，内容声明为转载/已授权/站外 bilibili。",
        "extras": [
            ("topic_ids", "关联社区（默认 431327,477625）"),
            ("hashtags", "关联话题（默认 东方project,东方同人）"),
            ("source", "站外转载来源（默认 bilibili）"),
        ],
        # 发布形式/草稿模式等常用项：前端渲染成开关/下拉等控件，
        # 其余 extras 仍以文本输入展示（无需再手动编辑 config.yaml）。
        "controls": {
            "publish_mode": {
                "kind": "select",
                "label": "发布形式",
                "options": [
                    ("auto", "自动（≤30 页图文 / >30 页文章）"),
                    ("image_text", "图文"),
                    ("article", "文章"),
                ],
            },
            "publish_draft": {
                "kind": "switch",
                "label": "先存草稿（不公开）",
            },
            "image_text_max_pages": {
                "kind": "number",
                "label": "图文/文章分界页数（auto）",
            },
            "article_max_pages": {
                "kind": "number",
                "label": "文章单帖最大页数",
            },
        },
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
    "image_text_max_pages": None,
    "article_max_pages": None,
    "publish_draft": None,
    "topic_ids": None,
    "hashtags": None,
    "source": None,
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


ARCHIVE_EXTS = {".zip", ".cbz", ".7z", ".rar"}


def _archive_tool(suffix: str) -> str | None:
    """找能解 7z/rar 的命令行工具：优先用项目内置 7za.exe，其次系统 7-Zip/unrar。"""
    import shutil

    # 完整版 7z.exe + 7z.dll 支持 RAR；7za.exe 是精简版，不支持 RAR
    for name in ("7z.exe", "7za.exe"):
        bundled = Path(__file__).resolve().parent / "data" / "7z" / name
        if bundled.is_file():
            return str(bundled)
    names = ["7z", "7za", "7zr"]
    if suffix == ".rar":
        names += ["unrar", "rar"]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        candidates = [
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        ]
        if suffix == ".rar":
            candidates += [
                Path(r"C:\Program Files\WinRAR\UnRAR.exe"),
                Path(r"C:\Program Files\WinRAR\Rar.exe"),
                Path(r"C:\Program Files (x86)\WinRAR\UnRAR.exe"),
                Path(r"C:\Program Files (x86)\WinRAR\Rar.exe"),
            ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


def extract_archive(archive: Path, dest: Path) -> Path:
    """按扩展名解压 ZIP/CBZ（内置）或 7z/RAR（需要 7-Zip/unrar）。"""
    suffix = archive.suffix.lower()
    if suffix in (".zip", ".cbz"):
        return extract_zip(archive, dest)
    if suffix not in ARCHIVE_EXTS:
        raise ValueError(f"不支持的压缩包格式：{suffix}")
    tool = _archive_tool(suffix)
    if not tool:
        raise ValueError(
            f"解压 {suffix} 失败：未找到可用的解压工具（项目内置 7za.exe 缺失，"
            "且系统未安装 7-Zip/unrar）"
        )
    import subprocess

    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [tool, "x", "-y", f"-o{dest}", str(archive)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise ValueError(f"{tool} 解压失败：{tail or '未知错误'}")
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


# ------------------------------------------------------------ 页序落盘
def write_page_order(
    comic_dir: str | Path,
    chapter_key: str,
    pages: list[str],
) -> None:
    """把章节的显式页序写进 manga.json 的 chapters 条目。

    - 页序与文件名自然排序一致（或为空）时省略 pages 字段，保持文件干净；
    - 单本 root 条目用 folder:"root"，多话用目录名，与 load_chapters 约定一致；
    - 只改 pages 字段，manga.json 其余字段（元数据/平台内容）原样保留。
    """
    root = Path(comic_dir)
    chapters = load_chapters(comic_dir, strict=False)
    chapter = next((c for c in chapters if c.key == chapter_key), None)
    if chapter is None:
        raise ValueError(f"找不到章节：{chapter_key}")

    meta_file = find_meta_file(root)
    data = read_meta(meta_file) if meta_file else {}
    if not isinstance(data, dict):
        data = {}

    entries = data.get("chapters")
    if not isinstance(entries, list):
        entries = []
        data["chapters"] = entries
    entry = next(
        (
            e
            for e in entries
            if isinstance(e, dict)
            and str(e.get("folder") or e.get("key") or e.get("name")) == chapter_key
        ),
        None,
    )
    if entry is None:
        entry = {"folder": chapter_key}
        entries.append(entry)

    names = [str(n) for n in (pages or [])]
    natural = [p.name for p in sort_images(chapter.source_dir.iterdir())]
    if names and names != natural:
        entry["pages"] = names
    else:
        entry.pop("pages", None)

    meta_file = meta_file or (root / "manga.json")
    if meta_file.suffix.lower() in (".yaml", ".yml"):
        import yaml

        meta_file.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    else:
        meta_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------- Staff 页

def staff_page_name(cover_name: str) -> str:
    """staff 页文件名：封面页名 + staff（如封面 001.jpg → 001staff.png）。

    部分平台按文件名排序：001staff.png 会自然排在封面（001）与下一页
    （002）之间，staff 页的位置由文件名即可保证，不依赖 manga.json
    的 pages 字段覆盖。
    """
    stem = Path(str(cover_name or "")).stem.strip()
    return f"{stem or '001'}staff.png"


def is_staff_page_name(name: str) -> bool:
    """判断是否为本工具生成的 staff 页（旧固定名 staff.* 或 封面名+staff）。"""
    stem = Path(str(name)).stem.lower()
    return stem == "staff" or stem.endswith("staff")


def _dump_meta(meta_file: Path, data: dict) -> None:
    """按扩展名把 meta 写回磁盘（JSON/YAML，UTF-8，中文原样）。"""
    if meta_file.suffix.lower() in (".yaml", ".yml"):
        import yaml

        meta_file.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    else:
        meta_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _chapter_entry(data: dict, chapter_key: str) -> dict:
    """在 manga.json 的 chapters 里定位章节条目，没有则创建（与 write_page_order 一致）。"""
    entries = data.get("chapters")
    if not isinstance(entries, list):
        entries = []
        data["chapters"] = entries
    entry = next(
        (
            e
            for e in entries
            if isinstance(e, dict)
            and str(e.get("folder") or e.get("key") or e.get("name")) == chapter_key
        ),
        None,
    )
    if entry is None:
        entry = {"folder": chapter_key}
        entries.append(entry)
    return entry


def read_staff_rows(comic_dir: str | Path, chapter_key: str) -> Optional[dict]:
    """读章节的 staff 数据：{"rows": [[职位, 名字], …], "bg": 背景页 0-based 序号}。

    无保存记录返回 None；有记录但缺 bg 时 bg 为 None（前端用布局默认）。
    """
    root = Path(comic_dir)
    meta_file = find_meta_file(root)
    if meta_file is None:
        return None
    try:
        data = read_meta(meta_file)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    key = str(chapter_key or "root")
    for entry in data.get("chapters") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("folder") or entry.get("key") or entry.get("name")) != key:
            continue
        staff = entry.get("staff")
        if not isinstance(staff, dict):
            return None
        out: dict = {"rows": None, "bg": None}
        if isinstance(staff.get("rows"), list):
            rows: list[list[str]] = []
            for row in staff["rows"]:
                if isinstance(row, (list, tuple)) and len(row) == 2:
                    rows.append([str(row[0]), str(row[1])])
            out["rows"] = rows
        bg = staff.get("bg")
        if isinstance(bg, int) and bg >= 0:
            out["bg"] = bg
        return out
    return None


def write_staff_rows(
    comic_dir: str | Path,
    chapter_key: str,
    rows: list,
    bg: Optional[int] = None,
) -> int:
    """把 staff 名单（和背景页选择）写进 manga.json 章节条目的 staff 字段。

    空名单且无 bg 时删字段。返回行数。
    """
    clean: list[list[str]] = []
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) == 2:
            role = str(row[0]).strip()
            name = str(row[1]).strip()
            if role or name:
                clean.append([role, name])

    root = Path(comic_dir)
    meta_file = find_meta_file(root)
    data = read_meta(meta_file) if meta_file else {}
    if not isinstance(data, dict):
        data = {}
    entry = _chapter_entry(data, str(chapter_key or "root"))
    have_bg = isinstance(bg, int) and not isinstance(bg, bool)
    if isinstance(rows, list) or have_bg:
        staff = entry.get("staff") if isinstance(entry.get("staff"), dict) else {}
        if isinstance(rows, list):
            # 空名单也原样保存：用户清空模板后重开面板不应又被默认职位顶回来
            staff["rows"] = clean
        if have_bg:
            staff["bg"] = max(0, bg)
        if clean or have_bg:
            entry["staff"] = staff
        else:
            entry.pop("staff", None)
    else:
        entry.pop("staff", None)

    _dump_meta(meta_file or (root / "manga.json"), data)
    return len(clean)


def upsert_staff_page(comic_dir: str | Path, chapter_key: str, data: bytes) -> int:
    """把前端渲染好的 staff 页 PNG 落成章节第 2 页（封面后），重复生成覆盖不堆叠。

    文件名取封面页名 + staff（如 001staff.png），便于按文件名排序的平台
    把 staff 页固定在封面之后；只清理“本工具生成的旧 staff 页”
    （历史固定名 staff.* + manga.json 记录的上一版文件名），不碰用户
    自己命名/加入的 xxxstaff 图片。后端零渲染，只存文件 + 管页序。
    返回章节页数。
    """
    from io import BytesIO

    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as img:
            img.load()
    except Exception as exc:
        raise ValueError(f"无法识别图片内容：{exc}") from exc

    chapters = load_chapters(comic_dir, strict=False)
    chapter = next((c for c in chapters if c.key == chapter_key), None)
    if chapter is None:
        raise ValueError(f"找不到章节：{chapter_key}")
    folder = chapter.source_dir

    current = [p.name for p in chapter.pages]
    # 读取本工具上次生成的 staff 文件名（manga.json 章节条目 staff.file）
    prev_tool_file = None
    meta_file = find_meta_file(Path(comic_dir))
    meta_data = read_meta(meta_file) if meta_file else {}
    for entry in meta_data.get("chapters") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("folder") or entry.get("key") or entry.get("name")) != str(chapter_key or "root"):
            continue
        staff_rec = entry.get("staff")
        if isinstance(staff_rec, dict):
            prev_tool_file = str(staff_rec.get("file") or "").strip() or None

    stale: set[str] = set()
    for name in current:
        # 历史固定名 staff.*（任何图片扩展名）
        if Path(name).stem.lower() == "staff":
            stale.add(name)
        # 上一版工具生成的动态名（封面改名后也能找到旧文件）
        if prev_tool_file and name == prev_tool_file:
            stale.add(name)
    for name in stale:
        current.remove(name)
        try:
            os.remove(folder / name)
        except OSError:
            pass  # 物理文件删不掉也不阻塞生成（不在页序里就不会发布）

    tmp = folder / f".mu_tmp_{time.time_ns()}.png"
    tmp.write_bytes(data)
    cover = current[0] if current else None
    name = staff_page_name(cover) if cover else "staff.png"
    if name in current:  # 同名重复生成：先移出页序再原子覆盖
        current.remove(name)
    os.replace(tmp, folder / name)
    pos = 1 if current else 0  # 封面（第 1 页）之后
    current.insert(pos, name)

    # 记录本工具生成的 staff 文件名，下次生成只清理它，避免误删用户自建页
    try:
        meta_file = find_meta_file(Path(comic_dir))
        meta_data = read_meta(meta_file) if meta_file else {}
        entry = _chapter_entry(meta_data, str(chapter_key or "root"))
        staff_rec = entry.get("staff") if isinstance(entry.get("staff"), dict) else {}
        staff_rec["file"] = name
        entry["staff"] = staff_rec
        _dump_meta(meta_file or (Path(comic_dir) / "manga.json"), meta_data)
    except Exception:  # pragma: no cover - 标记写失败不阻塞落页
        pass

    write_page_order(comic_dir, chapter_key, current)
    return len(current)


# 内置尾页素材（汉化组尾页/梅子版尾页）
TAIL_ASSET = Path(__file__).resolve().parent / "data" / "tail_page.jpg"


def add_tail_page(comic_dir: str | Path, chapter_key: str) -> tuple[int, str, bool]:
    """把内置尾页追加到章节末尾；章节已有尾页时跳过（不重复叠加）。

    返回 (页数, 尾页文件名, 是否新加)。识别规则：文件名主体以“尾页”结尾。
    """
    if not TAIL_ASSET.is_file():
        raise ValueError("缺少内置尾页素材（manga_uploader/data/tail_page.jpg）")
    chapters = load_chapters(comic_dir, strict=False)
    chapter = next((c for c in chapters if c.key == str(chapter_key or "root")), None)
    if chapter is None:
        raise ValueError(f"找不到章节：{chapter_key}")
    folder = chapter.source_dir
    current = [p.name for p in chapter.pages]
    target = "尾页" + TAIL_ASSET.suffix.lower()
    existing = next(
        (n for n in current if n == target or Path(n).stem.endswith("尾页")),
        None,
    )
    if existing:
        return len(current), existing, False
    (folder / target).write_bytes(TAIL_ASSET.read_bytes())
    current.append(target)
    write_page_order(comic_dir, str(chapter_key or "root"), current)
    return len(current), target, True


# B站封面设置（manga.json platforms.bilibili.cover）与自定义封面文件
BCOVER_FILE = "_cover_custom.bin"


def read_bcover(comic_dir: str | Path, chapter_key: str) -> dict:
    """读 B站封面设置：默认第一页整图，无自定义文件。"""
    root = Path(comic_dir)
    meta_file = find_meta_file(root)
    data = read_meta(meta_file) if meta_file else {}
    platform = {}
    if isinstance(data, dict):
        entry = next(
            (
                e
                for e in (data.get("chapters") or [])
                if isinstance(e, dict)
                and str(e.get("folder") or e.get("key") or e.get("name"))
                == str(chapter_key or "root")
            ),
            None,
        )
        raw = entry if entry is not None else data
        platforms = raw.get("platforms") if isinstance(raw, dict) else {}
        if isinstance(platforms, dict) and isinstance(platforms.get("bilibili"), dict):
            platform = platforms["bilibili"]
    cover = platform.get("cover") if isinstance(platform, dict) else {}
    if not isinstance(cover, dict):
        cover = {}
    mode = str(cover.get("mode") or "first")
    crop = cover.get("crop")
    if not (
        isinstance(crop, (list, tuple))
        and len(crop) == 4
        and all(isinstance(v, (int, float)) for v in crop)
    ):
        crop = [0.0, 0.0, 1.0, 1.0]
    folder = next(
        (c.source_dir for c in load_chapters(root, strict=False) if c.key == str(chapter_key or "root")),
        root,
    )
    return {
        "mode": mode if mode in ("first", "custom") else "first",
        "crop": [float(v) for v in crop],
        "custom": (folder / BCOVER_FILE).is_file(),
    }


def write_bcover(
    comic_dir: str | Path,
    chapter_key: str,
    payload: dict,
    cover_data: bytes | None = None,
) -> None:
    """保存 B站封面设置；有 cover_data 时一并写入章节目录的自定义封面文件。"""
    root = Path(comic_dir)
    meta_file = find_meta_file(root)
    data = read_meta(meta_file) if meta_file else {}
    if not isinstance(data, dict):
        data = {}
    entry = _chapter_entry(data, str(chapter_key or "root"))
    platforms = entry.get("platforms")
    if not isinstance(platforms, dict):
        platforms = {}
        entry["platforms"] = platforms
    bili = platforms.get("bilibili")
    if not isinstance(bili, dict):
        bili = {}
        platforms["bilibili"] = bili
    cover = bili.get("cover")
    if not isinstance(cover, dict):
        cover = {}
    mode = str(payload.get("mode") or "first")
    cover["mode"] = mode if mode in ("first", "custom") else "first"
    crop = payload.get("crop")
    if (
        isinstance(crop, (list, tuple))
        and len(crop) == 4
        and all(isinstance(v, (int, float)) for v in crop)
    ):
        cover["crop"] = [min(1.0, max(0.0, float(v))) for v in crop]
    elif mode == "custom":
        cover.setdefault("crop", [0.0, 0.0, 1.0, 1.0])
    bili["cover"] = cover
    _dump_meta(meta_file or (root / "manga.json"), data)
    if cover_data is not None:
        chapters = load_chapters(root, strict=False)
        chapter = next(
            (c for c in chapters if c.key == str(chapter_key or "root")),
            None,
        )
        folder = chapter.source_dir if chapter else root
        (folder / BCOVER_FILE).write_bytes(cover_data)


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
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError(
            "缺少 PyYAML 依赖：请重新运行 start-web.ps1（或 pip install pyyaml）"
        ) from exc

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

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写同目录临时文件再原子替换：避免写入中断把 config 写坏，
        # 同时能拿到更明确的权限/占用错误
        tmp = path.with_name(
            f".{path.name}.mu-tmp-{os.getpid()}-{time.time_ns()}"
        )
        tmp.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        raise ConfigError(
            f"无法写入配置文件 {path}：{exc}。"
            "请把程序放到可写目录（不要放在 Program Files 等只读位置），"
            "或检查该文件是否被其他程序占用"
        ) from exc
    return path


# ------------------------------------------------------------ 配置切换表（多配置）

def config_profiles_path(config_path: str | Path) -> Path:
    """配置预设文件与 config.yaml 同目录（config.profiles.yaml）。"""
    return Path(config_path).with_name("config.profiles.yaml")


def _read_yaml_doc(path: Path) -> dict[str, Any]:
    import yaml

    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_yaml_doc(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.mu-tmp-{os.getpid()}-{time.time_ns()}")
    tmp.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    os.replace(tmp, path)


def load_config_profiles(profiles_path: str | Path) -> dict[str, Any]:
    """读取预设文件，统一返回 {"active": str, "profiles": {名称: 条目}}。"""
    data = _read_yaml_doc(Path(profiles_path))
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    active = data.get("active")
    if not isinstance(active, str) or active not in profiles:
        active = ""
    return {"active": active, "profiles": profiles}


def normalize_profile_name(name: Any) -> str:
    text = str(name or "").strip()
    if not text:
        raise ConfigError("配置名称不能为空")
    if len(text) > 50:
        raise ConfigError("配置名称最长 50 字")
    if any(ord(c) < 32 for c in text):
        raise ConfigError("配置名称包含非法字符")
    return text


def save_profile_config(
    profiles_path: str | Path, name: Any, config_payload: Any
) -> tuple[bool, dict[str, Any]]:
    """保存/覆盖一个配置预设；返回 (是否新建, 最新数据)。"""
    name = normalize_profile_name(name)
    if not isinstance(config_payload, dict):
        raise ConfigError("配置内容为空，无法保存")
    if not isinstance(config_payload.get("platforms"), dict) or not config_payload.get(
        "platforms"
    ):
        raise ConfigError("配置内容为空：请先在页面填写平台账号/设置后再保存")
    path = Path(profiles_path)
    data = _read_yaml_doc(path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    created = name not in profiles
    profiles[name] = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": config_payload,
    }
    active = data.get("active") if isinstance(data.get("active"), str) else ""
    if active not in profiles:
        active = ""
    result = {"active": active, "profiles": profiles}
    _write_yaml_doc(path, result)
    return created, result


def rename_profile_config(
    profiles_path: str | Path, old_name: Any, new_name: Any
) -> dict[str, Any]:
    old = normalize_profile_name(old_name)
    new = normalize_profile_name(new_name)
    if old == new:
        raise ConfigError("新名称与旧名称相同")
    data = load_config_profiles(profiles_path)
    if old not in data["profiles"]:
        raise ConfigError(f"找不到配置：{old}")
    if new in data["profiles"]:
        raise ConfigError(f"已存在同名配置：{new}")
    entry = data["profiles"].pop(old)
    data["profiles"][new] = entry
    if data["active"] == old:
        data["active"] = new
    _write_yaml_doc(Path(profiles_path), data)
    return data


def delete_profile_config(profiles_path: str | Path, name: Any) -> bool:
    name = normalize_profile_name(name)
    data = load_config_profiles(profiles_path)
    if name not in data["profiles"]:
        return False
    data["profiles"].pop(name)
    if data["active"] == name:
        data["active"] = ""
    _write_yaml_doc(Path(profiles_path), data)
    return True


def _platform_block(item: dict[str, Any]) -> dict[str, Any]:
    """把 config.yaml 里的平台块规范成 {enabled, cookies, settings}。"""
    settings = item.get("settings")
    return {
        "enabled": bool(item.get("enabled", True)),
        "cookies": {str(k): str(v) for k, v in (item.get("cookies") or {}).items()},
        "settings": settings if isinstance(settings, dict) else {},
    }


def merge_payload_keep_cookies(
    config_path: str | Path, payload: dict[str, Any]
) -> dict[str, Any]:
    """切换预设时，把 config.yaml 里已有而预设没写的平台 / Cookie 补回来。

    预设只是一份“当时的快照”：早期保存的预设可能压根没有别的平台（或该平台的
    Cookie 还是空的）。直接整块覆盖 config.yaml 会把那些平台的登录态清空——
    用户反馈的“切一次配置就掉 Cookie”就是这么来的。

    合并规则：
    - 预设里没有的平台：完整保留 config.yaml 里的那份；
    - 预设里有该平台、但某个 Cookie 键缺失或为空：用 config.yaml 的值补上；
    - 预设里非空的 Cookie：以预设为准（切预设仍然能换账号）。
    """
    if not isinstance(payload, dict):
        return payload
    path = Path(config_path)
    if not path.is_file():
        return payload
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return payload
    if not isinstance(raw, dict):
        return payload
    existing = raw.get("platforms")
    if not isinstance(existing, dict):
        return payload

    merged = copy.deepcopy(payload)
    platforms = merged.get("platforms")
    if not isinstance(platforms, dict):
        return merged

    for key, item in existing.items():
        if not isinstance(item, dict):
            continue
        current = _platform_block(item)
        incoming = platforms.get(key)
        if not isinstance(incoming, dict):
            platforms[str(key)] = current
            continue
        cookies = incoming.get("cookies")
        if not isinstance(cookies, dict):
            cookies = {}
        for ckey, cvalue in current["cookies"].items():
            if not str(cookies.get(ckey) or "").strip():
                cookies[ckey] = cvalue
        incoming["cookies"] = cookies
    return merged


def switch_profile_config(
    profiles_path: str | Path, config_path: str | Path, name: Any
) -> dict[str, Any]:
    """把指定预设写入 config.yaml 并标记为当前使用；返回该预设的 common+platforms。"""
    name = normalize_profile_name(name)
    data = load_config_profiles(profiles_path)
    if name not in data["profiles"]:
        raise ConfigError(f"找不到配置：{name}")
    entry = data["profiles"][name]
    payload = entry.get("config")
    if not isinstance(payload, dict):
        raise ConfigError(f"配置「{name}」内容损坏，请删除后重新保存")
    payload = merge_payload_keep_cookies(config_path, payload)
    save_config(config_path, payload)
    data["active"] = name
    _write_yaml_doc(Path(profiles_path), data)
    return payload


# ------------------------------------------------------------ B站扫码（分步）

_BILI_QR_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "Chrome/126.0 Safari/537.36"
)
# 扫码登录能拿到的会话 Cookie：设备指纹一并保存，减少后续 -352
_BILI_QR_WANTED = [
    "SESSDATA",
    "bili_jct",
    "buvid3",
    "buvid4",
    "b_nut",
    "DedeUserID",
]


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
