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
    parser.add_argument("--gui", action="store_true", help="启动图形界面")

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


def main(argv: list[str] | None = None) -> int:
    ensure_utf8()
    args = _parse_args(argv)
    if args.gui:
        from .gui import run_gui

        return run_gui(config_path=args.config)
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
