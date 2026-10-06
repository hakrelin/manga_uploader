"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .runner import Runner
from .util import ensure_utf8, setup_logging


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="manga_uploader",
        description="一键把漫画发布到 B站 / 贴吧 / e-hentai 等多平台",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default=None, help="config.yaml 路径（默认找当前目录）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--gui", action="store_true", help="启动图形界面（旧 tkinter，备用）")
    parser.add_argument("--web", action="store_true", help="启动浏览器前端（本地服务 + 自动拉起浏览器）")
    parser.add_argument("--port", type=int, default=None, help="Web 服务端口（默认 8970，被占自动后移）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1；如需局域网访问填 0.0.0.0）")
    parser.add_argument("--no-browser", action="store_true", help="启动 Web 服务但不自动打开浏览器")

    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="检查各平台登录状态")
    check.add_argument("--platform", default="", help="只检查指定平台，逗号分隔")

    publish = sub.add_parser("publish", help="发布漫画")
    publish.add_argument("comic", help="漫画目录（含 manga.json 与各话图片）")
    publish.add_argument("--platform", default="", help="指定平台，逗号分隔（默认全部已启用）")
    publish.add_argument("--chapter", action="append", default=None, help="只发布指定章节（可多次）")
    publish.add_argument("--dry-run", action="store_true", help="只打印计划，不联网不发布")
    publish.add_argument("--yes", action="store_true", help="跳过确认直接发布")
    publish.add_argument("--parallel", action="store_true", help="多个章节并行发布")

    scaffold = sub.add_parser("scaffold", help="生成漫画目录模板（含示例元数据与占位图）")
    scaffold.add_argument("path", help="要创建的漫画目录")
    scaffold.add_argument("--no-demo-images", action="store_true", help="不生成占位图片")

    login = sub.add_parser(
        "login", help="打开浏览器登录，自动把 Cookie 保存进 config.yaml"
    )
    login.add_argument(
        "platform", help="平台：bilibili / tieba / ehentai / zaimanhua / xiaoheihe"
    )
    login.add_argument("--timeout", type=float, default=600.0, help="等待登录的秒数（默认 600）")
    login.add_argument("--no-save", action="store_true", help="只打印 Cookie，不写入 config.yaml")
    login.add_argument("--headless", action="store_true", help="不显示浏览器窗口（调试用）")

    return parser.parse_args(argv)


def _split_names(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _cmd_check(args: argparse.Namespace, cfg_path: str | None) -> int:
    app = load_config(cfg_path)
    runner = Runner(app)
    results = runner.check(_split_names(args.platform))
    print("\n【平台登录检查】")
    ok = True
    for result in results:
        mark = "✓" if result.ok else "✗"
        print(f"  {mark} {result.platform}: {result.message}")
        ok = ok and result.ok
    return 0 if ok else 1


def _cmd_publish(args: argparse.Namespace, cfg_path: str | None) -> int:
    app = load_config(cfg_path, dry_run=args.dry_run, confirm=None if args.yes else True)
    if args.verbose:
        app.common.verbose = True
    if args.parallel:
        app.common.parallel = True
    runner = Runner(app)
    try:
        results = runner.run_publish(
            args.comic,
            names=_split_names(args.platform),
            only_chapters=list(args.chapter) if args.chapter else None,
            dry_run=args.dry_run,
            confirm=not args.yes,
        )
    except (ConfigError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        return 0
    return 0 if not any(r.status in ("failed", "partial") for r in results) else 1


def _cmd_scaffold(args: argparse.Namespace) -> int:
    from .scaffold import scaffold_comic

    try:
        scaffold_comic(Path(args.path), demo_images=not args.no_demo_images)
    except (OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_login(args: argparse.Namespace, cfg_path: str | None) -> int:
    """打开真实浏览器登录，登录完自动把 Cookie 写回 config.yaml。"""
    from . import browser_login
    from .webui import update_platform_cookies

    try:
        spec = browser_login.spec_for(args.platform)
    except browser_login.BrowserLoginError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    path = None
    try:
        path = load_config(cfg_path).path
    except ConfigError as exc:
        print(f"[提示] 读不到配置（{exc}），本次只打印 Cookie、不保存。", file=sys.stderr)

    print(f"【浏览器登录】{spec.label}")
    print(f"  将要打开：{spec.url}")
    if spec.note:
        print(f"  提示：{spec.note}")
    print("  请在浏览器窗口里登录；登录成功后本程序会自动读取 Cookie，请不要中途关掉窗口。")

    def on_status(message: str) -> None:
        print(f"  {message}", flush=True)

    try:
        cookies = browser_login.grab_cookies(
            args.platform,
            timeout=args.timeout,
            headless=args.headless,
            on_status=on_status,
        )
    except browser_login.BrowserLoginError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1

    print(f"✓ 已获取 Cookie：{', '.join(sorted(cookies)) or '（空）'}")
    if args.no_save or path is None:
        for name, value in cookies.items():
            print(f"  {name} = {value}")
        return 0
    try:
        saved = update_platform_cookies(path, args.platform, cookies)
    except Exception as exc:  # noqa: BLE001 - 保存失败也要把 Cookie 打印出来
        print(f"写入 config.yaml 失败：{exc}", file=sys.stderr)
        for name, value in cookies.items():
            print(f"  {name} = {value}")
        return 1
    print(f"✓ 已保存到 {saved}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8()
    args = _parse_args(argv)
    if args.gui:
        from .gui import run_gui

        return run_gui(config_path=args.config)
    if args.web:
        from .web import run_server

        run_server(
            port=args.port,
            open_browser=not args.no_browser,
            verbose=args.verbose,
            config_path=args.config,
            host=args.host,
        )
        return 0
    setup_logging(verbose=args.verbose or getattr(args, "dry_run", False))

    if not args.command:
        _parse_args(["--help"])
        return 2
    if args.command == "check":
        return _cmd_check(args, args.config)
    if args.command == "publish":
        return _cmd_publish(args, args.config)
    if args.command == "scaffold":
        return _cmd_scaffold(args)
    if args.command == "login":
        return _cmd_login(args, args.config)
    return 1


if __name__ == "__main__":
    sys.exit(main())
