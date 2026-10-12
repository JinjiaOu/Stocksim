"""`python -m app [--no-browser]` — run the server on 127.0.0.1 and open the browser.

If StockSim is already running, just open the browser and exit.
"""
import logging
import os
import socket
import sys
import threading
import webbrowser

import uvicorn

from app import db, main as server

HOST, PORT = "127.0.0.1", int(os.environ.get("STOCKSIM_PORT", 8765))
URL = f"http://{HOST}:{PORT}/"


def already_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    browser = "--no-browser" not in sys.argv
    if already_running():
        if sys.stdout:
            print(f"StockSim 已在运行：{URL}")
        if browser:
            webbrowser.open(URL)
        sys.exit(0)
    frozen = getattr(sys, "frozen", False)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if frozen:  # windowed exe has no console: log to a file next to the exe
        db.APP_ROOT.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format=fmt, filename=db.APP_ROOT / "stocksim.log", encoding="utf-8")
        server.watch_idle = True
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
        print(f"StockSim 运行中：{URL}  （关闭此窗口即退出）")
    if browser:
        threading.Timer(1.5, webbrowser.open, (URL,)).start()
    uvicorn.run(server.app, host=HOST, port=PORT, log_level="warning", log_config=None)
