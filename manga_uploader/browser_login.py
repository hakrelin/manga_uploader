"""打开真实浏览器登录，自动把 Cookie 抓回来填进配置。

背景：再漫画的 token 每 30 天过期，过期后上传接口直接报「请先登录」，
手动去开发者工具里翻 Cookie 又很容易漏 / 复制错。这里的做法是：

1. 用**独立档案目录**启动系统里的 Chrome/Edge（不影响用户日常浏览器，
   登录状态也能长期保留在档案里，下次直接复用）；
2. 打开该平台的登录页，用户自己扫码 / 输密码登录（不需要我们处理验证码）；
3. 通过 CDP（DevTools 协议）轮询浏览器 Cookie，等到目标 Cookie 出现
   （再漫画还会顺带校验 JWT 的 exp，避免拿到浏览器里那个旧的过期 token）；
4. 把 Cookie 交给调用方保存。

只依赖标准库，WebSocket 客户端见 `cdp.py`。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .cdp import BrowserCDP, CDPError, debug_port_alive
from .util import get_logger

LOGGER = get_logger("browser_login")


class BrowserLoginError(RuntimeError):
    """浏览器登录流程失败（拿不到 Cookie / 启动不了浏览器）。"""


class BrowserLoginClosed(BrowserLoginError):
    """用户把浏览器窗口关了。"""


class BrowserLoginTimeout(BrowserLoginError):
    """等登录超时。"""


class BrowserLoginCancelled(BrowserLoginError):
    """调用方主动取消。"""


@dataclass(frozen=True)
class LoginSpec:
    """某个平台「浏览器登录」需要什么。"""

    key: str
    label: str
    url: str
    domains: tuple[str, ...]
    save: tuple[str, ...]
    require: tuple[str, ...]
    full_cookie: bool = False
    note: str = ""


LOGIN_SPECS: dict[str, LoginSpec] = {
    "bilibili": LoginSpec(
        key="bilibili",
        label="B站",
        url="https://passport.bilibili.com/login",
        domains=("bilibili.com",),
        save=("SESSDATA", "bili_jct", "buvid3", "buvid4", "b_nut", "DedeUserID"),
        require=("SESSDATA", "bili_jct"),
    ),
    "tieba": LoginSpec(
        key="tieba",
        label="百度贴吧",
        url="https://tieba.baidu.com/",
        domains=("baidu.com",),
        save=("BDUSS", "STOKEN", "BAIDUID", "BAIDUID_BFESS"),
        require=("BDUSS",),
    ),
    "ehentai": LoginSpec(
        key="ehentai",
        label="e-hentai",
        url="https://forums.e-hentai.org/index.php?act=Login&CODE=00",
        domains=("e-hentai.org", "exhentai.org"),
        save=("ipb_member_id", "ipb_pass_hash", "ipb_session_id", "igneous"),
        require=("ipb_member_id", "ipb_pass_hash"),
        note="需要登录论坛（forums）后，再打开一次 exhentai/e-hentai 主页，Cookie 才齐全。",
    ),
    "zaimanhua": LoginSpec(
        key="zaimanhua",
        label="再漫画",
        url="https://manhua.zaimanhua.com/",
        domains=("zaimanhua.com",),
        save=("token", "clientId"),
        require=("token",),
        note="token 有效期 30 天，过期后要重新登录一次。",
    ),
    "xiaoheihe": LoginSpec(
        key="xiaoheihe",
        label="小黑盒",
        url="https://www.xiaoheihe.cn/creator/editor/draft/image_text",
        domains=("xiaoheihe.cn",),
        save=("heybox_id",),
        require=("heybox_id",),
        full_cookie=True,
        note="会保存该域名下的整段 Cookie（含登录态与设备标识）。",
    ),
}


def spec_for(platform: str) -> LoginSpec:
    spec = LOGIN_SPECS.get(str(platform or "").strip().lower())
    if spec is None:
        raise BrowserLoginError(f"平台 {platform} 不支持浏览器登录取 Cookie")
    return spec


def supports(platform: str) -> bool:
    return str(platform or "").strip().lower() in LOGIN_SPECS


# ----------------------------------------------------------------- 浏览器定位

_BROWSER_ENV = "MANGA_UPLOADER_BROWSER"
_HEADLESS_ENV = "MANGA_UPLOADER_BROWSER_HEADLESS"


def headless_default() -> bool:
    """默认是否无界面启动（可用环境变量临时改成 1，便于自动化测试）。"""
    return os.environ.get(_HEADLESS_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def default_profile_dir() -> Path:
    """浏览器档案目录（放用户数据目录，避免被 output 清理逻辑删掉）。"""
    override = os.environ.get("MANGA_UPLOADER_BROWSER_PROFILE")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "manga_uploader" / "browser-profile"


def _windows_registry_paths() -> list[str]:
    paths: list[str] = []
    if os.name != "nt":
        return paths
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for exe in ("msedge.exe", "chrome.exe"):
                key_name = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        value, _ = winreg.QueryValueEx(key, "")
                        if value:
                            paths.append(str(value))
                except OSError:
                    continue
    except Exception:  # noqa: BLE001 - 注册表读不到就退回常见路径
        pass
    return paths


def _windows_common_paths() -> list[str]:
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    rels = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
        r"Vivaldi\Application\vivalo.exe",
    ]
    out = []
    for root in roots:
        if not root:
            continue
        for rel in rels:
            out.append(os.path.join(root, rel))
    return out


def find_browser() -> Optional[str]:
    """找一个 Chromium 系浏览器；返回可执行文件路径，找不到返回 None。"""
    override = os.environ.get(_BROWSER_ENV)
    if override and Path(override).is_file():
        return override

    candidates: list[str] = []
    if os.name == "nt":
        candidates += _windows_registry_paths()
        candidates += _windows_common_paths()
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    for candidate in candidates:
        try:
            if candidate and Path(candidate).is_file():
                return str(candidate)
        except OSError:
            continue
    return None


# --------------------------------------------------------------- Cookie 处理


def _domain_matches(domain: str, wanted: tuple[str, ...]) -> bool:
    domain = str(domain or "").lstrip(".").lower()
    if not domain:
        return False
    return any(domain == item or domain.endswith("." + item) for item in wanted)


def pick_cookies(rows: list[dict], spec: LoginSpec) -> dict[str, str]:
    """从浏览器全部 Cookie 里挑出这个平台要保存的那些。"""
    picked: dict[str, str] = {}
    ordered: list[tuple[str, str]] = []
    wanted = set(spec.save)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _domain_matches(str(row.get("domain") or ""), spec.domains):
            continue
        name = str(row.get("name") or "").strip()
        value = str(row.get("value") or "")
        if not name:
            continue
        ordered.append((name, value))
        if name in wanted and value:
            picked[name] = value
    if spec.full_cookie and ordered:
        text = "; ".join(f"{n}={v}" for n, v in ordered if v)
        if text:
            picked["cookie"] = text
    return picked


def _zaimanhua_ready(picked: dict[str, str]) -> tuple[bool, str]:
    """再漫画专用：浏览器里可能还留着旧的过期 token，必须等到新的。"""
    from .publishers.zaimanhua import decode_token_expiry

    token = str(picked.get("token") or "")
    exp = decode_token_expiry(token)
    if exp is None:
        return True, "已获取再漫画 token（不是 JWT，无法判断有效期）"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
    if exp <= time.time():
        return False, f"浏览器里还是过期的那个 token（{when} 到期），请在该窗口重新登录一次"
    return True, f"已获取新的再漫画 token（有效期至 {when}）"


_READY_CHECKS: dict[str, Callable[[dict[str, str]], tuple[bool, str]]] = {
    "zaimanhua": _zaimanhua_ready,
}


def spec_ready(spec: LoginSpec, picked: dict[str, str]) -> tuple[bool, str]:
    missing = [name for name in spec.require if not str(picked.get(name) or "").strip()]
    if missing:
        return False, f"等待登录…（还没拿到 {'/'.join(missing)}）"
    checker = _READY_CHECKS.get(spec.key)
    if checker is not None:
        return checker(picked)
    return True, f"已获取 {spec.label} 的 Cookie"


# ------------------------------------------------------------------ 启动浏览器


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _chromium_args(exe: str, url: str, profile_dir: Path, port: int, *, headless: bool) -> list[str]:
    args = [
        exe,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--new-window",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url)
    return args


class BrowserLoginSession:
    """启动浏览器 → 等用户登录 → 读 Cookie 的一整套流程。"""

    def __init__(
        self,
        platform: str,
        *,
        profile_dir: Optional[Path] = None,
        timeout: float = 600.0,
        poll: float = 1.5,
        headless: Optional[bool] = None,
        browser: Optional[str] = None,
        log=None,
    ) -> None:
        self.spec = spec_for(platform)
        self.profile_dir = Path(profile_dir) if profile_dir else default_profile_dir()
        self.timeout = float(timeout)
        self.poll = max(0.5, float(poll))
        self.headless = headless_default() if headless is None else bool(headless)
        self.browser = browser
        self.log = log if log is not None else LOGGER
        self.port: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self._cdp: Optional[BrowserCDP] = None

    # -- 生命周期 --

    def _port_file(self) -> Path:
        return self.profile_dir / "cdp-port.txt"

    def _read_saved_port(self) -> int:
        try:
            text = self._port_file().read_text(encoding="utf-8").strip()
            return int(text)
        except (OSError, ValueError):
            return 0

    def start(self) -> int:
        """（复用或新启）浏览器并连上调试端口，返回端口号。"""
        exe = self.browser or find_browser()
        if not exe:
            raise BrowserLoginError(
                "没找到 Chrome / Edge 浏览器。装一个 Chrome 或 Edge 后重试，"
                "或设置环境变量 MANGA_UPLOADER_BROWSER 指向浏览器可执行文件。"
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        reuse = self._read_saved_port()
        if reuse and debug_port_alive(reuse):
            self.port = reuse
            self.log.info("复用已打开的浏览器（调试端口 %s）", reuse)
            return reuse

        port = _free_port()
        args = _chromium_args(exe, self.spec.url, self.profile_dir, port, headless=self.headless)
        self.log.info("启动浏览器：%s", " ".join(args[:3]))
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(args, **kwargs)
        self.port = port

        deadline = time.time() + 30.0
        while time.time() < deadline:
            if debug_port_alive(port):
                try:
                    self._port_file().write_text(str(port), encoding="utf-8")
                except OSError:
                    pass
                return port
            if self.proc is not None and self.proc.poll() is not None and self.proc.returncode not in (0, None):
                break
            time.sleep(0.4)
        raise BrowserLoginError(
            "浏览器起来了但调试端口没响应。常见原因：该档案目录已经有窗口在运行"
            "（请全部关掉再重试），或被安全软件拦住了远程调试。"
        )

    def close(self, *, kill: bool = False) -> None:
        if self._cdp is not None:
            self._cdp.close()
            self._cdp = None
        if kill and self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:  # pragma: no cover
                pass
        self.proc = None

    # -- 读 Cookie --

    def _cookies(self) -> list[dict]:
        if self.port is None:
            raise BrowserLoginError("浏览器还没启动")
        if self._cdp is None:
            self._cdp = BrowserCDP(self.port, timeout=10.0)
            self._cdp.connect()
        try:
            return self._cdp.cookies(timeout=8.0)
        except CDPError:
            self._cdp.close()
            self._cdp = None
            raise

    def wait_for_login(
        self,
        *,
        should_stop: Optional[Callable[[], bool]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, str]:
        """轮询到目标 Cookie（再漫画还要确认 token 没过期）为止。"""
        if self.port is None:
            self.start()
        deadline = time.time() + float(timeout if timeout is not None else self.timeout)
        last_message = ""
        while time.time() < deadline:
            if should_stop is not None and should_stop():
                raise BrowserLoginCancelled("已取消浏览器登录")
            try:
                rows = self._cookies()
            except CDPError as exc:
                if self.port and not debug_port_alive(self.port):
                    raise BrowserLoginClosed(
                        "浏览器窗口已被关闭，Cookie 没拿到。重新点一次「浏览器登录」即可。"
                    ) from exc
                last_message = f"连接浏览器失败，重试中：{exc}"
                if on_status:
                    on_status(last_message)
                time.sleep(self.poll)
                continue

            picked = pick_cookies(rows, self.spec)
            ok, message = spec_ready(self.spec, picked)
            if message != last_message:
                last_message = message
                self.log.info("%s", message)
                if on_status:
                    on_status(message)
            if ok:
                return picked
            time.sleep(self.poll)
        raise BrowserLoginTimeout(
            f"等了 {int(self.timeout)} 秒还没拿到 Cookie。可以再点一次「浏览器登录」，"
            "已登录的状态会保留在浏览器档案里。"
        )


def grab_cookies(
    platform: str,
    *,
    profile_dir: Optional[Path] = None,
    timeout: float = 600.0,
    headless: Optional[bool] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_status: Optional[Callable[[str], None]] = None,
    keep_browser_open: bool = True,
    log=None,
) -> dict[str, str]:
    """一次性完成「开浏览器 → 登录 → 读 Cookie」，返回要保存的 Cookie。"""
    session = BrowserLoginSession(
        platform, profile_dir=profile_dir, timeout=timeout, headless=headless, log=log
    )
    try:
        session.start()
        return session.wait_for_login(should_stop=should_stop, on_status=on_status)
    finally:
        session.close(kill=not keep_browser_open)
