"""PKCE 回调捕获监听器：在 127.0.0.1:56121 起一个极简 HTTP 服务接 OAuth 回调。

背景：authorize_pkce 的 page.on("request") 钩子在部分浏览器下拿不到跨进程重定向的
最终 URL；与其依赖它，不如真的把回调接住——浏览器 redirect 到 loopback 会命中本
服务，我们从 query 里取 code，页面回 200 "可关闭"。线程安全。

并发隔离：回调按 state 分桶存储。同一进程里多个注册任务共用 56121 监听时，
每个任务只读自己 state 对应的 code，绝不串号。
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

_CAPTURED: dict[str, str] = {}  # state -> callback url
_LOCK = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if "code=" in (parsed.query or ""):
            qs = parse_qs(parsed.query)
            state = str((qs.get("state") or [""])[0]).strip()
            with _LOCK:
                if state:
                    _CAPTURED[state] = f"http://127.0.0.1:56121{self.path}"
        body = b"ok. you may close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass


def start_listener() -> threading.Thread | None:
    """幂等启动（本进程内）。端口被外部进程占用时返回 None 并置 bound=False，
    调用方据此降级（取证根因 A：静默假装成功导致回调落进别的进程）。"""
    start_listener.bound = getattr(start_listener, "bound", False)
    if start_listener.bound:
        return None
    try:
        httpd = HTTPServer(("127.0.0.1", 56121), _Handler)
    except OSError:
        start_listener.bound = False  # type: ignore[attr-defined]
        return None
    start_listener.bound = True  # type: ignore[attr-defined]

    def _serve():
        try:
            httpd.serve_forever()
        except Exception:
            pass

    t = threading.Thread(target=_serve, daemon=True, name="pkce-callback")
    t.start()
    return t


def get_capture(state: str = "") -> str:
    """按 state 取回调；state 为空则返回任意一条（单任务兼容）。"""
    with _LOCK:
        if state:
            return _CAPTURED.get(state, "")
        return next(iter(_CAPTURED.values()), "")


def clear_capture(state: str = "") -> None:
    with _LOCK:
        if state:
            _CAPTURED.pop(state, None)
        else:
            _CAPTURED.clear()
