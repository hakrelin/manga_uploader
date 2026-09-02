"""数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class Chapter:
    """一个可发布单元：一话 / 一卷 / 一本，包含图片与元数据。"""

    key: str  # 目录名或 "root"
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    author: str = ""
    cover: Optional[Path] = None
    pages: list[Path] = field(default_factory=list)
    source_dir: Path = field(default_factory=Path)
    raw: dict[str, Any] = field(default_factory=dict)  # 合并后的原始元数据


@dataclass
class PreparedPage:
    """上传前处理好的页面文件。"""

    src: Path
    path: Path
    width: int = 0
    height: int = 0
    size_bytes: int = 0

    @property
    def size_kb(self) -> float:
        return self.size_bytes / 1024.0


@dataclass
class CheckResult:
    """登录/权限检查结果。"""

    platform: str
    ok: bool
    message: str


@dataclass
class PublishResult:
    """单个平台、单个章节的发布结果。"""

    platform: str
    chapter: str
    title: str
    status: str  # ok / partial / failed / skipped
    url: Optional[str] = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, platform: str, chapter: Chapter, url: str, message: str = "", **details) -> "PublishResult":
        return cls(
            platform=platform,
            chapter=chapter.key,
            title=chapter.title,
            status="ok",
            url=url,
            message=message,
            details=details,
        )

    @classmethod
    def partial(cls, platform: str, chapter: Chapter, url: str, message: str = "", **details) -> "PublishResult":
        return cls(
            platform=platform,
            chapter=chapter.key,
            title=chapter.title,
            status="partial",
            url=url,
            message=message,
            details=details,
        )

    @classmethod
    def failed(cls, platform: str, chapter: Chapter, message: str, **details) -> "PublishResult":
        return cls(
            platform=platform,
            chapter=chapter.key,
            title=chapter.title,
            status="failed",
            message=message,
            details=details,
        )

    @classmethod
    def skipped(cls, platform: str, chapter: Chapter, message: str = "") -> "PublishResult":
        return cls(
            platform=platform,
            chapter=chapter.key,
            title=chapter.title,
            status="skipped",
            message=message or "未启用或页面为空",
        )

