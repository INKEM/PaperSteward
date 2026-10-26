"""离线验收专用服务：任何非回环网络请求立即失败并留痕。"""

import os
import sys
from pathlib import Path

import uvicorn


def network_guard(event: str, args: tuple) -> None:
    if event not in {"socket.connect", "socket.getaddrinfo"}:
        return
    host = args[1][0] if event == "socket.connect" else args[0]
    if host not in {"127.0.0.1", "::1", "localhost", None}:
        Path(os.environ["INSIGHTS_DENIED_LOG"]).write_text(str(host))
        raise RuntimeError("离线验收禁止外网")


sys.addaudithook(network_guard)
uvicorn.run("papersteward.main:app", host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
