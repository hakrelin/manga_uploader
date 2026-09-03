"""发布器抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
from pathlib import Path

from ..config import CommonConfig, PlatformConfig, missing_cookies
from ..http_client import HttpClient
from ..models import Chapter, CheckResult, PublishResult
from ..util import get_logger, prepare_page_cached


class PublisherError(RuntimeError):
    pass


class CaptchaRequiredError(PublisherError):
    """平台明确要求人机验证（验证码），需要用户手动处理后重试。"""


class BasePublisher(ABC):
    key: str = ""
    display_name: str = ""

    def __init__(self, cfg: PlatformConfig, common: CommonConfig, output_dir: Path | None = None):
        self.cfg = cfg
        self.common = common
        self.log = get_logger(self.key)
        self.output_dir = Path(output_dir) if output_dir else Path(common.output_dir)
        dump_dir = self.output_dir / "debug"
        # 平台级代理覆盖：config 里 platforms.<key>.settings 可单独指定
        # proxy_url / use_system_proxy，未配置时沿用 common 的全局设置
        proxy_url = self.cfg.get("proxy_url", common.proxy_url)
        use_system_proxy = bool(
            self.cfg.get("use_system_proxy", common.use_system_proxy)
        )
        self.http = HttpClient(
            cookies=cfg.cookies,
            timeout=common.timeout,
            retries=common.retries,
            dump_dir=dump_dir,
            log_prefix=self.key,
            proxy_url=proxy_url,
            use_system_proxy=use_system_proxy,
        )

    # ---------- 通用 ----------

    def missing_cookies(self) -> list[str]:
        return missing_cookies(self.cfg)

    def require_cookies(self) -> None:
        missing = self.missing_cookies()
        if missing:
            raise PublisherError(
                f"{self.display_name} 缺少 Cookie：{', '.join(missing)}，请填入 config.yaml 后重试"
            )

    def _meta(self, chapter: Chapter) -> dict:
        """平台专属元数据（manga.json 中 platforms.<key>）。"""
        from ..comic import platform_meta

        return platform_meta(chapter, self.key)

    def prepare_pages(
        self,
        chapter: Chapter,
        *,
        allowed_exts: set[str] | None = None,
        max_bytes: int | None = None,
    ) -> list:
        """统一压缩/转换页面，返回 PreparedPage 列表（零拷贝优先）。

        输出目录按“处理规格”分桶（不按平台分）：同一章节里多个平台使用
        相同规格（允许格式/单张上限/缩放/质量）时共享同一批结果文件，
        同一张图只解码压缩一次。cleanup_prepared 由调度器整批发布后调用。
        """
        if max_bytes is None:
            mb = float(self.common.max_bytes_mb or 0)
            max_bytes = int(mb * 1024 * 1024) if mb > 0 else 0
        spec = self._spec_key(allowed_exts, max_bytes)
        out_dir = self.output_dir / "prepared" / "_shared" / spec / chapter.key
        prepared = []
        for index, page in enumerate(chapter.pages, 1):
            try:
                item = prepare_page_cached(
                    page,
                    out_dir,
                    allowed_exts=allowed_exts,
                    max_width=self.common.max_width or 0,
                    max_height=self.common.max_height or 0,
                    quality=self.common.quality,
                    max_bytes=max_bytes,
                )
            except (ValueError, RuntimeError) as exc:
                raise PublisherError(str(exc)) from exc
            prepared.append(item)
        return prepared

    @staticmethod
    def _spec_key(allowed_exts: set[str] | None, max_bytes: int) -> str:
        """把图片处理参数编码成共享目录名（不同发布器同规格共用）。"""
        exts = ",".join(sorted(e.lower() for e in (allowed_exts or []))) or "any"
        raw = f"{exts}|{int(max_bytes or 0)}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return f"{digest}-{int(max_bytes or 0)}m"

    def cleanup_prepared(self, chapter: Chapter) -> None:
        """不再单独删除共享结果，交由 Runner 在整批发布后统一清理。

        多个平台共用同一批 prepared 文件时，任一平台提前删除会让后续
        平台重新处理甚至失败；若此时清空进程内缓存，后处理的平台还会
        失去去重命中。因此这里不做任何事（保留钩子供子类/单测调用），
        Runner 会在整批发布结束后统一清缓存并删除 prepared 目录。
        """
        return None

    def summarize(self, chapter: Chapter) -> str:
        total_kb = sum(p.stat().st_size for p in chapter.pages) / 1024.0
        return f"{len(chapter.pages)} 页 / {total_kb:.1f} KB"

    def full_preview(self, chapter: Chapter) -> list[str]:
        """发布前的“全文预览”：展示将提交的字段与页面顺序，不联网上传。

        子类可覆盖以展示各自真实的正文/HTML/表单内容。
        """
        lines = [
            f"发布平台：{self.display_name}",
            f"标题：{chapter.title}",
        ]
        if chapter.author:
            lines.append(f"作者：{chapter.author}")
        if chapter.description:
            desc = chapter.description
            lines.append("正文/简介文本：")
            for part in desc.splitlines() or [desc]:
                lines.append("  " + part)
        else:
            lines.append("（正文/简介为空）")
        tags = chapter.tags
        if tags:
            lines.append("标签：" + "、".join(str(t) for t in tags))
        self._append_page_preview(lines, chapter)
        return lines

    def _append_page_preview(self, lines: list[str], chapter: Chapter) -> None:
        from ..comic import page_sequence_warnings
        from ..util import human_size

        pages = chapter.pages
        lines.append(f"图片共 {len(pages)} 张，将按以下顺序上传：")
        for index, page in enumerate(pages, 1):
            lines.append(
                f"  [{index:>3}] {page.name}（{human_size(page.stat().st_size)}）"
            )
        warnings = page_sequence_warnings(pages)
        if warnings:
            lines.append("⚠ 检查发现：")
            for warning in warnings:
                lines.append("  - " + warning)
        else:
            lines.append("✓ 页面顺序连续，未发现重复或明显漏号")

    # ---------- 子类实现 ----------

    @abstractmethod
    def check(self) -> CheckResult: ...

    @abstractmethod
    def plan(self, chapter: Chapter) -> list[str]:
        """dry-run 时展示将要做什么。"""

    @abstractmethod
    def publish(self, chapter: Chapter) -> PublishResult: ...
