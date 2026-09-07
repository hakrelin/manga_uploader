"""本地覆盖更新器：把旧版本原地更新到 GitHub 最新版，保留本机配置与数据。

原则：
- 只替换仓库内受版本管理的文件；config.yaml、.venv、.tools、.git、output、
  local-*.ttf 等本机文件一律不覆盖、不删除；
- 首次更新（旧版没有 manifest）采用“只覆盖不删除”，后续更新按 manifest
  同步，清理已从新版移除的旧文件；
- 每次更新前自动备份被替换/删除的文件与 config.yaml，出错可回滚；
- 更新后自动把新版 requirements.txt 装进本机 .venv（失败不阻断，可由
  start.bat 首次启动时重建环境）。

用法（详见 README「本地覆盖更新」）：
    python update.py            # 一键更新到 GitHub 默认分支最新版
    python update.py --check    # 只检查远端是否有新版本
    python update.py --url <镜像zip地址>   # GitHub 连不上时用镜像
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional


DEFAULT_REPO = "https://github.com/hakrelin/manga_uploader"
DEFAULT_BRANCH = "main"
STATE_NAME = ".updater-state.json"
UA = "manga-uploader-updater/0.1 (+https://github.com/hakrelin/manga_uploader)"

# 永远不覆盖、不删除的路径（项目根相对路径；目录按首段匹配）
PROTECTED_DIRS = {
    ".git",
    ".venv",
    ".tools",
    "output",
    "__pycache__",
}
PROTECTED_FILES = {
    "config.yaml",
    "config.local.yaml",
    "config.profiles.yaml",
    "config.yml",
    STATE_NAME,
    "desktop.ini",
    "Thumbs.db",
}


class UpdaterError(RuntimeError):
    """更新过程中的可预期错误（提示信息面向终端用户）。"""


# ---------------------------------------------------------------- 小工具

def _console_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 非 Windows 或无 reconfigure 时忽略
        pass


def info(msg: str) -> None:
    print(msg)


def warn(msg: str) -> None:
    print(f"[警告] {msg}")


def fail(msg: str) -> None:
    print(f"[错误] {msg}")


def _is_protected(rel: PurePosixPath) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if parts[0].lower() in PROTECTED_DIRS:
        return True
    name = parts[-1].lower()
    # 本地商用字体（gitignored，不入仓库）即使误入 manifest 也绝不删除
    if (
        len(parts) >= 5
        and tuple(p.lower() for p in parts[:4])
        == ("manga_uploader", "web", "assets", "fonts")
        and name.startswith("local-")
        and name.endswith(".ttf")
    ):
        return True
    return name in PROTECTED_FILES


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    return url


def system_proxy() -> str:
    """读取系统代理：优先环境变量，其次 Windows 注册表（与 _common.ps1 一致）。"""
    for env_name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        value = os.environ.get(env_name) or os.environ.get(env_name.lower())
        url = _normalize_url(value or "")
        if url:
            return url
    if os.name == "nt":  # pragma: no cover - 仅 Windows
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            winreg.CloseKey(key)
            if enabled and server:
                # 形如 http=...;https=... 的按协议配置，取 https 段
                match = re.search(r"https?=([^;]+)", str(server), re.I)
                if match:
                    server = match.group(1)
                return _normalize_url(str(server))
        except OSError:
            pass
    return ""


def _opener(proxy: str = ""):
    handlers = []
    proxy = _normalize_url(proxy)
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # 不读环境变量里的意外代理
    return urllib.request.build_opener(*handlers)


def _http_get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _opener(system_proxy()).open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise UpdaterError(f"下载失败 HTTP {exc.code}：{url}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdaterError(
            f"网络错误：{url}\n原因：{reason}\n"
            "若开启了系统代理请确认代理可用；也可以改用 --url 指定镜像地址。"
        ) from exc


# ---------------------------------------------------------------- 版本与远端

def _repo_parts(repo_url: str) -> tuple[str, str]:
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
    if not m:
        raise UpdaterError(f"无法从仓库地址解析 owner/name：{repo_url}")
    return m.group(1), m.group(2).rstrip("/")


def remote_commit(repo_url: str, branch: str) -> str:
    """取 GitHub 远端某分支最新 commit（尽力而为，失败返回空串由调用方处理）。"""
    try:
        owner, name = _repo_parts(repo_url)
        url = f"https://api.github.com/repos/{owner}/{name}/commits/{branch}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
        )
        with _opener(system_proxy()).open(req, timeout=15.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return str((payload.get("sha") or ""))
    except Exception as exc:  # noqa: BLE001
        warn(f"获取远端版本号失败（{exc}），将跳过版本比对")
        return ""


def _state_path(root: Path) -> Path:
    return root / STATE_NAME


def load_state(root: Path) -> dict:
    path = _state_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_state(root: Path, commit: str = "", files: Optional[list[str]] = None) -> None:
    data = load_state(root)
    data.update(
        {
            "repo": DEFAULT_REPO,
            "branch": DEFAULT_BRANCH,
            "commit": commit,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    if files is not None:
        data["files"] = sorted(set(files))
    _state_path(root).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def check_update(root: Path, repo_url: str, branch: str) -> int:
    state = load_state(root)
    local = state.get("commit") or "未知（此前未记录）"
    info(f"本地版本：{local}")
    info(f"正在查询远端 {repo_url} 分支 {branch} …")
    remote = remote_commit(repo_url, branch)
    if not remote:
        fail("无法获取远端版本信息，请检查网络/代理，稍后重试")
        return 1
    short_local = str(local)[:10]
    short_remote = remote[:10]
    if local and short_local == short_remote:
        info(f"当前已是最新（{short_remote}），无需更新")
        return 0
    if local and str(local) != "未知（此前未记录）":
        info(f"发现新版本：{short_local} → {short_remote}")
    else:
        info(f"远端最新版本：{short_remote}")
    return 0


# ---------------------------------------------------------------- 下载与解压

def _archive_url(repo_url: str, branch: str) -> str:
    return f"{repo_url.rstrip('/')}/archive/refs/heads/{branch}.zip"


def download_zip(url: str, dest: Path, proxy: str = "") -> Path:
    info(f"下载 {url} …")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _opener(proxy).open(req, timeout=60.0) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh, length=1024 * 256)
    except urllib.error.HTTPError as exc:
        raise UpdaterError(
            f"下载失败 HTTP {exc.code}：{url}\n"
            "若分支/地址有误请检查；GitHub 连不上时可加 --url 指定镜像。"
        ) from exc
    except urllib.error.URLError as exc:
        raise UpdaterError(f"网络错误：{url}\n原因：{getattr(exc, 'reason', exc)}") from exc
    if not dest.is_file() or dest.stat().st_size < 50 * 1024:
        raise UpdaterError("下载内容异常（文件过小），可能拿到了错误页面，请重试")
    info(f"下载完成：{dest.stat().st_size / 1024 / 1024:.1f} MB")
    return dest


def _extract_zip(zip_path: Path, dest: Path) -> Path:
    """解压 GitHub 归档，去掉外层顶层目录，目标文件直接落在 dest 下。"""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            raise UpdaterError("压缩包内没有文件")
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1:
            raise UpdaterError(f"压缩包结构异常（顶层目录 {sorted(tops)}），请换 --url 重试")
        prefix = f"{next(iter(tops))}/"
        for member in names:
            if not member.startswith(prefix):
                continue
            rel = member[len(prefix):]
            target = (dest / rel).resolve()
            if not str(target).startswith(str(dest)):  # 防 zip 路径穿越
                raise UpdaterError(f"压缩包内存在非法路径：{member}")
            (target).parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 256)
    return dest


def collect_files(root: Path) -> list[str]:
    """列出 root 下全部文件的相对路径（POSIX 风格、按名排序）。"""
    result = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            rel = Path(dirpath).relative_to(root) / name
            result.append(rel.as_posix())
    return sorted(result)


# ---------------------------------------------------------------- 同步计划

def sync_plan(
    old_files: Optional[Iterable[str]],
    new_files: Iterable[str],
) -> tuple[list[str], list[str]]:
    """给定旧 manifest 文件集与新版本文件集，算需要删除与覆盖的文件。

    返回 (to_delete, to_overwrite)。无旧 manifest（首次更新）只覆盖不删除；
    config/.venv 等受保护路径永远不进入删除清单。
    """
    new_set = {f for f in new_files if not _is_protected(PurePosixPath(f))}
    old_set = {f for f in (old_files or []) if not _is_protected(PurePosixPath(f))}
    to_delete = sorted(old_set - new_set)
    to_overwrite = sorted(new_set)
    return to_delete, to_overwrite


def _copy_tree_into_backup(root: Path, backup: Path, rel_files: Iterable[str]) -> None:
    for rel in rel_files:
        src = root / rel
        if not src.is_file():
            continue
        dst = backup / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _restore_backup(root: Path, backup: Path) -> None:
    if not backup.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(backup):
        for name in filenames:
            rel = Path(dirpath).relative_to(backup) / name
            src = backup / rel
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _prune_backups(backup_root: Path, keep: int = 3) -> None:
    try:
        dirs = sorted(
            (p for p in backup_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        for old in dirs[keep:]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 应用更新

def apply_update(
    root: Path,
    new_root: Path,
    old_files: Optional[Iterable[str]],
    *,
    dry_run: bool = False,
) -> list[str]:
    """把 new_root 的内容同步进 root；返回本次写入的文件列表。"""
    new_files = collect_files(new_root)
    to_delete, to_overwrite = sync_plan(old_files, new_files)

    if dry_run:
        info(f"[dry-run] 将覆盖 {len(to_overwrite)} 个文件，删除 {len(to_delete)} 个旧文件")
        if to_delete:
            info("将删除的旧文件：")
            for rel in to_delete[:20]:
                info(f"  - {rel}")
            if len(to_delete) > 20:
                info(f"  … 等共 {len(to_delete)} 个")
        return new_files

    backup_root = root / "output" / "update_backups"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_root / stamp
    backup.mkdir(parents=True, exist_ok=True)

    # 1) 备份将受影响文件 + config.yaml（防呆）
    changed = [f for f in to_overwrite if (root / f).is_file()]
    _copy_tree_into_backup(root, backup, changed + to_delete)
    for cfg in ("config.yaml", "config.local.yaml"):
        src = root / cfg
        if src.is_file():
            dst = backup / cfg
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    try:
        # 2) 删除旧版独有文件（先删后写，避免被删文件挡住同名目录/文件）
        for rel in to_delete:
            target = root / rel
            if target.is_file():
                target.unlink()
        # 3) 覆盖写入新版本
        written: list[str] = []
        for rel in to_overwrite:
            src = new_root / rel
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            written.append(rel)
    except Exception as exc:  # noqa: BLE001
        warn(f"更新过程中出错（{exc}），正在回滚…")
        _restore_backup(root, backup)
        raise UpdaterError("更新失败，已自动回滚到更新前状态，可重新运行再试") from exc

    _prune_backups(backup_root)
    info(f"代码更新完成：覆盖 {len(to_overwrite)} 个、删除 {len(to_delete)} 个文件")
    info(f"备份目录（保留最近 3 份）：{backup_root}")
    return to_overwrite


# ---------------------------------------------------------------- 依赖安装

def install_dependencies(root: Path) -> bool:
    """把新版 requirements.txt 装进 .venv；返回是否成功（.venv 缺失视为跳过）。"""
    venv_py = root / ".venv" / "Scripts" / "python.exe"
    if not venv_py.is_file():
        venv_py = root / ".venv" / "bin" / "python"
    if not venv_py.is_file():
        warn("未找到 .venv，跳过依赖安装（首次启动 start.bat 会自动准备环境）")
        return True
    req = root / "requirements.txt"
    if not req.is_file():
        return True
    mirrors = ("https://pypi.tuna.tsinghua.edu.cn/simple", "https://pypi.org/simple")
    for index, mirror in enumerate(mirrors, 1):
        info(f"安装依赖（{index}/{len(mirrors)}：{mirror}）…")
        proc = subprocess.run(
            [
                str(venv_py),
                "-m",
                "pip",
                "install",
                "-i",
                mirror,
                "--timeout",
                "60",
                "--upgrade",
                "-r",
                str(req),
            ],
            cwd=str(root),
        )
        if proc.returncode == 0:
            info("依赖安装完成")
            return True
    warn("依赖安装失败，可先忽略；下次启动 start.bat 检测到环境不可用时会自动重建")
    return False


# ---------------------------------------------------------------- git 克隆模式

def update_via_git(root: Path) -> None:
    if not shutil.which("git"):
        raise UpdaterError("目录是 git 克隆但未找到 git 命令，请用 --force-zip 走整包覆盖")
    proc = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise UpdaterError(
            "git pull 失败：可能有本地未提交改动或远端需要合并。"
            "请先处理本地改动后重试，或用 --force-zip 强制整包覆盖（会只替换仓库文件）。"
        )
    files = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        capture_output=True,
    )
    listed = [
        f for f in files.stdout.decode("utf-8", errors="replace").split("\0") if f
    ]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True
    )
    save_state(root, commit=head.stdout.strip() or "", files=listed)
    info("git 模式更新完成")


# ---------------------------------------------------------------- 主流程

def _check_writable(root: Path) -> None:
    probe = root / ".update-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise UpdaterError(
            f"项目目录不可写（{exc}）。请把程序放到可写的本地目录（例如桌面/D盘）后重试"
        ) from exc


def _check_not_running() -> None:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 8970), timeout=0.5):
            raise UpdaterError(
                "检测到 Web 界面仍在运行（127.0.0.1:8970）。"
                "请先关闭程序窗口（start.bat / start-gui.bat），再运行更新。"
            )
    except UpdaterError:
        raise
    except OSError:
        pass  # 端口未占用，继续


def update_via_zip(
    root: Path,
    *,
    repo_url: str,
    branch: str,
    url: str = "",
    dry_run: bool = False,
    install_deps: bool = True,
) -> None:
    proxy = system_proxy()
    if proxy:
        info(f"检测到系统代理：{proxy}")

    branch_candidates = [branch] if branch else [DEFAULT_BRANCH, "master"]
    old_state = load_state(root)
    old_files = old_state.get("files")
    old_commit = old_state.get("commit", "")
    if old_commit:
        info(f"本地版本：{old_commit[:10]}")

    with tempfile.TemporaryDirectory(prefix="mau_update_") as work:
        work_dir = Path(work)
        tmp_zip = work_dir / "update.zip"
        used_branch = ""
        if url:
            download_zip(url, tmp_zip, proxy=proxy)
            used_branch = branch or DEFAULT_BRANCH
        else:
            last_error = ""
            for candidate in branch_candidates:
                info(f"尝试远端分支 {candidate} …")
                try:
                    download_zip(_archive_url(repo_url, candidate), tmp_zip, proxy=proxy)
                    used_branch = candidate
                    break
                except UpdaterError as exc:
                    last_error = str(exc)
                    warn(last_error)
                    tmp_zip.unlink(missing_ok=True)
                    continue
            if not used_branch:
                raise UpdaterError(
                    "GitHub 所有候选分支均下载失败。可检查代理，或用 --url 指定镜像地址，"
                    "例如：\n"
                    "  python update.py --url "
                    "https://ghproxy.com/https://github.com/hakrelin/manga_uploader/"
                    "archive/refs/heads/main.zip"
                )

        new_root = work_dir / "new"
        new_root.mkdir()
        info(f"解压新版本…")
        _extract_zip(tmp_zip, new_root)
        apply_update(root, new_root, old_files, dry_run=dry_run)

    if not dry_run:
        commit = remote_commit(repo_url, used_branch)
        save_state(root, commit=commit)
        if install_deps:
            install_dependencies(root)
        info("")
        info("更新完成！config.yaml、漫画导入缓存与本机环境均未改动。")
        info("现在可以重新双击 start.bat 启动。")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="update",
        description="漫画发布器本地覆盖更新（保留 config.yaml / .venv / output 等）",
    )
    parser.add_argument("--check", action="store_true", help="只检查远端是否有新版本")
    parser.add_argument(
        "--repo", default=DEFAULT_REPO, help=f"GitHub 仓库地址（默认 {DEFAULT_REPO}）"
    )
    parser.add_argument(
        "--branch",
        default="",
        help=f"分支名（默认先试 {DEFAULT_BRANCH}，失败再试 master）",
    )
    parser.add_argument(
        "--url", default="", help="直接指定下载地址（GitHub 连不上时用镜像）"
    )
    parser.add_argument(
        "--force-zip",
        action="store_true",
        help="即使目录是 git 克隆也走整包覆盖（默认 git 克隆用 git pull）",
    )
    parser.add_argument(
        "--no-deps", action="store_true", help="更新后不自动安装依赖"
    )
    parser.add_argument("--dry-run", action="store_true", help="只下载并预览，不改本机")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    _console_utf8()
    args = parse_args(argv)
    root = Path(__file__).resolve().parent.parent

    if args.check:
        return check_update(root, args.repo, args.branch or DEFAULT_BRANCH)

    info("漫画发布器 本地覆盖更新")
    info(f"项目目录：{root}")
    _check_writable(root)
    _check_not_running()

    use_git = (root / ".git").is_dir() and not args.force_zip
    try:
        if use_git:
            if args.dry_run:
                info("[dry-run] 检测到 git 克隆；dry-run 仅对整包覆盖模式有意义，已跳过")
                return 0
            info("检测到 git 克隆，使用 git pull 更新（保留所有本机文件）…")
            update_via_git(root)
            if not args.no_deps:
                install_dependencies(root)
        else:
            update_via_zip(
                root,
                repo_url=args.repo,
                branch=args.branch,
                url=args.url,
                dry_run=args.dry_run,
                install_deps=not args.no_deps,
            )
    except UpdaterError as exc:
        fail(str(exc))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
