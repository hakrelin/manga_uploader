"""发布器抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import CommonConfig, PlatformConfig, missing_cookies
from ..http_client import HttpClient
from ..models import Chapter, CheckResult, PublishResult
from ..util import get_logger, prepare_page


class PublisherError(RuntimeError):
    pass


class BasePublisher(ABC):
    key: str = ""
    display_name: str = ""

    def __init__(self, cfg: PlatformConfig, common: CommonConfig, output_dir: Path | None = None):
        self.cfg = cfg
        self.common = common
        self.log = get_logger(self.key)
        self.output_dir = Path(output_dir) if output_dir else Path(common.output_dir)
        dump_dir = self.output_dir / "debug"
        self.http = HttpClient(
            cookies=cfg.cookies,
            timeout=common.timeout,
            retries=common.retries,
            dump_dir=dump_dir,
            log_prefix=self.key,
            proxy_url=common.proxy_url,
            use_system_proxy=common.use_system_proxy,
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
        """统一压缩/转换页面，返回 PreparedPage 列表（零拷贝优先）。"""
        if max_bytes is None:
            mb = float(self.common.max_bytes_mb or 0)
            max_bytes = int(mb * 1024 * 1024) if mb > 0 else 0
        out_dir = self.output_dir / "prepared" / self.key / chapter.key
        prepared = []
        for index, page in enumerate(chapter.pages, 1):
            self.log.info("[%s] 处理图片 %d/%d：%s", chapter.key, index, len(chapter.pages), page.name)
            try:
                item = prepare_page(
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

    def cleanup_prepared(self, chapter: Chapter) -> None:
        prepared_dir = self.output_dir / "prepared" / self.key / chapter.key
        if prepared_dir.is_dir():
            try:
                for f in prepared_dir.iterdir():
                    if f.is_file():
                        f.unlink()
            except OSError:  # pragma: no cover
                pass

    def summarize(self, chapter: Chapter) -> str:
        total_kb = sum(p.stat().st_size for p in chapter.pages) / 1024.0
        return f"{len(chapter.pages)} 页 / {total_kb:.1f} KB"

    # ---------- 子类实现 ----------

    @abstractmethod
    def check(self) -> CheckResult: ...

    @abstractmethod
    def plan(self, chapter: Chapter) -> list[str]:
        """dry-run 时展示将要做什么。"""

    @abstractmethod
    def publish(self, chapter: Chapter) -> PublishResult: ...
