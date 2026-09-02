# -*- coding: utf-8 -*-
"""Windows 图形界面启动器：双击本文件即可打开 GUI。

注意：很多电脑的 .pyw 关联被旧版 Python（如 ArcGIS 自带的 Python 2.7）
占用。本文件保持 Python 2/3 都能解析的写法，若检测到当前解释器太老，
会自动重新用本机 Python 3（优先 C:\\Python313）启动，避免双击没反应。
"""

from __future__ import absolute_import

import os
import subprocess
import sys
import tempfile


def _find_candidates():
    """收集本机可用的 pythonw.exe / python.exe（去重）。"""
    seen = set()
    result = []

    def _add(path):
        path = os.path.normpath(path)
        if path in seen or not os.path.isfile(path):
            return
        seen.add(path)
        result.append(path)

    # 已知的常见 Python 3 安装位置
    for ver in ("313", "312", "311", "310", "39", "38"):
        _add("C:\\Python%s\\pythonw.exe" % ver)
        _add("C:\\Python%s\\python.exe" % ver)
    # PATH 里的 pythonw/python
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip()
        if not folder:
            continue
        _add(os.path.join(folder, "pythonw.exe"))
        _add(os.path.join(folder, "python.exe"))
    # Windows 官方 py 启动器（pyw.exe 无控制台窗口）
    windir = os.environ.get("WINDIR", "C:\\Windows")
    _add(os.path.join(windir, "pyw.exe"))
    _add(os.path.join(windir, "py.exe"))
    return result


def _check_version(path):
    """运行解释器并返回 (major, minor)；拿不到版本返回 (0, 0)。"""
    tag = os.path.join(
        tempfile.gettempdir(),
        "mangaupload_pyver_%d_%d.txt" % (os.getpid(), len(_find_candidates())),
    )
    try:
        if os.path.exists(tag):
            os.remove(tag)
        code = (
            "import os,sys;"
            "open(os.environ['MU_VER_FILE'],'w').write('%d.%d'%sys.version_info[:2])"
        )
        env = dict(os.environ)
        env["MU_VER_FILE"] = tag
        with open(os.devnull, "w") as devnull:
            proc = subprocess.Popen(
                [path, "-c", code],
                stdout=devnull,
                stderr=devnull,
                env=env,
            )
            proc.wait()
        if not os.path.exists(tag):
            return (0, 0)
        with open(tag, "r") as fh:
            text = fh.read().strip()
        parts = text.split(".")
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except Exception:
        return (0, 0)
    finally:
        try:
            if os.path.exists(tag):
                os.remove(tag)
        except Exception:
            pass


def _show_error(message):
    """用系统 MessageBox 提示（pythonw 没有控制台，消息框是唯一可见反馈）。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, u"漫画多平台发布器", 0x10)
    except Exception:
        try:
            log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui-error.log")
            with open(log, "a") as fh:
                fh.write(message.encode("utf-8", "replace") if isinstance(message, str) else message)
                fh.write("\n")
        except Exception:
            pass


def _relaunch_with_python3():
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "gui.pyw")
    pyw_launcher = os.path.join(
        os.environ.get("WINDIR", "C:\\Windows"), "pyw.exe"
    )
    for candidate in _find_candidates():
        version = _check_version(candidate)
        if version < (3, 7):
            continue
        try:
            if candidate.lower().endswith("pyw.exe") or candidate.lower().endswith("py.exe"):
                if os.path.isfile(pyw_launcher):
                    # 优先无控制台启动
                    subprocess.Popen(
                        [pyw_launcher, "-3", script], cwd=here
                    )
                else:
                    subprocess.Popen([candidate, "-3", script], cwd=here)
            else:
                subprocess.Popen([candidate, script], cwd=here)
            return True
        except Exception:
            continue
    _show_error(
        u"没有找到可用的 Python 3。\n\n"
        u"请先安装 Python 3（勾选 Add to PATH），或右键本文件→打开方式→"
        u"C:\\Python313\\pythonw.exe。"
    )
    return False


def _main():
    if sys.version_info[0] < 3 or sys.version_info < (3, 7):
        # 被旧版 pythonw（例如 ArcGIS Python 2.7）拉起：换 Python 3 重启
        _relaunch_with_python3()
        return
    from manga_uploader.gui import run_gui

    raise SystemExit(run_gui())


if __name__ == "__main__":
    _main()
