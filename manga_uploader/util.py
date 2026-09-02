"""日志、图片处理、通用小工具。"""

from __future__ import annotations

import io
import logging
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

from .models import PreparedPage

LOGGER_NAME = "manga_uploader"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def ensure_utf8() -> None:
    """Windows 控制台默认 GBK，确保 UTF-8 输出不乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_logging(verbose: bool = False, log_file: Optional[Path] = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=handlers,
    )
    logging.getLogger(LOGGER_NAME).setLevel(level)
    # 抑制第三方库噪音
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def natural_sort_key(value: Path) -> list[object]:
    """按文件名中的数字自然排序：1, 2, ..., 10, 11。"""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value.stem)]


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def sort_images(paths: Iterable[Path]) -> list[Path]:
    return sorted((p for p in paths if is_image(p)), key=natural_sort_key)


def chunk_list(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def mask_secret(value: str) -> str:
    """只保留前 2 位与后 2 位的脱敏。"""
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} GB"


def _pil() -> "module":
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "需要 Pillow 处理/压缩图片：pip install Pillow"
        ) from exc
    return Image


def read_image_size(path: Path) -> tuple[int, int]:
    """读取图片宽高（不依赖格式扩展名）。"""
    Image = _pil()
    with Image.open(path) as img:
        return img.size


def _flatten(image: "Image.Image") -> "Image.Image":
    """透明图铺白底，否则转 JPEG 会出错。"""
    Image = _pil()
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _save_with_quality(image, dst: Path, quality: int) -> None:
    ext = dst.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        image.save(dst, "JPEG", quality=quality, optimize=True)
    elif ext == ".png":
        image.save(dst, "PNG", optimize=True)
    elif ext == ".gif":
        image.save(dst, "GIF")
    elif ext == ".webp":
        image.save(dst, "WEBP", quality=quality)
    else:
        image.save(dst)


def prepare_page(
    src: Path,
    out_dir: Path,
    *,
    allowed_exts: set[str] | None = None,
    max_width: int = 0,
    max_height: int = 0,
    quality: int = 88,
    max_bytes: int = 0,
) -> PreparedPage:
    """把单张图处理成可直接上传的文件。

    规则：
    - 保留扩展名在 allowed_exts 内且无需缩放、未超限时，直接返回原文件（零拷贝）；
    - 其他情况用 Pillow 重压缩/转换并输出到 out_dir；
    - 全程只做等比缩放/重压缩，不裁剪画面（透明图转 JPEG 时按原尺寸铺白底）；
    - allowed_exts 为空表示不限制格式。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    allowed = set(allowed_exts) if allowed_exts else IMAGE_EXTS

    Image = _pil()
    with Image.open(src) as img:
        try:
            img.load()
        except Exception as exc:  # 损坏文件
            raise ValueError(f"无法读取图片 {src.name}：{exc}") from exc

        width, height = img.size
        need_resize = bool(
            (max_width and width > max_width) or (max_height and height > max_height)
        )
        keep_ext = src.suffix.lower() in allowed
        size_bytes = src.stat().st_size
        over_limit = bool(max_bytes and size_bytes > max_bytes)

        if keep_ext and not need_resize and not over_limit:
            return PreparedPage(src=src, path=src, width=width, height=height, size_bytes=size_bytes)

        # 决定目标扩展名：优先保留原格式，其次 jpg -> png -> gif -> webp
        pref = [src.suffix.lower()] + [".jpg", ".png", ".gif", ".webp"]
        target_ext = next((ext for ext in pref if ext in allowed), ".jpg")

        if target_ext == ".gif" and src.suffix.lower() == ".gif" and not need_resize:
            # 动图无法安全重压缩，若仅超限则提示
            if over_limit:
                raise ValueError(f"动图 {src.name} 超出大小限制且无法无损压缩")
            return PreparedPage(src=src, path=src, width=width, height=height, size_bytes=size_bytes)

        if need_resize:
            img.thumbnail((max_width or width, max_height or height), Image.LANCZOS)
            width, height = img.size

        # 超过单张上限时优先转 JPEG，逐级降质量再缩小，确保能压进 10MB 等限制
        candidates = [target_ext]
        if target_ext in (".png", ".webp") and ".jpg" in allowed:
            candidates.append(".jpg")

        last_dst: Path | None = None
        last_size = size_bytes
        for fmt in candidates:
            dst = out_dir / f"{src.stem}{fmt}"
            last_dst = dst
            if fmt in (".jpg", ".jpeg", ".webp"):
                image = _flatten(img)
                current = quality
                while current >= 45:
                    _save_with_quality(image, dst, current)
                    last_size = dst.stat().st_size
                    if not max_bytes or last_size <= max_bytes:
                        break
                    current -= 10
                if max_bytes and last_size > max_bytes:
                    # 降质量还不够就缩小画布，最多尝试 8 轮
                    scaled = image.copy()
                    for _round in range(8):
                        if scaled.width <= 600 and scaled.height <= 600:
                            break
                        scaled = scaled.resize(
                            (
                                max(1, int(scaled.width * 0.82)),
                                max(1, int(scaled.height * 0.82)),
                            ),
                            Image.LANCZOS,
                        )
                        _save_with_quality(scaled, dst, max(current, 55))
                        last_size = dst.stat().st_size
                        if last_size <= max_bytes:
                            width, height = scaled.size
                            break
                if not max_bytes or last_size <= max_bytes:
                    break
            else:
                _save_with_quality(img, dst, quality)
                last_size = dst.stat().st_size
                if not max_bytes or last_size <= max_bytes:
                    break

        if last_dst is None or (max_bytes and last_size > max_bytes):
            name = last_dst.name if last_dst else src.name
            raise ValueError(
                f"{name} 压缩后仍为 {human_size(last_size)}，超过限制 {human_size(max_bytes)}"
            )

    return PreparedPage(
        src=src,
        path=last_dst,
        width=width,
        height=height,
        size_bytes=last_size,
    )
