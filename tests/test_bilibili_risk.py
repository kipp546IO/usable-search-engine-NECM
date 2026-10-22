"""B站风控边界回归：风控不能伪装成 UID 不存在。"""
from __future__ import annotations

import json
import os
import sys
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import bilibili, server


def test_user_info_exposes_risk_control() -> None:
    original = bilibili._request

    def fake_request(url: str, **kwargs) -> dict:
        return {"code": -352, "message": "风控校验失败"}

    bilibili._request = fake_request
    try:
        try:
            bilibili.user_info(1459104794)
        except bilibili.RiskControlError:
            return
        raise AssertionError("UID 风控应抛出 RiskControlError，而不是返回空用户")
    finally:
        bilibili._request = original


def test_uid_api_exposes_risk_control() -> None:
    original_request = bilibili._request
    original_path = server.Handler.db_path
    temp_path = os.path.join(ROOT, "tests", ".run", "risk_api.sqlite3")
    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
    listener = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)

    def fake_request(url: str, **kwargs) -> dict:
        return {"code": -352, "message": "风控校验失败"}

    bilibili._request = fake_request
    server.Handler.db_path = temp_path
    thread.start()
    try:
        query = urlencode({"src": "bilibili", "type": "uid", "q": "1459104794"})
        try:
            with urlopen(f"http://127.0.0.1:{listener.server_port}/api/search?{query}", timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
                status = response.status
        except HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read().decode("utf-8"))
        assert status == 200, (status, body)
        assert body.get("risk") is True, body
    finally:
        bilibili._request = original_request
        server.Handler.db_path = original_path
        listener.shutdown()
        listener.server_close()
        thread.join(timeout=5)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(temp_path + suffix)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    test_user_info_exposes_risk_control()
    test_uid_api_exposes_risk_control()
    print("[bilibili-risk] PASS")
