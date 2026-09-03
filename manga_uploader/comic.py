"""扫描漫画目录、读取元数据、组装发布单元。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .models import Chapter
from .util import is_image, sort_images

META_FILES = ("manga.json", "manga.yaml", "manga.yml", "comic.json", "comic.yaml")
SKIP_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    "__pycache__",
    "output",
    "out",
    "prepared",
    "preview",
    "thumbnails",
}


class ComicError(RuntimeError):
    pass


def read_meta(path: Path) -> dict[str, Any]:
    """读取 manga.json / manga.yaml / comic.json 等元数据文件。"""
    if not path.exists():
        return {}
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ComicError(f"元数据 JSON 解析失败：{path}\n{exc}") from exc
    elif path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise ComicError("需要 PyYAML 才能读取 yaml 元数据：pip install pyyaml") from exc
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ComicError(f"元数据 YAML 解析失败：{path}\n{exc}") from exc
    else:
        return {}
    return data if isinstance(data, dict) else {}


def find_meta_file(directory: Path) -> Optional[Path]:
    for name in META_FILES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _image_files(folder: Path) -> list[Path]:
    return sort_images(folder.iterdir() if folder.is_dir() else [])


def _to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _clean_dir_name(text: str) -> str:
    """把 ch01 / 第01话 变成更友好的展示名。"""
    return text.replace("_", " ").replace("-", " ")


def load_chapters(
    comic_dir: str | Path,
    *,
    only_chapters: Optional[list[str]] = None,
    strict: bool = True,
) -> list[Chapter]:
    """扫描漫画目录，返回待发布章节。

    目录约定：
    - 根目录放 manga.json（标题/简介/标签/平台配置）；
    - 每个子目录一话，子目录内是排好序的图片；
    - 也可直接用一个全是图片的目录当作单本发布。
    """
    root = Path(comic_dir).expanduser().resolve()
    if not root.is_dir():
        raise ComicError(f"漫画目录不存在：{root}")

    root_meta = read_meta(find_meta_file(root)) if find_meta_file(root) else {}
    root_meta.setdefault("title", root.name)
    # 章节条目合并时会覆盖 title，单独保留系列名供“同一作品多话”使用
    root_meta.setdefault("series_title", str(root_meta.get("title") or root.name))

    direct_pages = _image_files(root)
    child_folders = sorted(
        (
            child
            for child in root.iterdir()
            if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith(".")
        ),
        key=lambda p: p.name.lower(),
    )
    chapter_folders = [c for c in child_folders if _image_files(c)]

    # manga.json 的 chapters 数组决定发布顺序，未列出的目录按名称排在后面
    listed_order: dict[str, int] = {}
    for index, item in enumerate(root_meta.get("chapters") or []):
        if isinstance(item, dict):
            key = str(item.get("folder") or item.get("key") or item.get("name") or index)
            listed_order.setdefault(key, index)
    chapter_folders.sort(key=lambda folder: (listed_order.get(folder.name, 10**9), folder.name.lower()))

    raw_units: list[tuple[str, Path, dict]] = []
    if chapter_folders:
        for folder in chapter_folders:
            raw_units.append((folder.name, folder, {}))
    elif direct_pages:
        raw_units.append(("root", root, {}))
    else:
        raise ComicError(
            f"目录里没有找到图片：{root}\n"
            "请放 .jpg/.png/.gif/.webp 图片，每个子目录代表一话。"
        )

    chapters_by_key: dict[str, dict] = {}
    for idx, item in enumerate(root_meta.get("chapters") or []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("folder") or item.get("key") or item.get("name") or idx)
        chapters_by_key[key] = item

    chapters: list[Chapter] = []
    for key, folder, _ in raw_units:
        folder_meta_file = find_meta_file(folder)
        folder_meta = read_meta(folder_meta_file) if folder_meta_file else {}
        listed_meta = chapters_by_key.get(key, {})
        merged = _deep_merge(root_meta, listed_meta)
        merged = _deep_merge(merged, folder_meta)

        if only_chapters and key not in only_chapters and folder.name not in only_chapters:
            continue

        pages = _image_files(folder)
        if not pages:
            if strict:
                raise ComicError(f"章节目录没有图片：{folder}")
            continue

        listed_entry = chapters_by_key.get(key, {})
        listed_title = listed_entry.get("title") if isinstance(listed_entry, dict) else None
        folder_title = folder_meta.get("title") if folder_meta else None
        explicit_title = str(listed_title or folder_title or "").strip()
        # 若无单独章节标题，用「系列名 + 目录名」
        if not explicit_title:
            series_title = str(root_meta.get("title") or root.name)
            if key == "root":
                # 整目录直接放图 = 单本，直接用目录名/系列名，避免出现“xx root”
                explicit_title = series_title
            else:
                explicit_title = f"{series_title} {_clean_dir_name(key)}".strip()

        cover_name = str(merged.get("cover") or "").strip()
        cover: Optional[Path] = None
        if cover_name:
            cover_candidate = (root / cover_name) if not (folder / cover_name).exists() else (folder / cover_name)
            if cover_candidate.is_file():
                cover = cover_candidate

        tags = _to_list(merged.get("tags"))
        chapters.append(
            Chapter(
                key=key,
                title=explicit_title,
                description=str(merged.get("description") or merged.get("long_description") or "").strip(),
                tags=tags,
                author=str(merged.get("author") or "").strip(),
                cover=cover or (pages[0] if pages else None),
                pages=pages,
                source_dir=folder,
                raw=merged,
            )
        )

    if only_chapters:
        found = {c.key for c in chapters}
        missing = [name for name in only_chapters if name not in found]
        if missing:
            raise ComicError(f"找不到指定章节：{', '.join(missing)}")
    if not chapters:
        raise ComicError(f"没有可发布的章节：{root}")
    return chapters


def platform_meta(chapter: Chapter, platform: str) -> dict[str, Any]:
    """取出 manga.json 中某平台的覆盖配置。"""
    platforms = chapter.raw.get("platforms")
    if isinstance(platforms, dict):
        item = platforms.get(platform)
        if isinstance(item, dict):
            return item
    return {}


def first_line(text: str) -> str:
    text = text.strip()
    return re.split(r"[\r\n]", text)[0] if text else ""


def page_sequence_warnings(pages: list[Path]) -> list[str]:
    """检查页面列表是否有重复页/明显漏号，防止“缺页”发布。"""
    warnings: list[str] = []
    stems: dict[str, list[str]] = {}
    for page in pages:
        stems.setdefault(page.stem.lower(), []).append(page.name)
    duplicates = [names for names in stems.values() if len(names) > 1]
    if duplicates:
        warnings.append(
            "发现同名文件（可能是重复页，会都上传）：" + "; ".join(", ".join(n) for n in duplicates)
        )

    numbers: list[int] = []
    for page in pages:
        match = re.match(r"^\s*(\d+)", page.stem)
        if match:
            numbers.append(int(match.group(1)))
    unique = sorted(set(numbers))
    if len(unique) >= 3 and unique[-1] - unique[0] + 1 != len(unique):
        missing = [
            str(n) for n in range(unique[0], unique[-1] + 1) if n not in set(unique)
        ]
        warnings.append(
            "文件名编号存在缺口，可能漏页：" + ", ".join(missing[:20])
            + (f" 等共 {len(missing)} 个" if len(missing) > 20 else "")
        )
    return warnings
