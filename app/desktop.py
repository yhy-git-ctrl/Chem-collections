"""桌面版入口：后台起本地服务，用原生窗口展示。

用法：python -m app.desktop
逻辑：先探测指定端口上是否已在跑"本 App"（通过 /api/stats）。是 -> 直接复用；
      端口被其它程序占用 -> 换一个空闲端口；端口空闲 -> 自己启动。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn
import webview

from . import config
from .web import app

START_PORT = 8011
LOG_FILE = config.PROJECT_ROOT / "desktop.log"


def _log(msg: str) -> None:
    """把运行信息写到项目根目录 desktop.log，便于诊断。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _is_our_app(host: str, port: int) -> bool:
    """请求 /api/stats，能取到 json 且含 total 字段即视为本 App。"""
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/stats", timeout=1.5
        ) as r:
            if r.status != 200:
                return False
            text = r.read(2000).decode("utf-8", "ignore")
            return '"total"' in text
    except Exception:
        return False


def _port_listening(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _find_free_port(host: str, port: int) -> int:
    """从 port 起找一个没有被监听（也没有本 App 占用）的端口。"""
    for p in range(port, port + 200):
        if not _port_listening(host, p):
            return p
    raise RuntimeError("找不到空闲端口")


def _wait_server(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_our_app(host, port):
            return True
        time.sleep(0.3)
    return False


def _run_server(port: int) -> None:
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


def main() -> int:
    config.ensure_dirs()

    # 1) 端口上已是我们 App -> 直接复用
    if _is_our_app("127.0.0.1", START_PORT):
        port = START_PORT
        _log(f"端口 {port} 已在运行本服务，直接复用。")
    # 2) 端口被占用但不是我们 -> 换空闲端口自己启动
    elif _port_listening("127.0.0.1", START_PORT):
        port = _find_free_port("127.0.0.1", START_PORT)
        _log(f"端口 {START_PORT} 被其它程序占用，改用端口 {port}。")
        t = threading.Thread(target=_run_server, args=(port,), daemon=True)
        t.start()
        if not _wait_server("127.0.0.1", port):
            _log("服务启动失败。")
            return 1
    # 3) 端口空闲 -> 自己启动
    else:
        port = START_PORT
        _log(f"正在启动本地服务 http://127.0.0.1:{port} ...")
        t = threading.Thread(target=_run_server, args=(port,), daemon=True)
        t.start()
        if not _wait_server("127.0.0.1", port):
            _log("服务启动失败。")
            return 1

    # 带一个时间戳作为版本号，避免 WebView2/浏览器缓存旧页面。
    url = f"http://127.0.0.1:{port}/?v={int(time.time()*1000)}"
    _log(f"打开窗口：{url}（关闭窗口即退出）")
    try:
        webview.create_window(
            "化学文献库",
            url,
            width=1280,
            height=860,
            min_size=(1024, 720),
            x=None,
            y=None,
        )
        # private_mode=True：WebView2 用内存缓存，不落盘，避免加载到旧页面。
        webview.start(private_mode=True)
    except Exception as e:  # 窗口失败则回退浏览器
        _log(f"桌面窗口不可用：{e}，回退到浏览器。")
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
