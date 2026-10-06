"""极简 Chrome DevTools Protocol 客户端（只依赖标准库）。

用途：启动一个真实的 Chrome/Edge 让用户登录，然后用 CDP 把 Cookie 读回来。
这里手写了一个够用的 WebSocket 客户端（握手 + 文本帧 + ping/pong + 分片重组），
避免为了这点功能引入 websocket-client / playwright 之类的重依赖。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.request import urlopen


class CDPError(RuntimeError):
    pass


# ------------------------------------------------------------------ WebSocket


class WebSocketClient:
    """最小可用 WebSocket 客户端（仅客户端、仅文本帧、带掩码发送）。"""

    def __init__(self, url: str, *, timeout: float = 20.0) -> None:
        self.url = url
        self.timeout = float(timeout)
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._frag = b""

    # -- 连接 --

    def connect(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise CDPError(f"不支持的 WebSocket 地址：{self.url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        sock = socket.create_connection((host, port), timeout=self.timeout)
        if parsed.scheme == "wss":  # pragma: no cover - 本机调试用不到
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket 握手失败：连接被关闭")
            head += chunk
            if len(head) > 65536:  # pragma: no cover - 防御性
                raise CDPError("WebSocket 握手响应异常")
        header, _, rest = head.partition(b"\r\n\r\n")
        status_line = header.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101" not in status_line:
            raise CDPError(f"WebSocket 握手失败：{status_line}")
        sock.settimeout(self.timeout)
        self.sock = sock
        self._buf = rest

    # -- 收发 --

    def _read_exact(self, count: int) -> bytes:
        while len(self._buf) < count:
            chunk = self.sock.recv(65536)  # type: ignore[union-attr]
            if not chunk:
                raise CDPError("WebSocket 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.sock is None:
            raise CDPError("WebSocket 未连接")
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        """读一条完整文本消息（自动处理 ping / 分片）。超时抛 socket.timeout。"""
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:
                raise CDPError("WebSocket 连接已被浏览器关闭")
            if opcode == 0x9:  # ping → pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue

            self._frag += payload
            if not final:
                continue
            text = self._frag.decode("utf-8", "replace")
            self._frag = b""
            return text

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self._send_frame(0x8, b"")
        except Exception:  # noqa: BLE001 - 关闭失败无所谓
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None


# ------------------------------------------------------------------------ CDP


def browser_ws_url(port: int, *, timeout: float = 5.0) -> str:
    """读取 http://127.0.0.1:<port>/json/version，取浏览器级调试地址。"""
    url = f"http://127.0.0.1:{int(port)}/json/version"
    try:
        with urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise CDPError(f"连不上浏览器调试端口 {port}：{exc}") from exc
    ws = str(data.get("webSocketDebuggerUrl") or "").strip()
    if not ws:
        raise CDPError("浏览器未返回 webSocketDebuggerUrl（远程调试没开起来）")
    return ws


def list_targets(port: int, *, timeout: float = 5.0) -> list[dict]:
    """列出浏览器里的调试目标（page / iframe / service_worker …）。"""
    url = f"http://127.0.0.1:{int(port)}/json/list"
    try:
        with urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise CDPError(f"读不到浏览器目标列表（端口 {port}）：{exc}") from exc
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def find_page_ws_url(port: int, url_part: str = "", *, timeout: float = 5.0) -> str:
    """找某个页面目标的调试地址（Runtime.evaluate / 截图要用页面会话）。"""
    pages = [t for t in list_targets(port, timeout=timeout) if t.get("type") == "page"]
    if url_part:
        matched = [t for t in pages if url_part in str(t.get("url") or "")]
        pages = matched or pages
    for target in pages:
        ws = str(target.get("webSocketDebuggerUrl") or "").strip()
        if ws:
            return ws
    raise CDPError("没有可用的页面调试目标")


def debug_port_alive(port: int, *, timeout: float = 1.5) -> bool:
    try:
        browser_ws_url(port, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


class BrowserCDP:
    """浏览器级 CDP 会话（用于 Storage.getCookies）。"""

    def __init__(self, port: int, *, timeout: float = 20.0, ws_url: Optional[str] = None) -> None:
        self.port = int(port)
        self.timeout = float(timeout)
        self.ws_url = ws_url
        self._ws: Optional[WebSocketClient] = None
        self._id = 0

    def __enter__(self) -> "BrowserCDP":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def connect(self) -> None:
        ws = WebSocketClient(self.ws_url or browser_ws_url(self.port), timeout=self.timeout)
        ws.connect()
        self._ws = ws

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def call(self, method: str, params: Optional[dict] = None, *, timeout: Optional[float] = None):
        """发一条 CDP 命令并等它的返回值（中间的 event 直接忽略）。"""
        if self._ws is None:
            raise CDPError("CDP 未连接")
        self._id += 1
        message_id = self._id
        self._ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CDPError(f"CDP 命令超时：{method}")
            self._ws.sock.settimeout(max(0.5, remaining))  # type: ignore[union-attr]
            try:
                raw = self._ws.recv_text()
            except socket.timeout:
                raise CDPError(f"CDP 命令超时：{method}") from None
            try:
                message = json.loads(raw)
            except ValueError:  # pragma: no cover - 防御性
                continue
            if message.get("id") != message_id:
                continue  # 事件或别的响应
            if message.get("error"):
                raise CDPError(f"CDP 报错（{method}）：{message['error']}")
            return message.get("result") or {}

    def cookies(self, *, timeout: Optional[float] = None) -> list[dict]:
        """取浏览器里全部 Cookie（Storage.getCookies 是浏览器级命令）。"""
        result = self.call("Storage.getCookies", {}, timeout=timeout)
        rows = result.get("cookies")
        return rows if isinstance(rows, list) else []
