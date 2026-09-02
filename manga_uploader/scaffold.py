"""生成示例漫画目录，方便用户理解结构与快速试跑。"""

from __future__ import annotations

import json
from pathlib import Path


SAMPLE_MANGA_JSON = {
    "title": "示例漫画",
    "author": "作者名",
    "description": "这里是漫画简介：讲了一个在多个平台同时连载的故事。",
    "tags": ["原创漫画", "示例"],
    "cover": "cover.png",
    "chapters": [
        {"folder": "ch01", "title": "示例漫画 第01话 开场", "description": "第一话：主角登场。"},
        {"folder": "ch02", "title": "示例漫画 第02话 转折", "description": "第二话：剧情推进。"},
    ],
    "platforms": {
        "bilibili": {
            "caption": "每日更新，欢迎三连关注～",
            "topics": ["原创漫画"],
            "image_category": "draw",
        },
        "tieba": {
            "forum": "请改成你的目标吧名",
        },
        "ehentai": {
            "category": "Manga",
            "language": "Chinese (Simplified)",
            "rating": "Safe",
            "extra_tags": ["parody:original"],
        },
    },
}


def _make_png(path: Path, size: tuple[int, int], rgb: tuple[int, int, int], text: str) -> None:
    """用 Pillow 生成占位页。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, rgb)
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, size[0] - 9, size[1] - 9), outline=(255, 255, 255), width=3)
    draw.text((24, size[1] // 2), text, fill=(255, 255, 255))
    image.save(path, "PNG")


def scaffold_comic(directory: Path, demo_images: bool = True) -> None:
    root = Path(directory).expanduser().resolve()
    (root / "ch01").mkdir(parents=True, exist_ok=True)
    (root / "ch02").mkdir(parents=True, exist_ok=True)
    meta_path = root / "manga.json"
    if not meta_path.exists():
        meta_path.write_text(json.dumps(SAMPLE_MANGA_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
    for chapter_dir in (root / "ch01", root / "ch02"):
        note = chapter_dir / "说明.txt"
        if not note.exists():
            note.write_text("把这一话的页面图片放进本目录：001.jpg、002.jpg……（支持 jpg/png/gif/webp，自动按文件名排序）\n", encoding="utf-8")

    if demo_images:
        for chapter_dir in (root / "ch01", root / "ch02"):
            note = chapter_dir / "说明.txt"
            if note.exists():
                note.unlink()
        colors = [(70, 100, 160), (90, 130, 90), (160, 110, 70), (120, 80, 130)]
        labels = ["封面页：示例漫画", "P1", "P2", "P3"]
        for index, (rgb, label) in enumerate(zip(colors, labels)):
            target = root / "cover.png" if index == 0 else root / "ch01" / f"00{index}.png"
            _make_png(target, (1200, 1600), rgb, label)
        for index in range(1, 4):
            _make_png(root / "ch02" / f"00{index}.png", (1200, 1600), colors[index - 1], f"第02话 P{index}")

    print(f"示例漫画已生成：{root}")
    print("结构：")
    for line in [
        f"  manga.json        漫画元数据（标题/简介/标签/各平台配置）",
        f"  cover.png         封面（可选）",
        f"  ch01/ 00*.png     第 1 话页面（图片会自动按文件名排序）",
        f"  ch02/ 00*.png     第 2 话页面",
    ]:
        print(line)
