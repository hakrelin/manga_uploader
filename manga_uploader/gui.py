"""图形界面：账号 Cookie 管理、漫画导入、压缩/代理设置、检查与一键发布。

运行方式：
    python -m manga_uploader --gui
    或直接双击根目录的 gui.pyw

各平台“账号密码”登录大多有验证码/滑块限制，程序不做验证码绕过；
推荐的登录方式是浏览器登录后复制单个 Cookie 值（如 SESSDATA、BDUSS、
bili_jct、token）分别填入；每个平台都提供“粘贴整段 Cookie”按钮可一键拆分。
B站额外支持扫码登录（需要 pip install qrcode[pil]）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import webbrowser
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .comic import META_FILES, load_chapters
from . import composer
from .config import (
    AppConfig,
    CommonConfig,
    ConfigError,
    DEFAULT_SETTINGS,
    PlatformConfig,
    REQUIRED_COOKIES,
    find_config_file,
)
from .models import Chapter
from .publishers.ehentai import DEFAULT_FIELD_ROWS
from .publishers.zaimanhua import CATE_LABELS
from .runner import PLATFORM_CLASSES, Runner
from .util import get_logger, human_size, is_image, natural_sort_key, setup_logging

LOGGER_NAME = "manga_uploader"

# 平台卡片顺序与说明
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
        "上传入口：upload.e-hentai.org/managegallery?act=new；通常需要代理直连。"
        "画廊分类/语言/汉化标记等发布内容请在“漫画与压缩 → 各平台发布内容”里设置。",
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
    },
]


# 漫画信息页：基础字段（写入 manga.json 根级，作为各平台自动组合的来源）
BASE_FIELDS: list[tuple[str, str, str]] = [
    ("event", "展会（如 C105）", "text"),
    ("author", "作者/画师（日文原标题侧用原名）", "text"),
    ("author_en", "作者罗马音（可留空，自动转换）", "text"),
    ("circle", "社团", "text"),
    ("circle_en", "社团罗马音（可留空，自动转换）", "text"),
    ("group", "汉化组（如 茶与金平糖汉化组）", "text"),
    ("title", "中文标题", "text"),
    ("title_jp", "日文原标题", "text"),
    ("title_en", "英文/罗马音标题", "text"),
    ("series", "系列/tag 中文（如 东方）", "text"),
    ("series_en", "系列英文（如 Touhou Project）", "text"),
    ("series_jp", "系列日文（如 東方Project）", "text"),
    ("language", "语言（Chinese / 中文）", "text"),
    ("tags", "标签（逗号分隔，如 东方,汉化）", "text"),
    ("chapter_name", "再漫画章节名（默认短篇）", "text"),
    ("description", "简介", "textarea"),
]


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
            for row in content:
                lines.append("      " + row)
    return "\n".join(lines)


def _cate_label(value: str) -> str:
    return CATE_LABELS.get(str(value), str(value))


class _LogHandler(logging.Handler):
    """把日志转发到主线程刷新 GUI Text。"""

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__(logging.INFO)
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self._callback(message)
        except Exception:  # pragma: no cover
            pass


class _ScrollFrame(ttk.Frame):
    """带垂直滚动条的 Frame。"""

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bd=0, highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._window, width=e.width),
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def bind_mousewheel(self) -> None:
        def _on_enter(_e: tk.Event) -> None:
            self.canvas.bind_all("<MouseWheel>", self._wheel)

        def _on_leave(_e: tk.Event) -> None:
            self.canvas.unbind_all("<MouseWheel>")

        self.canvas.bind("<Enter>", _on_enter)
        self.canvas.bind("<Leave>", _on_leave)

    def _wheel(self, event: tk.Event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class UploaderApp:
    def __init__(self, root: tk.Tk, config_path: str | None = None) -> None:
        self.root = root
        self.root.title(f"漫画多平台发布器 v{__version__}")
        self.root.geometry("1020x780")
        self.root.minsize(880, 640)

        self.config_path: Optional[Path] = None
        self.app = self._load_or_default(config_path)
        self.chapters: list[Chapter] = []

        # 通用配置变量
        self.timeout_var = tk.StringVar(value=str(self.app.common.timeout))
        self.retries_var = tk.StringVar(value=str(self.app.common.retries))
        self.interval_var = tk.StringVar(value=str(self.app.common.interval_seconds))
        self.max_width_var = tk.StringVar(value=str(self.app.common.max_width))
        self.max_height_var = tk.StringVar(value=str(self.app.common.max_height))
        self.quality_var = tk.StringVar(value=str(self.app.common.quality))
        self.max_mb_var = tk.StringVar(value=f"{self.app.common.max_bytes_mb:g}")
        self.output_dir_var = tk.StringVar(value=self.app.common.output_dir)
        self.parallel_var = tk.BooleanVar(value=self.app.common.parallel)
        self.verbose_var = tk.BooleanVar(value=self.app.common.verbose)
        self.use_system_proxy_var = tk.BooleanVar(value=self.app.common.use_system_proxy)
        self.proxy_url_var = tk.StringVar(value=self.app.common.proxy_url)

        # 平台相关变量（先建空，_build_account_tab 里填充）
        self.enabled_vars: dict[str, tk.BooleanVar] = {}
        self.cookie_vars: dict[str, dict[str, tk.StringVar]] = {}
        self.status_vars: dict[str, tk.StringVar] = {}
        self.extra_vars: dict[str, dict[str, tk.Variable]] = {}
        self.field_maps: dict[str, list[dict[str, Any]]] = {}
        self.meta_vars: dict[str, tk.Variable] = {}
        self.meta_widgets: dict[str, tk.Widget] = {}
        self.platform_content_vars: dict[str, dict[str, tk.Variable]] = {}
        self.platform_content_widgets: dict[str, dict[str, tk.Widget]] = {}
        self.platform_schema_rows: dict[str, dict[str, dict[str, Any]]] = {}
        self._editing_chapter: Chapter | None = None
        self.comic_dir_var = tk.StringVar()
        self.chapter_list: Optional[tk.Listbox] = None
        self.meta_var = tk.StringVar()
        self.log_text: Optional[tk.Text] = None
        self._busy = False

        self._build_layout()
        self._populate_platform_ui()
        self._sync_common_ui()

        setup_logging(verbose=False, log_file=None)
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(logging.DEBUG if self.verbose_var.get() else logging.INFO)
        handler = _LogHandler(self._append_log)
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
        self._log(
            "就绪。在各平台卡片分别填入 Cookie（或用“粘贴整段 Cookie”/扫码登录），"
            "再到“漫画与压缩”选择目录或点“上传漫画”。"
        )
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 基础 ----------

    def _load_or_default(self, config_path: str | None) -> AppConfig:
        try:
            path = find_config_file(config_path)
            self.config_path = path
            return self._load_config(path)
        except ConfigError as exc:
            self.config_path = None
            self.root.after(200, lambda: self._info(f"未找到可用配置文件：{exc}"))
            return self._default_app()

    @staticmethod
    def _load_config(path: Path) -> AppConfig:
        from .config import load_config

        return load_config(str(path))

    @staticmethod
    def _default_app() -> AppConfig:
        common = CommonConfig()
        platforms = {
            name: PlatformConfig(name=name, enabled=True, cookies={}, settings=copy.deepcopy(defaults))
            for name, defaults in DEFAULT_SETTINGS.items()
        }
        return AppConfig(common=common, platforms=platforms)

    def _log(self, message: str) -> None:
        self._append_log(message)

    def _append_log(self, message: str) -> None:
        def _do() -> None:
            if self.log_text is None:
                return
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        try:
            self.root.after(0, _do)
        except tk.TclError:  # pragma: no cover
            pass

    def _info(self, message: str) -> None:
        self.root.after(0, lambda: messagebox.showinfo("提示", message, parent=self.root))

    def _warn(self, message: str) -> None:
        self.root.after(0, lambda: messagebox.showwarning("提示", message, parent=self.root))

    # ---------- 布局 ----------

    def _build_layout(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 2))

        self.tab_account = ttk.Frame(self.notebook)
        self.tab_comic = ttk.Frame(self.notebook)
        self.tab_publish = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_account, text=" 平台账号 ")
        self.notebook.add(self.tab_comic, text=" 漫画与压缩 ")
        self.notebook.add(self.tab_publish, text=" 发布与日志 ")

        self._build_account_tab()
        self._build_comic_tab()
        self._build_publish_tab()

    # ---- 平台账号页 ----

    def _build_account_tab(self) -> None:
        scroll = _ScrollFrame(self.tab_account)
        scroll.pack(fill="both", expand=True)
        scroll.bind_mousewheel()
        body = scroll.inner
        body.columnconfigure(0, weight=1)
        title = ttk.Label(
            body,
            text="把各 Cookie 分别填入对应输入框（如 SESSDATA、bili_jct、BDUSS、token）。"
            "已登录的浏览器里 F12 → Network → 请求头 Cookie 可复制单个值；"
            "也可点“粘贴整段 Cookie”自动拆分。账号密码登录因验证码限制请用浏览器登录。",
            wraplength=950,
        )
        title.grid(row=0, column=0, sticky="w", pady=(0, 8))

        self._platform_frames: dict[str, ttk.LabelFrame] = {}
        for index, card in enumerate(PLATFORM_CARDS, start=1):
            frame = ttk.LabelFrame(body, text=card["label"], padding=(8, 6))
            frame.grid(row=index, column=0, sticky="ew", pady=4)
            self._platform_frames[card["key"]] = frame

        proxy = ttk.LabelFrame(body, text="网络代理", padding=(8, 6))
        proxy.grid(row=len(PLATFORM_CARDS) + 1, column=0, sticky="ew", pady=(8, 4))
        self._build_proxy_ui(proxy)

    def _build_proxy_ui(self, parent: tk.Widget) -> None:
        hint = (
            "默认直连。e-hentai 等海外站连不上时再开“系统代理”或填手动代理；"
            "国内平台走国外代理可能触发风控。"
        )
        ttk.Label(parent, text=hint, wraplength=930).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(
            parent, text="使用 Windows 系统代理（自动检测注册表/环境变量）",
            variable=self.use_system_proxy_var,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(parent, text="手动代理 URL：").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.proxy_url_var, width=64).grid(
            row=2, column=1, columnspan=3, sticky="w", pady=4
        )
        ttk.Button(parent, text="检测系统代理", command=self._detect_proxy).grid(
            row=3, column=0, sticky="w", pady=2
        )
        self.detected_proxy_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.detected_proxy_var, foreground="#666").grid(
            row=3, column=1, columnspan=3, sticky="w"
        )

    def _detect_proxy(self) -> None:
        from .http_client import detect_system_proxy

        url = detect_system_proxy()
        self.detected_proxy_var.set(f"检测到：{url or '无（未开启系统代理）'}")
        if url:
            self.proxy_url_var.set(url)

    def _open_browser(self, url: str) -> None:
        """打开系统默认浏览器；pythonw 环境下 webbrowser 可能静默失败，做多重兜底。"""
        url = url or ""
        if not url:
            self._warn("该平台没有可打开的登录页")
            return
        if os.name == "nt":
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                self._log(f"已用系统默认浏览器打开：{url}")
                return
            except Exception as exc:
                self._log(f"os.startfile 打开失败：{exc}")
        try:
            if webbrowser.open(url, new=2):
                self._log(f"已打开浏览器：{url}")
                return
        except Exception as exc:
            self._log(f"webbrowser.open 打开失败：{exc}")
        # 最后兜底：把网址复制到剪贴板并提示
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.root.update()
            copied = "（网址已复制到剪贴板）"
        except tk.TclError:
            copied = ""
        self._warn(f"无法自动打开浏览器{copied}，请手动访问：\n{url}")

    def _fill_cookie_vars(self, key: str, cookies: dict[str, str]) -> list[str]:
        """把解析出的 Cookie 写入某平台的字段；返回实际写入的字段名。"""
        filled = []
        for name in self.cookie_vars.get(key, {}):
            if name in cookies and cookies[name]:
                self.cookie_vars[key][name].set(cookies[name])
                filled.append(name)
        return filled

    def _populate_platform_ui(self) -> None:
        for card in PLATFORM_CARDS:
            key = card["key"]
            cfg = self.app.platforms.get(key)
            enabled = bool(cfg and cfg.enabled)
            self.enabled_vars[key] = tk.BooleanVar(value=enabled)
            self.status_vars[key] = tk.StringVar(value="未检查")
            self.extra_vars[key] = {}
            self.cookie_vars[key] = {}

            frame = self._platform_frames[key]
            head = ttk.Frame(frame)
            head.pack(fill="x")
            ttk.Checkbutton(
                head, text="启用此平台", variable=self.enabled_vars[key]
            ).pack(side="left")
            ttk.Button(
                head, text="检查登录", command=lambda k=key: self._check_one(k)
            ).pack(side="right", padx=(4, 0))
            ttk.Button(
                head,
                text="打开登录页",
                command=lambda k=key: self._open_browser(PLATFORM_CARDS[k]["login_url"]),
            ).pack(side="right", padx=(4, 0))
            if card.get("qr"):
                ttk.Button(head, text="扫码登录", command=self._bilibili_qr_login).pack(
                    side="right", padx=(4, 0)
                )

            cookies = (cfg.cookies if cfg else {}) or {}
            ttk.Button(
                frame,
                text="粘贴整段 Cookie（自动拆分）…",
                command=lambda k=key: self._paste_cookie_bundle(k),
            ).pack(anchor="w", pady=(4, 0))
            self._build_cookie_rows(frame, card, cookies)

            ttk.Label(frame, text=card["hint"], foreground="#666", wraplength=940).pack(
                anchor="w", pady=(2, 0)
            )
            ttk.Label(frame, textvariable=self.status_vars[key], foreground="#2a7").pack(
                anchor="w", pady=(2, 0)
            )

    def _build_cookie_rows(
        self, frame: tk.Widget, card: dict[str, Any], cookies: dict[str, str]
    ) -> None:
        """每个 Cookie 单独一行输入框（必需/可选分开标注）。"""
        key = card["key"]
        required = [f["name"] for f in card.get("cookie_fields", []) if f.get("required")]
        optional = [f["name"] for f in card.get("cookie_fields", []) if not f.get("required")]
        field_hints = {f["name"]: f.get("hint") for f in card.get("cookie_fields", [])}
        # 兼容历史配置：配置里出现的多余 Cookie 也显示出来
        extra_names = sorted(set(cookies) - set(required) - set(optional))
        optional.extend(extra_names)

        for name in required + optional:
            var = tk.StringVar(value=str(cookies.get(name, "")))
            self.cookie_vars[key][name] = var
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=1)
            label_text = name + ("（必需）" if name in required else "（可选）")
            ttk.Label(row, text=label_text, width=24, anchor="w").pack(side="left")
            ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
            hint = field_hints.get(name)
            if hint:
                ttk.Label(row, text=hint, foreground="#999").pack(side="left", padx=6)

    def _paste_cookie_bundle(self, key: str) -> None:
        """弹窗粘贴 'k=v; k2=v2'，自动填到各字段。"""
        win = tk.Toplevel(self.root)
        win.title("粘贴整段 Cookie")
        win.geometry("560x180")
        win.transient(self.root)
        ttk.Label(
            win,
            text="从浏览器复制整段 Cookie 粘贴到下面，确定后会自动拆到各字段：",
            wraplength=520,
        ).pack(anchor="w", padx=8, pady=(8, 2))
        text = tk.Text(win, height=4, wrap="char")
        text.pack(fill="both", expand=True, padx=8)
        result: dict[str, str] = {}

        def _ok() -> None:
            parsed = parse_cookie_text(text.get("1.0", "end"))
            if not parsed:
                self._warn("没有解析到任何 Cookie（格式：k=v; k2=v2）")
                return
            result.update(parsed)
            win.destroy()

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", pady=6)
        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="right", padx=6)
        ttk.Button(buttons, text="填入", command=_ok).pack(side="right", padx=6)
        win.wait_window()
        if not result:
            return
        filled = []
        for name in self.cookie_vars.get(key, {}):
            if name in result:
                self.cookie_vars[key][name].set(result[name])
                filled.append(name)
        self._log(f"已把 {len(filled)} 个 Cookie 填到 {key}：{', '.join(filled)}")

    # ---- 上传表单字段填写配置（e-hentai 等表单平台） ----

    _FIELD_SOURCE_CHOICES: list[tuple[str, str]] = [
        ("章节标题", "title"),
        ("系列名", "series"),
        ("作者", "author"),
        ("简介", "description"),
        ("标签", "tags"),
        ("manga.json/config 字段", "meta:"),
        ("固定文本", "text:"),
        ("下拉框：分类（按选项文本匹配）", "category"),
        ("下拉框：语言（按选项文本匹配）", "language"),
        ("下拉框：评分（按选项文本匹配）", "rating"),
    ]

    def _open_field_map_editor(self, key: str) -> None:
        card = next((c for c in PLATFORM_CARDS if c["key"] == key), {})
        rows = copy.deepcopy(self.field_maps.get(key) or DEFAULT_FIELD_ROWS)
        win = tk.Toplevel(self.root)
        win.title(f"{card.get('label', key)} - 上传表单填写配置")
        win.geometry("1060x620")
        win.transient(self.root)

        hint = (
            "每个表单输入框一行：\n"
            "• 页面字段名：浏览器打开上传页后 F12 查看输入框的 name（如 name / name_jpn / comment），可留空自动识别；\n"
            "• 内容来源：章节标题 / 简介 / manga.json 字段 / 固定文本等；下拉框选对应“选项匹配”。\n"
            "manga.json 字段示例：platforms.ehentai 里写 title_jpn: \"原作日文标题\"，来源选 manga.json 字段并填 title_jpn。"
        )
        ttk.Label(win, text=hint, wraplength=1020, justify="left").pack(
            anchor="w", padx=10, pady=(8, 2)
        )

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=10)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        scroll = _ScrollFrame(body)
        scroll.grid(row=0, column=0, sticky="nsew")
        inner = scroll.inner

        heads = ["用途说明", "页面字段名(name)", "内容来源", "参数(字段键/固定文本)", ""]
        for col, text in enumerate(heads):
            ttk.Label(inner, text=text, font=("", 9, "bold")).grid(
                row=0, column=col, sticky="w", padx=4, pady=4
            )

        row_data: list[dict] = []

        def _redraw() -> None:
            for record in row_data:
                for widget in record["widgets"]:
                    widget.destroy()
            row_data.clear()
            for index, row in enumerate(rows, start=1):
                _add_row(index, row)

        def _add_row(index: int, row: dict) -> None:
            label_var = tk.StringVar(value=str(row.get("label") or ""))
            field_var = tk.StringVar(value=str(row.get("field") or ""))
            source = str(row.get("source") or "title")
            base = source
            param = ""
            for label, token in self._FIELD_SOURCE_CHOICES:
                if source == token or (token.endswith(":") and source.startswith(token)):
                    base = token
                    if token.endswith(":"):
                        param = source[len(token):]
                    break
            display = next(
                (label for label, token in self._FIELD_SOURCE_CHOICES if token == base),
                base,
            )
            source_var = tk.StringVar(value=display)
            param_var = tk.StringVar(value=param)

            widgets = []
            label_entry = ttk.Entry(inner, textvariable=label_var, width=14)
            label_entry.grid(row=index, column=0, sticky="ew", padx=4, pady=2)
            widgets.append(label_entry)
            field_entry = ttk.Entry(inner, textvariable=field_var, width=18)
            field_entry.grid(row=index, column=1, sticky="ew", padx=4, pady=2)
            widgets.append(field_entry)
            combo = ttk.Combobox(
                inner,
                textvariable=source_var,
                values=[label for label, _ in self._FIELD_SOURCE_CHOICES],
                width=30,
                state="readonly",
            )
            combo.grid(row=index, column=2, sticky="ew", padx=4, pady=2)
            widgets.append(combo)
            param_entry = ttk.Entry(inner, textvariable=param_var, width=24)
            param_entry.grid(row=index, column=3, sticky="ew", padx=4, pady=2)
            widgets.append(param_entry)

            def _refresh_param_state(*_args) -> None:
                label = str(source_var.get())
                needs = any(
                    token.endswith(":") and shown == label
                    for shown, token in self._FIELD_SOURCE_CHOICES
                )
                param_entry.configure(state="normal" if needs else "disabled")

            source_var.trace_add("write", _refresh_param_state)
            _refresh_param_state()

            delete_btn = ttk.Button(
                inner, text="删除", command=lambda: _delete_row(row)
            )
            delete_btn.grid(row=index, column=4, padx=4, pady=2)
            widgets.append(delete_btn)
            row_data.append(
                {
                    "row": row,
                    "label_var": label_var,
                    "field_var": field_var,
                    "source_var": source_var,
                    "param_var": param_var,
                    "widgets": widgets,
                }
            )

        def _delete_row(row: dict) -> None:
            if row in rows:
                rows.remove(row)
            _redraw()

        for idx, row in enumerate(rows, start=1):
            _add_row(idx, row)

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=10, pady=8)

        def _add_new() -> None:
            rows.append({"label": "", "field": "", "source": "title"})
            _redraw()

        def _save() -> None:
            result: list[dict[str, str]] = []
            for record in row_data:
                source_label = str(record["source_var"].get())
                token = next(
                    (t for label, t in self._FIELD_SOURCE_CHOICES if label == source_label),
                    "",
                )
                if token.endswith(":"):
                    param = str(record["param_var"].get()).strip()
                    if not param:
                        self._warn("manga.json 字段/固定文本需要填写参数")
                        return
                    source = token + param
                else:
                    source = token
                result.append(
                    {
                        "label": str(record["label_var"].get()).strip(),
                        "field": str(record["field_var"].get()).strip(),
                        "source": source,
                    }
                )
            self.field_maps[key] = result
            self._log(f"已保存 {key} 的上传表单字段映射：{len(result)} 行")
            win.destroy()
            self._save_config()

        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="添加一行", command=_add_new).pack(side="right", padx=4)
        ttk.Button(buttons, text="保存配置", command=_save).pack(side="right", padx=4)

    def _build_extra_fields(self, frame: tk.Widget, card: dict[str, Any]) -> None:
        key = card["key"]
        cfg = self.app.platforms.get(key)
        settings = (cfg.settings if cfg else {}) or {}
        for extra_key, placeholder in card.get("extras", []):
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=2)
            label = {
                "forum": "目标吧名",
                "category_label": "默认分类",
                "cate": "作品类型",
                "language_label": "画廊语言",
                "langtype": "语言类型",
                "title_jpn": "默认日文标题",
            }.get(extra_key, extra_key)
            ttk.Label(row, text=f"{label}：").pack(side="left")
            if extra_key == "cate":
                cate_value = str(settings.get("cate", "1"))
                var = tk.StringVar(value=f"{cate_value} - {_cate_label(cate_value)}")
                combo = ttk.Combobox(
                    row,
                    textvariable=var,
                    values=[f"{k} - {_cate_label(k)}" for k in sorted(CATE_LABELS)],
                    width=24,
                    state="readonly",
                )
                combo.pack(side="left")
                self.extra_vars[key]["cate"] = var
                continue
            var = tk.StringVar(value=str(settings.get(extra_key, "")))
            entry = ttk.Entry(row, textvariable=var, width=56)
            entry.pack(side="left", fill="x", expand=True)
            if placeholder:
                hint = ttk.Label(row, text=placeholder, foreground="#999")
                hint.pack(side="left", padx=6)
            self.extra_vars[key][extra_key] = var

    # ---- 漫画与压缩页 ----

    def _build_comic_tab(self) -> None:
        body = self.tab_comic
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        top = ttk.Frame(body)
        top.grid(row=0, column=0, sticky="ew", pady=(6, 4))
        ttk.Label(top, text="漫画目录：").pack(side="left")
        ttk.Entry(top, textvariable=self.comic_dir_var, width=70).pack(
            side="left", fill="x", expand=True, padx=4
        )
        ttk.Button(top, text="浏览…", command=self._pick_comic_dir).pack(side="left")
        ttk.Button(top, text="加载章节", command=self._load_comic).pack(side="left", padx=4)
        ttk.Button(top, text="上传漫画…", command=self._import_comic).pack(side="left")

        self.import_hint_var = tk.StringVar(value="")
        ttk.Label(
            top, textvariable=self.import_hint_var, foreground="#777"
        ).pack(side="left", padx=8)

        mid = ttk.LabelFrame(body, text="章节（Ctrl/Shift 多选，不选则全部）", padding=6)
        mid.grid(row=1, column=0, sticky="ew", pady=4)
        mid.columnconfigure(0, weight=1)
        self.chapter_list = tk.Listbox(mid, height=6, selectmode="extended")
        self.chapter_list.grid(row=0, column=0, sticky="ew")
        self.chapter_list.bind("<<ListboxSelect>>", self._on_chapter_select)
        ttk.Label(mid, textvariable=self.meta_var, wraplength=960, foreground="#333").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )

        self.comic_sub = ttk.Notebook(body)
        self.comic_sub.grid(row=2, column=0, sticky="nsew", pady=(2, 4))
        page_meta = ttk.Frame(self.comic_sub)
        page_platform = ttk.Frame(self.comic_sub)
        page_compress = ttk.Frame(self.comic_sub)
        self.comic_sub.add(page_meta, text=" 漫画信息 ")
        self.comic_sub.add(page_platform, text=" 各平台发布内容 ")
        self.comic_sub.add(page_compress, text=" 压缩与通用 ")
        self._build_comic_meta_page(page_meta)
        self._build_platform_content_page(page_platform)
        self._build_compress_page(page_compress)

    # ---- 漫画信息（基础字段，自动组合的来源） ----

    def _build_comic_meta_page(self, parent: tk.Widget) -> None:
        scroll = _ScrollFrame(parent)
        scroll.pack(fill="both", expand=True, padx=4, pady=4)
        scroll.bind_mousewheel()
        body = scroll.inner
        body.columnconfigure(1, weight=1)
        ttk.Label(
            body,
            text="按上传习惯填写一次；保存后会自动生成“各平台发布内容”。"
            "作者/社团/日文标题的罗马音可留空，用右侧按钮自动转换后手动微调。",
            foreground="#555",
            wraplength=940,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(2, 6))

        textarea_keys = {key for key, _label, kind in BASE_FIELDS if kind == "textarea"}
        row_index = 1
        for key, label, kind in BASE_FIELDS:
            label_widget = ttk.Label(body, text=label + "：")
            label_widget.grid(row=row_index, column=0, sticky="nw", padx=(4, 2), pady=3)
            if kind == "textarea":
                text = tk.Text(body, height=5, wrap="word")
                text.grid(
                    row=row_index, column=1, columnspan=3, sticky="ew", padx=4, pady=3
                )
                self.meta_widgets[key] = text
                row_index += 1
                continue
            var = tk.StringVar()
            entry = ttk.Entry(body, textvariable=var)
            entry.grid(row=row_index, column=1, columnspan=3, sticky="ew", padx=4, pady=3)
            self.meta_vars[key] = var
            self.meta_widgets[key] = entry
            row_index += 1
            if row_index == 9:
                row_index += 1  # 留一行给转换按钮，视觉分组

        buttons = ttk.Frame(body)
        buttons.grid(row=row_index + 2, column=0, columnspan=4, sticky="w", pady=6)
        ttk.Button(
            buttons,
            text="作者/社团 → 罗马音",
            command=self._fill_romaji_names,
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons,
            text="日文标题 → 罗马音标题",
            command=self._fill_romaji_title,
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons,
            text="保存漫画信息",
            command=self._save_comic_meta,
        ).pack(side="left", padx=4)
        ttk.Label(
            body,
            text="（保存后自动按新信息重新生成各平台发布内容）",
            foreground="#888",
        ).grid(row=row_index + 3, column=0, columnspan=4, sticky="w", pady=(0, 6))

    def _meta_value(self, key: str) -> str:
        var = self.meta_vars.get(key)
        if var is not None:
            return str(var.get()).strip()
        widget = self.meta_widgets.get(key)
        if isinstance(widget, tk.Text):
            return widget.get("1.0", "end").strip()
        return ""

    def _set_meta_value(self, key: str, value: Any) -> None:
        var = self.meta_vars.get(key)
        if var is not None:
            var.set("" if value is None else str(value))
            return
        widget = self.meta_widgets.get(key)
        if isinstance(widget, tk.Text):
            widget.delete("1.0", "end")
            widget.insert("1.0", "" if value is None else str(value))

    def _fill_romaji_names(self) -> None:
        author = self._meta_value("author")
        circle = self._meta_value("circle")
        if author and not self._meta_value("author_en"):
            self._set_meta_value("author_en", composer.to_romaji(author))
        if circle and not self._meta_value("circle_en"):
            self._set_meta_value("circle_en", composer.to_romaji(circle))

    def _fill_romaji_title(self) -> None:
        jp = self._meta_value("title_jp")
        if jp and not self._meta_value("title_en"):
            self._set_meta_value("title_en", composer.to_romaji(jp))

    # ---- 各平台发布内容（自动生成，可手动修改后保存） ----

    def _build_platform_content_page(self, parent: tk.Widget) -> None:
        scroll = _ScrollFrame(parent)
        scroll.pack(fill="both", expand=True, padx=4, pady=4)
        scroll.bind_mousewheel()
        body = scroll.inner
        body.columnconfigure(0, weight=1)
        ttk.Label(
            body,
            text="每个平台按上传表单陈列。点“重新生成”按左侧漫画信息自动组合，"
            "也可直接修改任意文本框；修改后的内容会随“保存”写入 manga.json 覆盖自动值。",
            foreground="#555",
            wraplength=960,
        ).grid(row=0, column=0, sticky="w", pady=(2, 6))

        select_options: dict[str, tuple[str, list[str]]] = {
            "ehentai": ("category", composer.ehentai_categories()),
            "zaimanhua": ("cate", [f"{k} - {label}" for k, label in sorted(CATE_LABELS.items())]),
        }
        extra_select_options: dict[tuple[str, str], list[str]] = {
            ("ehentai", "langtype"): ["0 - 官方/无字", "1 - 汉化（默认）", "2 - 改写"],
        }
        row = 1
        for card in PLATFORM_CARDS:
            key = card["key"]
            if key not in composer.PLATFORM_SCHEMA:
                continue
            frame = ttk.LabelFrame(body, text=card["label"], padding=(8, 4))
            frame.grid(row=row, column=0, sticky="ew", pady=4)
            row += 1
            self.platform_schema_rows[key] = {}
            self.platform_content_vars[key] = {}
            self.platform_content_widgets[key] = {}

            head = ttk.Frame(frame)
            head.pack(fill="x")
            ttk.Label(
                head,
                text=self._platform_content_hint(key),
                foreground="#777",
                wraplength=760,
            ).pack(side="left")
            ttk.Button(
                head,
                text="重新生成",
                command=lambda k=key: self._regenerate_platform(k),
            ).pack(side="right")

            for field_index, field_schema in enumerate(composer.PLATFORM_SCHEMA[key]):
                field_key = field_schema["key"]
                kind = field_schema["kind"]
                self.platform_schema_rows[key][field_key] = field_schema
                frow = ttk.Frame(frame)
                frow.pack(fill="x", pady=2)
                label = field_schema["label"]
                ttk.Label(frow, text=label + "：", width=30, anchor="w").pack(side="left")
                if kind == "select":
                    var = tk.StringVar()
                    if key in select_options and field_key == select_options[key][0]:
                        values = select_options[key][1]
                    elif (key, field_key) in extra_select_options:
                        values = extra_select_options[(key, field_key)]
                    else:
                        values = []
                    combo = ttk.Combobox(
                        frow,
                        textvariable=var,
                        values=values,
                        width=54,
                        state="readonly",
                    )
                    combo.pack(side="left", fill="x", expand=True)
                    self.platform_content_vars[key][field_key] = var
                    self.platform_content_widgets[key][field_key] = combo
                elif kind == "textarea":
                    text = tk.Text(frow, height=4, wrap="word")
                    text.pack(side="left", fill="x", expand=True)
                    self.platform_content_widgets[key][field_key] = text
                else:
                    var = tk.StringVar()
                    entry = ttk.Entry(frow, textvariable=var)
                    entry.pack(side="left", fill="x", expand=True)
                    self.platform_content_vars[key][field_key] = var
                    self.platform_content_widgets[key][field_key] = entry

        save_bar = ttk.Frame(body)
        save_bar.grid(row=row + 1, column=0, sticky="ew", pady=8)
        ttk.Button(
            save_bar,
            text="保存各平台发布内容",
            command=self._save_platform_content,
        ).pack(side="left", padx=4)
        ttk.Label(
            save_bar,
            text="（先选择左侧要发布的章节；当前编辑章节会显示在日志里）",
            foreground="#888",
        ).pack(side="left")

    def _platform_content_hint(self, key: str) -> str:
        hints = {
            "ehentai": (
                "示例：英文 (C105) [Taisanchi (Ichidai Taisa)] Bannou-gata … | 万能型… "
                "(Touhou Project) [Chinese] [茶与金平糖汉化组]；日文对应 [中国翻訳]"
            ),
            "bilibili": "标题：【汉化组】中文标题；正文：作者/社团/简介（图片自动排在正文后）",
            "tieba": "与 B站类似：标题【汉化组】中文标题；一楼放简介+封面，其余每楼最多 9 张",
            "zaimanhua": "标题=中文标题；简介=tag/作者/简介；章节名默认“短篇”",
        }
        return hints.get(key, "")

    def _platform_value(self, key: str, field: str) -> str:
        var = self.platform_content_vars.get(key, {}).get(field)
        if var is not None:
            return str(var.get()).strip()
        widget = self.platform_content_widgets.get(key, {}).get(field)
        if isinstance(widget, tk.Text):
            return widget.get("1.0", "end").strip()
        return ""

    def _set_platform_value(self, key: str, field: str, value: Any) -> None:
        var = self.platform_content_vars.get(key, {}).get(field)
        if var is not None:
            var.set("" if value is None else str(value))
            return
        widget = self.platform_content_widgets.get(key, {}).get(field)
        if isinstance(widget, tk.Text):
            widget.delete("1.0", "end")
            widget.insert("1.0", "" if value is None else str(value))

    def _temp_chapter(self) -> Chapter:
        """根据当前基础字段构造临时 Chapter，供“重新生成”计算。"""
        base = self._selected_chapter()
        if base is None:
            raise RuntimeError("请先加载章节")
        raw = copy.deepcopy(dict(base.raw))
        raw.setdefault("platforms", {})
        for field in (
            "event", "author", "author_en", "circle", "circle_en", "group",
            "title", "title_jp", "title_en", "series", "series_en", "series_jp",
            "language", "tags", "chapter_name",
        ):
            value = self._meta_value(field)
            raw[field] = value
        if self._meta_value("description"):
            raw["description"] = self._meta_value("description")
        if self._meta_value("tags"):
            raw["tags"] = [
                part.strip()
                for part in re.split(r"[,，、\n]", self._meta_value("tags"))
                if part.strip()
            ]
        chapter = Chapter(
            key=base.key,
            title=str(raw.get("title") or base.title),
            description=str(raw.get("description") or ""),
            tags=list(raw.get("tags") or base.tags),
            author=str(raw.get("author") or ""),
            cover=base.cover,
            pages=base.pages,
            source_dir=base.source_dir,
            raw=raw,
        )
        return chapter

    def _regenerate_platform(self, key: str) -> None:
        try:
            chapter = self._temp_chapter()
        except Exception as exc:
            self._warn(str(exc))
            return
        meta = composer.platform_meta(chapter, key)
        meta_override = copy.deepcopy(dict(meta))
        if key == "ehentai":
            meta_override.pop("gname_en", None)
            meta_override.pop("gname_jp", None)
            meta_override.pop("comment", None)
            language = self._platform_value("ehentai", "language") or "Chinese"
            langtype = (self._platform_value("ehentai", "langtype") or "1").split(" - ")[0]
            meta_override["language"] = language
            meta_override["langtype"] = langtype
            chapter.raw["platforms"][key] = meta_override
            self._set_platform_value("ehentai", "gname_en", composer.ehentai_title_en(chapter))
            self._set_platform_value("ehentai", "gname_jp", composer.ehentai_title_jp(chapter))
            self._set_platform_value("ehentai", "comment", composer.ehentai_comment(chapter))
            if not self._platform_value("ehentai", "category"):
                self._set_platform_value(
                    "ehentai",
                    "category",
                    self.app.platforms["ehentai"].get("category_label", "Doujinshi")
                    if "ehentai" in self.app.platforms
                    else "Doujinshi",
                )
        elif key in ("bilibili", "tieba"):
            meta_override.pop("title", None)
            meta_override.pop("description", None)
            chapter.raw["platforms"][key] = meta_override
            self._set_platform_value(key, "title", composer.platform_title(chapter, key))
            self._set_platform_value(key, "description", composer.platform_body(chapter, key))
        elif key == "zaimanhua":
            meta_override.pop("work_name", None)
            meta_override.pop("chapter_name", None)
            meta_override.pop("introduction", None)
            chapter.raw["platforms"][key] = meta_override
            self._set_platform_value("zaimanhua", "work_name", composer.zaim_work_name(chapter))
            self._set_platform_value("zaimanhua", "chapter_name", composer.zaim_chapter_name(chapter))
            self._set_platform_value("zaimanhua", "introduction", composer.zaim_introduction(chapter))
        self._log(f"已按漫画信息重新生成 {key} 发布内容（可继续手动修改）")

    @staticmethod
    def _langtype_display(value: str) -> str:
        mapping = {"0": "0 - 官方/无字", "1": "1 - 汉化（默认）", "2": "2 - 改写"}
        return mapping.get(str(value).strip(), str(value))

    # ---- 保存到 manga.json ----

    def _meta_root_path(self) -> Path:
        comic_dir = self.comic_dir_var.get().strip()
        if not comic_dir:
            raise RuntimeError("请先选择漫画目录")
        root = Path(comic_dir).expanduser().resolve()
        for name in META_FILES:
            candidate = root / name
            if candidate.is_file():
                return candidate
        return root / "manga.json"

    def _save_comic_meta(self) -> None:
        chapter = self._selected_chapter()
        if chapter is None:
            self._warn("请先加载漫画章节")
            return
        try:
            meta_path = self._meta_root_path()
            from .comic import read_meta

            raw = read_meta(meta_path)
            for field in (
                "event", "author", "author_en", "circle", "circle_en", "group",
                "title_jp", "title_en", "series", "series_en", "series_jp",
                "language", "chapter_name",
            ):
                value = self._meta_value(field)
                if value:
                    raw[field] = value
                else:
                    raw.pop(field, None)
            tags_value = self._meta_value("tags")
            raw["tags"] = (
                [p.strip() for p in re.split(r"[,，、\n]", tags_value) if p.strip()]
                if tags_value
                else []
            )
            self._write_chapter_fields(raw, chapter, title=True, description=True)
            self._write_meta_file(meta_path, raw)
            self._log(f"漫画信息已保存：{meta_path.name}（章节 {chapter.key}）")
            self._load_comic(reload_ui=True)
            self._select_chapter_key(chapter.key)
        except Exception as exc:
            self._warn(f"保存失败：{exc}")

    def _save_platform_content(self) -> None:
        chapter = self._selected_chapter()
        if chapter is None:
            self._warn("请先加载漫画章节")
            return
        try:
            meta_path = self._meta_root_path()
            from .comic import read_meta

            raw = read_meta(meta_path)
            raw.setdefault("platforms", {})
            entries = raw.setdefault("chapters", [])
            global_target = raw["platforms"]
            if chapter.key != "root" and len(entries) > 1:
                entry = None
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("folder") or item.get("key") or item.get("name")) == chapter.key:
                        entry = item
                        break
                if entry is None:
                    entry = {"folder": chapter.key}
                    entries.append(entry)
                entry.setdefault("platforms", {})
                global_target = entry["platforms"]
            for key in composer.PLATFORM_SCHEMA:
                if key not in self.platform_content_widgets:
                    continue
                target_platforms = global_target if key in ("ehentai", "bilibili", "tieba", "zaimanhua") else raw["platforms"]
                if key in ("tieba", "ehentai", "zaimanhua") and key not in ("bilibili",):
                    # forum/category/cate 属全局设置，其余文字内容按章节存
                    pass
                target_platforms.setdefault(key, {})
                for field in composer.PLATFORM_SCHEMA[key]:
                    field_key = field["key"]
                    global_keys = {"forum", "category", "cate"}
                    store = (
                        raw["platforms"].setdefault(key, {})
                        if field_key in global_keys and chapter.key != "root"
                        else target_platforms.setdefault(key, {})
                    )
                    value = self._platform_value(key, field_key)
                    if field_key == "langtype":
                        value = value.split(" - ")[0].strip()
                    if field_key in ("category", "cate"):
                        if field_key == "cate":
                            value = value.split(" - ")[0].strip() if value else value
                        if not value:
                            store.pop(field_key, None)
                        else:
                            store[field_key] = value
                        continue
                    if value:
                        store[field_key] = value
                    else:
                        store.pop(field_key, None)
            self._write_meta_file(meta_path, raw)
            self._log(f"各平台发布内容已保存（章节 {chapter.key}）")
            self._load_comic(reload_ui=True)
            self._select_chapter_key(chapter.key)
        except Exception as exc:
            self._warn(f"保存失败：{exc}")

    def _write_meta_file(self, path: Path, raw: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_chapter_fields(self, raw: dict, chapter: Chapter, *, title: bool, description: bool) -> None:
        """把当前 GUI 值写入 meta：单章/根目录写根级，多章写对应 chapters 条目。"""
        title_value = self._meta_value("title")
        description_value = self._meta_value("description")
        chapter_name = self._meta_value("chapter_name")
        target = raw
        entries = raw.setdefault("chapters", [])
        single_root = chapter.key == "root" and len(entries) <= 1
        if not single_root and chapter.key != "root":
            entry = None
            for item in entries:
                if not isinstance(item, dict):
                    continue
                if str(item.get("folder") or item.get("key") or item.get("name")) == chapter.key:
                    entry = item
                    break
            if entry is None:
                entry = {"folder": chapter.key}
                entries.append(entry)
            target = entry
        if title:
            if title_value:
                target["title"] = title_value
            else:
                target.pop("title", None)
        if description:
            if description_value:
                target["description"] = description_value
            else:
                target.pop("description", None)
        if chapter_name:
            target["chapter_name"] = chapter_name
        else:
            target.pop("chapter_name", None)
        if single_root:
            if title_value:
                raw["title"] = title_value
            if description_value:
                raw["description"] = description_value

    def _on_chapter_select(self, _event=None) -> None:
        chapter = self._selected_chapter()
        if chapter is not None:
            self._load_meta_ui(chapter)

    def _selected_chapter(self) -> Chapter | None:
        if self.chapter_list is None:
            return None
        selection = self.chapter_list.curselection()
        if not selection:
            return self.chapters[0] if self.chapters else None
        index = int(selection[0])
        return self.chapters[index] if 0 <= index < len(self.chapters) else None

    def _select_chapter_key(self, key: str) -> None:
        if self.chapter_list is None:
            return
        for index, chapter in enumerate(self.chapters):
            if chapter.key == key:
                self.chapter_list.selection_clear(0, "end")
                self.chapter_list.selection_set(index)
                self.chapter_list.see(index)
                self._on_chapter_select()
                return

    def _load_meta_ui(self, chapter: Chapter | None = None) -> None:
        chapter = chapter or self._selected_chapter()
        if chapter is None:
            return
        self._editing_chapter = chapter
        raw = chapter.raw
        mapping = {
            "event": raw.get("event", ""),
            "author": raw.get("author", "") or chapter.author,
            "author_en": raw.get("author_en", ""),
            "circle": raw.get("circle", ""),
            "circle_en": raw.get("circle_en", ""),
            "group": raw.get("group") or raw.get("group_name") or raw.get("汉化组") or "",
            "title": raw.get("title", "") or chapter.title,
            "title_jp": raw.get("title_jp") or raw.get("title_original") or raw.get("title_jpn") or "",
            "title_en": raw.get("title_en", ""),
            "series": raw.get("series") or raw.get("series_cn") or "",
            "series_en": raw.get("series_en", ""),
            "series_jp": raw.get("series_jp", ""),
            "language": raw.get("language", "") or "Chinese",
            "tags": "，".join(str(t) for t in (raw.get("tags") or chapter.tags)),
            "chapter_name": raw.get("chapter_name", "") or "",
            "description": raw.get("description", "") or chapter.description,
        }
        for key, value in mapping.items():
            self._set_meta_value(key, value)

        # 平台发布内容：覆盖值优先，无覆盖先显示自动组合结果
        temp = self._temp_chapter()
        platform_meta_values = {
            "ehentai": {
                "category": self.app.platforms.get("ehentai", None).get("category_label", "Doujinshi")
                if self.app.platforms.get("ehentai") else "Doujinshi",
                "language": self.app.platforms.get("ehentai", None).get("language_label", "Chinese")
                if self.app.platforms.get("ehentai") else "Chinese",
                "langtype": self.app.platforms.get("ehentai", None).get("langtype", "1")
                if self.app.platforms.get("ehentai") else "1",
                "gname_en": temp and composer.ehentai_title_en(temp) or "",
                "gname_jp": temp and composer.ehentai_title_jp(temp) or "",
                "comment": temp and composer.ehentai_comment(temp) or "",
            },
            "bilibili": {
                "title": temp and composer.platform_title(temp, "bilibili") or "",
                "description": temp and composer.platform_body(temp, "bilibili") or "",
            },
            "tieba": {
                "forum": self.app.platforms.get("tieba", None).get("forum", "")
                if self.app.platforms.get("tieba") else "",
                "title": temp and composer.platform_title(temp, "tieba") or "",
                "description": temp and composer.platform_body(temp, "tieba") or "",
            },
            "zaimanhua": {
                "work_name": temp and composer.zaim_work_name(temp) or "",
                "chapter_name": temp and composer.zaim_chapter_name(temp) or "",
                "introduction": temp and composer.zaim_introduction(temp) or "",
                "cate": self.app.platforms.get("zaimanhua", None).get("cate", "1")
                if self.app.platforms.get("zaimanhua") else "1",
            },
        }
        for key in composer.PLATFORM_SCHEMA:
            meta = composer.platform_meta(chapter, key)
            for field in composer.PLATFORM_SCHEMA[key]:
                field_key = field["key"]
                if field_key == "forum":
                    cfg = self.app.platforms.get("tieba")
                    value = meta.get("forum") or (cfg.get("forum", "") if cfg else "")
                else:
                    value = meta.get(field_key)
                if not value:
                    value = platform_meta_values.get(key, {}).get(field_key, "")
                if field_key == "langtype" and value:
                    value = self._langtype_display(value)
                if field_key == "cate" and value:
                    value = f"{value} - {_cate_label(value)}"
                self._set_platform_value(key, field_key, value)

    def _build_compress_ui(self, parent: tk.Widget) -> None:
        parent.columnconfigure(1, weight=1)
        rows = [
            ("单张上限 (MB)", self.max_mb_var, "0 = 不限制；平台常限制 10MB"),
            ("最长边 (px)", self.max_width_var, "0 = 不缩放；只等比缩小，不裁剪画面"),
            ("质量 (JPEG/WebP)", self.quality_var, "45–95"),
        ]
        for index, (label, var, hint) in enumerate(rows):
            ttk.Label(parent, text=label).grid(row=index, column=0, sticky="w", pady=3)
            ttk.Entry(parent, textvariable=var, width=12).grid(
                row=index, column=1, sticky="w", pady=3
            )
            ttk.Label(parent, text=hint, foreground="#888").grid(
                row=index, column=2, sticky="w", padx=8
            )

    def _build_common_ui(self, parent: tk.Widget) -> None:
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text="请求超时 (秒)").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=self.timeout_var, width=12).grid(
            row=0, column=1, sticky="w", pady=3
        )
        ttk.Label(parent, text="重试次数").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=self.retries_var, width=12).grid(
            row=1, column=1, sticky="w", pady=3
        )
        ttk.Label(parent, text="平台间/图片间隔 (秒)").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=self.interval_var, width=12).grid(
            row=2, column=1, sticky="w", pady=3
        )
        ttk.Label(parent, text="输出目录").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=self.output_dir_var, width=40).grid(
            row=3, column=1, sticky="w", pady=3
        )
        ttk.Checkbutton(parent, text="多章节并行发布", variable=self.parallel_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=2
        )
        ttk.Checkbutton(parent, text="详细调试日志", variable=self.verbose_var).grid(
            row=4, column=1, columnspan=3, sticky="w", pady=2
        )

    def _build_compress_page(self, parent: tk.Widget) -> None:
        body = parent
        body.columnconfigure(0, weight=1)
        settings = ttk.LabelFrame(
            body, text="图片压缩（> 上限自动压缩，默认 10MB/张）", padding=8
        )
        settings.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 4))
        self._build_compress_ui(settings)

        advanced = ttk.LabelFrame(body, text="通用（超时/重试/输出目录等）", padding=8)
        advanced.grid(row=1, column=0, sticky="nsew", padx=6, pady=(4, 6))
        self._build_common_ui(advanced)
        body.rowconfigure(1, weight=1)

    # ---- 发布与日志页 ----

    def _build_publish_tab(self) -> None:
        body = self.tab_publish
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        buttons = ttk.Frame(body)
        buttons.grid(row=0, column=0, sticky="ew", pady=(6, 4))
        self.publish_btn = ttk.Button(
            buttons, text="检查全部登录", command=self._check_all
        )
        self.publish_btn.pack(side="left", padx=2)
        ttk.Button(buttons, text="预览计划", command=self._preview_plan).pack(side="left", padx=2)
        ttk.Button(buttons, text="全文预览", command=self._preview_full).pack(side="left", padx=2)
        ttk.Button(buttons, text="保存配置", command=self._save_config).pack(side="left", padx=2)
        self.run_btn = ttk.Button(
            buttons, text="一键发布", command=self._publish, style="Accent.TButton"
        )
        self.run_btn.pack(side="right", padx=2)
        ttk.Label(buttons, text="发布目标 = 账号页勾选“启用”且已填 Cookie 的平台", foreground="#666").pack(
            side="right", padx=8
        )

        logframe = ttk.LabelFrame(body, text="运行日志", padding=4)
        logframe.grid(row=1, column=0, sticky="nsew")
        logframe.rowconfigure(0, weight=1)
        logframe.columnconfigure(0, weight=1)
        self.log_text = tk.Text(logframe, state="disabled", wrap="word", font=("Consolas", 9))
        vsb = ttk.Scrollbar(logframe, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        status = ttk.Frame(body)
        status.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.status_var = tk.StringVar(value="空闲")
        ttk.Label(status, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True
        )

    # ---------- 数据同步 ----------

    def _sync_common_ui(self) -> None:
        """把当前配置写回 GUI 输入框（通用字段已用构造初值，此处保留）。"""
        if self.verbose_var.get():
            logging.getLogger(LOGGER_NAME).setLevel(logging.DEBUG)

    def _build_app(self) -> AppConfig:
        try:
            timeout = float(self.timeout_var.get())
            retries = max(0, int(float(self.retries_var.get())))
            interval = max(0.0, float(self.interval_var.get()))
            max_width = max(0, int(float(self.max_width_var.get())))
            max_height = max(0, int(float(self.max_height_var.get())))
            quality = min(95, max(10, int(float(self.quality_var.get()))))
            max_mb = max(0.0, float(self.max_mb_var.get()))
        except ValueError as exc:
            raise ConfigError(f"压缩/通用数值填写有误：{exc}") from exc

        common = CommonConfig(
            timeout=timeout,
            retries=retries,
            interval_seconds=interval,
            max_width=max_width,
            max_height=max_height,
            quality=quality,
            max_bytes_mb=max_mb,
            output_dir=self.output_dir_var.get().strip() or "output",
            confirm=False,
            parallel=self.parallel_var.get(),
            verbose=self.verbose_var.get(),
            proxy_url=self.proxy_url_var.get().strip(),
            use_system_proxy=self.use_system_proxy_var.get(),
        )
        platforms: dict[str, PlatformConfig] = {}
        for card in PLATFORM_CARDS:
            key = card["key"]
            old = self.app.platforms.get(key)
            cookies = {
                name: var.get().strip()
                for name, var in self.cookie_vars.get(key, {}).items()
                if var.get().strip()
            }
            settings = copy.deepcopy(old.settings if old else DEFAULT_SETTINGS.get(key, {}))
            for extra_key, var in self.extra_vars.get(key, {}).items():
                value = str(var.get())
                if extra_key == "cate":
                    value = value.split(" - ")[0].strip()
                settings[extra_key] = value
            if key in self.field_maps and self.field_maps[key]:
                settings["field_map"] = copy.deepcopy(self.field_maps[key])
            platforms[key] = PlatformConfig(
                name=key,
                enabled=self.enabled_vars[key].get(),
                cookies=cookies,
                settings=settings,
            )
        app = AppConfig(common=common, platforms=platforms, path=self.config_path)
        return app

    def _enabled_with_cookie(self) -> list[str]:
        app = self._build_app()
        result = []
        for key, cfg in app.platforms.items():
            if key not in PLATFORM_CLASSES or not cfg.enabled:
                continue
            missing = [name for name in REQUIRED_COOKIES.get(key, []) if not cfg.cookies.get(name)]
            if missing:
                self._log(f"跳过 {key}：缺少 {', '.join(missing)}")
                continue
            result.append(key)
        return result

    # ---------- 动作 ----------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.run_btn.configure(state=state)
        self.publish_btn.configure(state=state)

    def _check_one(self, key: str) -> None:
        self._run_async(
            lambda: self._check_targets([key]),
            on_done=lambda _r: self.status_vars[key].set("检查完成，见日志"),
        )

    def _check_all(self) -> None:
        self._run_async(lambda: self._check_targets(None))

    def _check_targets(self, names: list[str] | None) -> None:
        try:
            app = self._build_app()
        except ConfigError as exc:
            self._warn(str(exc))
            return
        runner = Runner(app)
        try:
            results = runner.check(names)
        except Exception as exc:
            self._log(f"检查失败：{exc}")
            return
        for result in results:
            mark = "✓" if result.ok else "✗"
            self._log(f"{mark} {result.platform}: {result.message}")
            if result.platform in self.status_vars:
                self.status_vars[result.platform].set(
                    ("✓ " if result.ok else "✗ ") + result.message
                )

    def _preview_plan(self) -> None:
        comic_dir = self.comic_dir_var.get().strip()
        if not comic_dir:
            self._warn("请先选择漫画目录")
            return
        try:
            app = self._build_app()
            runner = Runner(app)
            names = self._enabled_with_cookie()
            if not names:
                self._warn("没有启用的平台或都未填 Cookie")
                return
            only = self._selected_chapter_keys()
            plan = runner.build_plan(comic_dir, names=names, only_chapters=only)
        except Exception as exc:
            self._log(f"生成计划失败：{exc}")
            return
        self._log("=" * 60)
        self._log("【发布计划预览】")
        for chapter, steps in plan:
            self._log(f"■ {chapter.title}（{chapter.key}，{len(chapter.pages)} 页）")
            for name, rows in steps:
                self._log(f"  ● {name}")
                for row in rows:
                    self._log(f"      - {row}")
        self._log("=" * 60)

    def _preview_full(self) -> None:
        comic_dir = self.comic_dir_var.get().strip()
        if not comic_dir:
            self._warn("请先选择/上传漫画")
            return

        def _task() -> Any:
            app = self._build_app()
            names = self._enabled_with_cookie()
            if not names:
                raise RuntimeError("没有启用的平台或都未填 Cookie")
            only = self._selected_chapter_keys()
            runner = Runner(app)
            return runner.build_full_preview(
                comic_dir, names=names, only_chapters=only
            )

        def _done(result: Any) -> None:
            text = format_full_preview(result)
            self._show_full_preview_window(text)
            chapters = len(result)
            self._log(f"全文预览完成（{chapters} 个章节，仅本地处理未发布）")

        self._run_async(_task, on_done=_done)

    def _show_full_preview_window(self, text: str) -> None:
        win = tk.Toplevel(self.root)
        win.title("发布前全文预览（不联网）")
        win.geometry("1080x760")
        win.transient(self.root)
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        box = tk.Text(frame, wrap="none", font=("Consolas", 9))
        vsb = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=box.xview)
        box.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        box.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        box.insert("1.0", text)
        box.configure(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=6)

    def _publish(self) -> None:
        comic_dir = self.comic_dir_var.get().strip()
        if not comic_dir:
            self._warn("请先选择漫画目录并点击“加载章节”")
            return
        if not self.chapters:
            self._warn("当前目录没有章节，请先点击“加载章节”")
            return
        self._run_async(lambda: self._publish_worker(comic_dir))

    def _publish_worker(self, comic_dir: str) -> None:
        try:
            app = self._build_app()
        except ConfigError as exc:
            self._warn(str(exc))
            return
        names = self._enabled_with_cookie()
        if not names:
            self._warn("没有启用的平台或都未填 Cookie")
            return
        only = self._selected_chapter_keys()
        runner = Runner(app)
        self._log("=" * 60)
        self._log(f"开始发布到：{', '.join(names)}（章节 {only or '全部'}）")
        started = time.time()
        try:
            results = runner.run_publish(
                comic_dir,
                names=names,
                only_chapters=only,
                dry_run=False,
                confirm=False,
            )
        except Exception as exc:
            self._log(f"发布中止：{exc}")
            return
        ok = sum(1 for r in results if r.status == "ok")
        partial = sum(1 for r in results if r.status == "partial")
        failed = sum(1 for r in results if r.status == "failed")
        self._log(
            f"完成：成功 {ok}，部分成功 {partial}，失败 {failed}，"
            f"耗时 {time.time() - started:.0f} 秒"
        )
        for result in results:
            url = f" {result.url}" if result.url else ""
            self._log(f"  [{result.status}] {result.platform} {result.title}: {result.message}{url}")

    def _selected_chapter_keys(self) -> list[str] | None:
        if self.chapter_list is None:
            return None
        sel = self.chapter_list.curselection()
        if not sel:
            return None
        keys = [self.chapter_list.get(i) for i in sel]
        return [k.split("  |  ", 1)[0] for k in keys]

    def _run_async(self, task: Callable[[], Any], on_done: Callable[[Any], None] | None = None) -> None:
        if self._busy:
            self._warn("已有任务在运行")
            return
        self._set_busy(True)
        self.status_var.set("运行中…")

        def _worker() -> None:
            result = None
            error: Optional[str] = None
            try:
                result = task()
            except Exception as exc:  # noqa: BLE001 - GUI 兜底
                error = str(exc)
                self._log(f"任务异常：{error}")
            finally:
                def _done() -> None:
                    self._set_busy(False)
                    self.status_var.set("空闲")
                    if error:
                        self._warn(f"运行出错：{error}")
                    elif on_done:
                        try:
                            on_done(result)
                        except Exception:  # pragma: no cover
                            pass

                try:
                    self.root.after(0, _done)
                except tk.TclError:  # pragma: no cover
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    # ---------- 漫画目录 ----------

    def _pick_comic_dir(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title="选择漫画目录")
        if path:
            self.comic_dir_var.set(path)
            self._load_comic()

    # ---- 上传漫画：文件夹 / 压缩包 / 图片 ----

    def _import_comic(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("上传漫画")
        win.geometry("420x190")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(
            win,
            text="选择漫画来源：\n\n"
            "• 文件夹：多话目录（子目录=各话）或整目录图片\n"
            "• 压缩包：ZIP / CBZ（RAR、7z 请先解压）\n"
            "• 图片：多选后按文件名排序，作为单本导入",
            wraplength=390,
            justify="left",
        ).pack(padx=14, pady=(12, 8))
        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=10, pady=4)

        def _pick_folder() -> None:
            win.grab_release()
            win.destroy()
            path = filedialog.askdirectory(
                parent=self.root, title="选择漫画文件夹（子目录=各话，或直接放图片）"
            )
            if path:
                self._accept_import_source(Path(path))

        def _pick_archive() -> None:
            win.grab_release()
            win.destroy()
            path = filedialog.askopenfilename(
                parent=self.root,
                title="选择漫画压缩包（ZIP/CBZ）",
                filetypes=[("漫画压缩包", "*.zip *.cbz"), ("所有文件", "*.*")],
            )
            if path:
                self._accept_import_archive(Path(path))

        def _pick_images() -> None:
            win.grab_release()
            win.destroy()
            paths = filedialog.askopenfilenames(
                parent=self.root,
                title="选择本话图片（可多选，按文件名排序）",
                filetypes=[("图片", "*.jpg *.jpeg *.png *.gif *.webp"), ("所有文件", "*.*")],
            )
            if paths:
                self._accept_import_images([Path(p) for p in paths])

        ttk.Button(buttons, text="选择文件夹", command=_pick_folder).pack(
            side="left", fill="x", expand=True, padx=4
        )
        ttk.Button(buttons, text="ZIP / CBZ", command=_pick_archive).pack(
            side="left", fill="x", expand=True, padx=4
        )
        ttk.Button(buttons, text="选择图片", command=_pick_images).pack(
            side="left", fill="x", expand=True, padx=4
        )
        ttk.Button(buttons, text="取消", command=win.destroy).pack(
            side="left", fill="x", expand=True, padx=4
        )

    def _import_staging_base(self) -> Path:
        base = Path(tempfile.gettempdir()) / "mangaupload_imports"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _accept_import_source(self, source: Path) -> None:
        """文件夹来源：完整漫画直接用；单本图片则拷到临时目录并补 manga.json。"""
        source = source.expanduser().resolve()
        if not source.is_dir():
            self._warn(f"文件夹不存在：{source}")
            return
        if self._looks_like_full_comic(source):
            self.comic_dir_var.set(str(source))
            self.import_hint_var.set("已直接使用原目录")
            self._load_comic()
            self._log(f"已导入漫画目录（原目录直接使用）：{source}")
            return
        # 只有一个目录套一层漫画内容的常见压缩结构：解开后再判断
        unwrapped = self._unwrap_single_dir(source)
        if unwrapped != source and self._looks_like_full_comic(unwrapped):
            self._accept_import_source(unwrapped)
            return
        # 纯图片单本：复制进导入缓存，避免改动用户原目录
        images = [p for p in source.iterdir() if p.is_file() and is_image(p)]
        images = sorted(images, key=natural_sort_key)
        if not images:
            self._warn(
                f"该目录里没有找到漫画图片：{source}\n"
                "需要 .jpg/.png/.gif/.webp；多话漫画请选根目录（子目录=各话）。"
            )
            return
        meta = self._ask_quick_meta(default_title=source.name)
        if meta is None:
            self._log("已取消导入")
            return
        staged = self._stage_images(images, title_hint=source.name)
        self._write_quick_meta(staged, meta)
        self.comic_dir_var.set(str(staged))
        self.import_hint_var.set("单本已复制到导入缓存")
        self._load_comic()
        self._log(
            f"已导入单本漫画：{source.name} → {staged}（复制 {len(images)} 张）"
        )

    def _accept_import_archive(self, archive: Path) -> None:
        archive = archive.expanduser().resolve()
        if not archive.is_file():
            self._warn(f"压缩包不存在：{archive}")
            return
        suffix = archive.suffix.lower()
        if suffix not in (".zip", ".cbz"):
            self._warn(
                f"暂不支持 {suffix or '未知'} 压缩包。\n"
                "请先用解压软件把 RAR / 7z 解压成文件夹，再选“选择文件夹”。"
            )
            return
        dest = self._import_staging_base() / (
            f"{archive.stem}_{int(time.time())}"
        )
        try:
            extracted = self._extract_zip(archive, dest)
        except Exception as exc:
            self._warn(f"解压失败：{exc}")
            return
        self._log(f"已解压：{archive.name} → {extracted}")
        self._accept_import_source(extracted)

    def _accept_import_images(self, images: list[Path]) -> None:
        images = sorted((p for p in images if is_image(p)), key=natural_sort_key)
        if not images:
            self._warn("没有选择任何图片")
            return
        name_hint = Path(images[0]).parent.name
        meta = self._ask_quick_meta(default_title=name_hint)
        if meta is None:
            return
        staged = self._stage_images(images, title_hint=name_hint)
        self._write_quick_meta(staged, meta)
        self.comic_dir_var.set(str(staged))
        self.import_hint_var.set("单本已复制到导入缓存")
        self._load_comic()
        self._log(f"已导入 {len(images)} 张图片 → {staged}")

    @staticmethod
    def _looks_like_full_comic(folder: Path) -> bool:
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

    @staticmethod
    def _unwrap_single_dir(folder: Path) -> Path:
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

    @staticmethod
    def _extract_zip(archive: Path, dest: Path) -> Path:
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

    @staticmethod
    def _stage_images(images: list[Path], title_hint: str = "") -> Path:
        base = Path(tempfile.gettempdir()) / "mangaupload_imports"
        base.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^\w\u4e00-\u9fff .()-]+', "_", str(title_hint or "comic"))
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

    @staticmethod
    def _write_quick_meta(folder: Path, meta: dict[str, str]) -> None:
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

    def _ask_quick_meta(
        self, default_title: str, default_author: str = "", default_desc: str = ""
    ) -> dict[str, str] | None:
        """单本导入时弹窗填标题/作者/简介；返回 None 表示取消。"""
        win = tk.Toplevel(self.root)
        win.title("单本漫画信息")
        win.geometry("560x330")
        win.transient(self.root)
        result: dict[str, str] = {}
        title_var = tk.StringVar(value=default_title)
        author_var = tk.StringVar(value=default_author)
        body = ttk.Frame(win, padding=10)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="该来源没有 manga.json，会复制到导入缓存后发布。").pack(
            anchor="w"
        )
        ttk.Label(body, text="标题（会同时作为作品名与章节名）：").pack(
            anchor="w", pady=(8, 2)
        )
        ttk.Entry(body, textvariable=title_var).pack(fill="x")
        ttk.Label(body, text="作者（可留空）：").pack(anchor="w", pady=(8, 2))
        ttk.Entry(body, textvariable=author_var).pack(fill="x")
        ttk.Label(body, text="简介：").pack(anchor="w", pady=(8, 2))
        desc_text = tk.Text(body, height=6, wrap="word")
        desc_text.insert("1.0", default_desc)
        desc_text.pack(fill="both", expand=True)

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=10, pady=8)

        def _ok() -> None:
            result["title"] = title_var.get().strip()
            result["author"] = author_var.get().strip()
            result["description"] = desc_text.get("1.0", "end").strip()
            if not result["title"]:
                messagebox.showwarning("提示", "标题不能为空", parent=win)
                return
            win.destroy()

        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="确定导入", command=_ok).pack(side="right", padx=4)
        win.grab_set()
        win.wait_window()
        return result or None

    def _load_comic(self, *, reload_ui: bool = False) -> None:
        path = self.comic_dir_var.get().strip()
        if not path:
            return
        try:
            chapters = load_chapters(path, strict=False)
        except Exception as exc:
            self._warn(f"加载失败：{exc}")
            return
        self.chapters = chapters
        if self.chapter_list is not None:
            self.chapter_list.delete(0, "end")
            for chapter in chapters:
                pages = len(chapter.pages)
                size = human_size(sum(p.stat().st_size for p in chapter.pages))
                self.chapter_list.insert("end", f"{chapter.key}  |  {chapter.title}  |  {pages} 页 / {size}")
        if chapters:
            first = chapters[0]
            over = [
                p
                for chapter in chapters
                for p in chapter.pages
                if p.stat().st_size > 10 * 1024 * 1024
            ]
            note = (
                f"\n⚠ 发现 {len(over)} 张超过 10MB 的图片，发布时会自动压缩。"
                if over
                else "\n所有图片均在 10MB 内，无需压缩。"
            )
            self.meta_var.set(
                f"系列：{first.raw.get('series_title') or first.raw.get('title') or path}　"
                f"作者：{first.raw.get('author') or '未填'}　"
                f"章节数：{len(chapters)}{note}"
            )
            self._log(f"已加载 {len(chapters)} 个章节：{path}")
            self._load_meta_ui(first)
            if self.chapter_list is not None and self.chapter_list.size():
                self.chapter_list.selection_clear(0, "end")
            if reload_ui:
                self._load_meta_ui(first)

    # ---------- B站扫码登录（可选） ----------

    def _bilibili_qr_login(self) -> None:
        try:
            import qrcode  # noqa: F401
        except ImportError:
            self._warn(
                "扫码登录需要 qrcode 库：\n\npip install qrcode[pil]\n\n"
                "安装后重试；或者直接在浏览器登录后粘贴 Cookie。"
            )
            return
        win = tk.Toplevel(self.root)
        win.title("B站扫码登录")
        win.geometry("300x360")
        ttk.Label(win, text="打开 B站手机 App 扫码").pack(pady=6)
        img_label = ttk.Label(win)
        img_label.pack()
        status = ttk.Label(win, text="正在获取二维码…")
        status.pack(pady=6)
        ttk.Button(win, text="取消", command=win.destroy).pack()
        state = {"stop": False}
        win.protocol(
            "WM_DELETE_WINDOW",
            lambda: (state.update(stop=True), win.destroy()),
        )

        def _worker() -> None:
            from PIL import Image, ImageTk
            import requests

            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "Chrome/126.0 Safari/537.36"
                    ),
                    "Referer": "https://passport.bilibili.com/login",
                }
            )
            try:
                data = session.get(
                    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                    timeout=15,
                ).json()
                qr_url = str(data["data"]["url"])
                key = str(data["data"]["qrcode_key"])
            except Exception as exc:
                self.root.after(0, lambda: status.configure(text=f"获取失败：{exc}"))
                return
            def _set_qr_image() -> None:
                img = qrcode.make(qr_url)
                photo = ImageTk.PhotoImage(img.resize((240, 240)))
                img_label.image = photo  # type: ignore[attr-defined]
                img_label.configure(image=photo)
                status.configure(text="等待扫码…")

            self.root.after(0, _set_qr_image)

            last_state = ""
            while not state["stop"]:
                time.sleep(2)
                try:
                    poll = session.get(
                        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                        params={"qrcode_key": key},
                        timeout=15,
                    ).json()
                except Exception as exc:
                    self.root.after(0, lambda e=exc: status.configure(text=f"轮询失败：{e}"))
                    continue
                code = poll.get("code")
                if code == 0:
                    cookies = {name: c.value for name, c in session.cookies.items()}
                    wanted = ["SESSDATA", "bili_jct", "buvid3", "DedeUserID"]
                    picked = {k: v for k, v in cookies.items() if k in wanted}
                    if not picked:
                        picked = cookies

                    def _apply(cookie_map: dict[str, str]) -> None:
                        filled = self._fill_cookie_vars("bilibili", cookie_map)
                        status.configure(text=f"登录成功，已填 {len(filled)} 个字段")

                    self.root.after(0, lambda: _apply(picked))
                    self.root.after(
                        0,
                        lambda: self._log(
                            f"B站扫码登录成功（UID {cookies.get('DedeUserID', '?')}）"
                        ),
                    )
                    self.root.after(800, win.destroy)
                    break
                if code == 86038:
                    self.root.after(0, lambda: status.configure(text="二维码失效，请重试"))
                    break
                if code == 86090 and last_state != "confirmed":
                    last_state = "confirmed"
                    self.root.after(0, lambda: status.configure(text="已扫码，请在手机上确认"))
                elif code == 86101 and last_state != "wait":
                    last_state = "wait"
                    self.root.after(0, lambda: status.configure(text="等待扫码…"))

        threading.Thread(target=_worker, daemon=True).start()

    # ---------- 配置保存 ----------

    def _save_config(self) -> None:
        try:
            app = self._build_app()
        except ConfigError as exc:
            self._warn(str(exc))
            return
        if self.config_path is None:
            default = Path("config.yaml")
            answer = messagebox.askyesno(
                "保存配置",
                f"没有找到 config.yaml，是否在当前目录创建？\n{default.resolve()}",
                parent=self.root,
            )
            if not answer:
                return
            self.config_path = default

        try:
            import yaml
        except ImportError as exc:
            self._warn("保存配置需要 PyYAML：pip install pyyaml")
            return

        # 保留已有文件里我们 GUI 不编辑的内容（如各平台 settings 深层配置）
        raw: dict[str, Any] = {}
        if self.config_path.exists():
            try:
                raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("common", {})
        raw.setdefault("platforms", {})
        raw["common"] = {
            "timeout": app.common.timeout,
            "retries": app.common.retries,
            "interval_seconds": app.common.interval_seconds,
            "max_width": app.common.max_width,
            "max_height": app.common.max_height,
            "quality": app.common.quality,
            "max_bytes_mb": app.common.max_bytes_mb,
            "output_dir": app.common.output_dir,
            "parallel": app.common.parallel,
            "verbose": app.common.verbose,
            "proxy_url": app.common.proxy_url,
            "use_system_proxy": app.common.use_system_proxy,
        }
        for key, cfg in app.platforms.items():
            item = raw["platforms"].get(key)
            if not isinstance(item, dict):
                item = {}
            item["enabled"] = cfg.enabled
            item["cookies"] = cfg.cookies
            item.setdefault("settings", {})
            if isinstance(item["settings"], dict):
                item["settings"].update(
                    {
                        k: v
                        for k, v in cfg.settings.items()
                        if k
                        in (
                            "forum",
                            "cate",
                            "work_name",
                            "chapter_name",
                            "category_label",
                            "language_label",
                            "langtype",
                            "title_jpn",
                            "publish_after_upload",
                            "field_map",
                        )
                    }
                )
            raw["platforms"][key] = item
        try:
            self.config_path.write_text(
                yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as exc:
            self._warn(f"写入失败：{exc}")
            return
        self._log(f"已保存配置：{self.config_path}")

    def _on_close(self) -> None:
        self.root.destroy()


def run_gui(config_path: str | None = None) -> int:
    root = tk.Tk()
    try:
        app = UploaderApp(root, config_path=config_path)
    except Exception as exc:  # GUI 启动失败也给出可读错误
        try:
            messagebox.showerror("启动失败", str(exc), parent=root)
        except Exception:  # pragma: no cover
            raise
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(run_gui())
