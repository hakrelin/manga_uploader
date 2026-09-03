"""发布调度：检查、计划、执行、汇总。"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .comic import load_chapters
from .config import AppConfig
from .models import Chapter, CheckResult, PublishResult
from .publishers.base import BasePublisher, PublisherError
from .publishers.bilibili import BilibiliPublisher
from .publishers.ehentai import EhentaiPublisher
from .publishers.tieba import TiebaPublisher
from .publishers.xiaoheihe import XiaoheihePublisher
from .publishers.zaimanhua import ZaimanhuaPublisher
from .util import get_logger, human_size

PLATFORM_CLASSES: dict[str, type[BasePublisher]] = {
    "bilibili": BilibiliPublisher,
    "tieba": TiebaPublisher,
    "ehentai": EhentaiPublisher,
    "xiaoheihe": XiaoheihePublisher,
    "zaimanhua": ZaimanhuaPublisher,
}


class Runner:
    def __init__(self, app: AppConfig):
        self.app = app
        self.log = get_logger("runner")

    def output_dir(self) -> Path:
        path = Path(self.app.common.output_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def make_publisher(self, name: str) -> BasePublisher:
        if name not in PLATFORM_CLASSES:
            raise PublisherError(f"未知平台：{name}，支持：{', '.join(PLATFORM_CLASSES)}")
        cfg = self.app.platforms.get(name)
        if cfg is None:
            raise PublisherError(f"配置里没有平台 {name}，请检查 config.yaml")
        return PLATFORM_CLASSES[name](cfg, self.app.common, output_dir=self.output_dir())

    def resolve_platforms(self, names: Optional[list[str]] = None) -> list[tuple[str, bool]]:
        """返回 (平台名, 是否启用)。空 names 时返回所有已启用平台。"""
        if names:
            result = []
            for name in names:
                name = name.strip().lower()
                if name not in PLATFORM_CLASSES:
                    raise PublisherError(f"未知平台：{name}，支持：{', '.join(PLATFORM_CLASSES)}")
                cfg = self.app.platforms.get(name)
                result.append((name, bool(cfg and cfg.enabled)))
            return result
        return [
            (name, bool(cfg and cfg.enabled))
            for name, cfg in self.app.platforms.items()
            if name in PLATFORM_CLASSES and cfg.enabled
        ]

    # ---------- 检查 ----------

    def check(self, names: Optional[list[str]] = None) -> list[CheckResult]:
        results = []
        for name, enabled in self.resolve_platforms(names):
            if not enabled:
                results.append(CheckResult(name, False, "未在 config.yaml 中启用"))
                continue
            try:
                publisher = self.make_publisher(name)
                results.append(publisher.check())
            except Exception as exc:
                results.append(CheckResult(name, False, f"检查过程出错：{exc}"))
        return results

    # ---------- 发布 ----------

    def load_chapters(self, comic_dir: str, only_chapters: Optional[list[str]] = None) -> list[Chapter]:
        return load_chapters(comic_dir, only_chapters=only_chapters)

    def build_plan(
        self, comic_dir: str, names: Optional[list[str]] = None, only_chapters: Optional[list[str]] = None
    ) -> list[tuple[Chapter, list[tuple[str, list[str]]]]]:
        """不联网的计划：每个章节每个平台的步骤说明。"""
        chapters = self.load_chapters(comic_dir, only_chapters=only_chapters)
        plan = []
        for chapter in chapters:
            steps = []
            for name, enabled in self.resolve_platforms(names):
                if not enabled:
                    continue
                try:
                    publisher = self.make_publisher(name)
                    steps.append((name, publisher.plan(chapter)))
                except PublisherError as exc:
                    steps.append((name, [f"配置不完整：{exc}"]))
            plan.append((chapter, steps))
        return plan

    def build_full_preview(
        self, comic_dir: str, names: Optional[list[str]] = None, only_chapters: Optional[list[str]] = None
    ) -> list[tuple[Chapter, list[tuple[str, list[str]]]]]:
        """不联网的全文预览：真实跑图片预处理并输出各平台将提交的内容。"""
        chapters = self.load_chapters(comic_dir, only_chapters=only_chapters)
        preview = []
        for chapter in chapters:
            rows = []
            for name, enabled in self.resolve_platforms(names):
                if not enabled:
                    continue
                try:
                    publisher = self.make_publisher(name)
                    rows.append((name, publisher.full_preview(chapter)))
                except PublisherError as exc:
                    rows.append((name, [f"配置不完整：{exc}"]))
            preview.append((chapter, rows))
        return preview

    def run_publish(
        self,
        comic_dir: str,
        *,
        names: Optional[list[str]] = None,
        only_chapters: Optional[list[str]] = None,
        dry_run: bool = False,
        confirm: bool = True,
    ) -> list[PublishResult]:
        chapters = self.load_chapters(comic_dir, only_chapters=only_chapters)
        platforms = self.resolve_platforms(names)
        enabled = [(n, e) for n, e in platforms if e]
        if not enabled:
            raise PublisherError("没有启用的平台，请在 config.yaml 中启用或指定 --platform")

        # dry-run：只打印计划
        if dry_run or self.app.common.dry_run:
            plan = []
            for chapter in chapters:
                rows = []
                for name, _ in enabled:
                    try:
                        publisher = self.make_publisher(name)
                        rows.append((name, publisher.plan(chapter)))
                    except PublisherError as exc:
                        rows.append((name, [f"配置不完整：{exc}"]))
                plan.append((chapter, rows))
            print(_format_plan(plan, dry_run=True))
            return []

        # 确认
        if confirm:
            plan = []
            for chapter in chapters:
                rows = []
                for name, _ in enabled:
                    try:
                        publisher = self.make_publisher(name)
                        rows.append((name, publisher.plan(chapter)))
                    except PublisherError as exc:
                        rows.append((name, [f"配置不完整：{exc}"]))
                plan.append((chapter, rows))
            print(_format_plan(plan, dry_run=False))
            if not _ask_yes_no(f"确认发布以上内容到 {len(enabled)} 个平台？"):
                print("已取消，未发布任何内容。")
                return []

        results: list[PublishResult] = []
        parallel = self.app.common.parallel
        if parallel and len(chapters) > 1:
            results = self._run_parallel(chapters, enabled)
        else:
            for chapter in chapters:
                for name, _ in enabled:
                    results.append(self._publish_one(name, chapter))
        self._write_report(results)
        _print_summary(results)
        return results

    def _publish_one(self, name: str, chapter: Chapter) -> PublishResult:
        publisher = self.make_publisher(name)
        try:
            started = time.time()
            result = publisher.publish(chapter)
            if result.status == "ok":
                self.log.info("[%s] %s 发布成功：%s（%.1fs）", chapter.key, name, result.url, time.time() - started)
            elif result.status == "partial":
                self.log.warning("[%s] %s 部分成功：%s", chapter.key, name, result.message)
            else:
                self.log.error("[%s] %s 发布失败：%s", chapter.key, name, result.message)
            return result
        except Exception as exc:
            self.log.exception("[%s] %s 未捕获异常", chapter.key, name)
            return PublishResult.failed(name, chapter, str(exc))

    def _run_parallel(self, chapters: list[Chapter], enabled: list[tuple[str, bool]]) -> list[PublishResult]:
        results: list[PublishResult] = []

        def task(chapter: Chapter) -> list[PublishResult]:
            return [self._publish_one(name, chapter) for name, _ in enabled]

        with ThreadPoolExecutor(max_workers=min(4, len(chapters))) as pool:
            futures = {pool.submit(task, chapter): chapter for chapter in chapters}
            for future in as_completed(futures):
                try:
                    results.extend(future.result())
                except Exception as exc:  # pragma: no cover
                    chapter = futures[future]
                    results.append(PublishResult.failed("unknown", chapter, str(exc)))
        return results

    def _write_report(self, results: list[PublishResult]) -> Path:
        payload = [
            {
                "platform": r.platform,
                "chapter": r.chapter,
                "title": r.title,
                "status": r.status,
                "url": r.url,
                "message": r.message,
                "details": r.details,
            }
            for r in results
        ]
        path = self.output_dir() / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def _ask_yes_no(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:  # pragma: no cover
        return False
    return answer in ("y", "yes", "是")


def _format_plan(plan: list[tuple[Chapter, list[tuple[str, list[str]]]]], *, dry_run: bool) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("【发布计划】" + ("（干跑模式，不会真正发布）" if dry_run else ""))
    lines.append("=" * 70)
    for chapter, steps in plan:
        size = sum(p.stat().st_size for p in chapter.pages)
        lines.append(f"\n■ 章节：{chapter.title}（{chapter.key}）")
        lines.append(f"  页面：{len(chapter.pages)} 张 / {human_size(size)}")
        if chapter.description:
            desc = chapter.description if len(chapter.description) <= 60 else chapter.description[:57] + "…"
            lines.append(f"  简介：{desc}")
        if not steps:
            lines.append("  无可用平台（均未启用或配置缺失）")
        for name, rows in steps:
            lines.append(f"  ● {name}")
            for row in rows:
                lines.append(f"      - {row}")
    return "\n".join(lines)


def _print_summary(results: list[PublishResult]) -> None:
    print("\n" + "=" * 70)
    print("【发布结果】")
    print("=" * 70)
    status_icon = {"ok": "✓", "partial": "◐", "failed": "✗", "skipped": "–"}
    for result in results:
        icon = status_icon.get(result.status, "?")
        url = f"  {result.url}" if result.url else ""
        print(f"  {icon} [{result.platform}] {result.title}: {result.message}{url}")
    ok_count = sum(1 for r in results if r.status == "ok")
    partial_count = sum(1 for r in results if r.status == "partial")
    failed_count = sum(1 for r in results if r.status == "failed")
    print(f"\n成功 {ok_count}，部分成功 {partial_count}，失败 {failed_count}，"
          f"跳过 {sum(1 for r in results if r.status == 'skipped')}")
