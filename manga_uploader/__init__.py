"""一键多平台漫画发布工具。"""

from __future__ import annotations

import re
from pathlib import Path

__version__ = "0.1.0"


def git_revision(root: Path | None = None) -> str:
    """当前代码的 Git 短提交号，取不到返回空串。"""
    base = Path(root) if root else Path(__file__).resolve().parent.parent
    git_dir = base / ".git"
    if not git_dir.is_dir():
        return ""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            sha = ""
            ref_file = git_dir / ref
            if ref_file.is_file():
                sha = ref_file.read_text(encoding="utf-8", errors="ignore").strip()
            else:  # 打包/浅克隆：提交号写在 packed-refs 里
                packed = git_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if line.endswith(" " + ref):
                            sha = line.split(" ", 1)[0].strip()
                            break
        else:
            sha = head
    except OSError:
        return ""
    return sha[:7] if re.fullmatch(r"[0-9a-fA-F]{7,40}", sha) else ""


def build_stamp(root: Path | None = None) -> str:
    """版本号 + 短提交号（如 0.1.0+68ddf36），用来核对是否最新版。"""
    rev = git_revision(root)
    return f"{__version__}+{rev}" if rev else __version__
