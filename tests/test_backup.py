"""失效视频备份站查询回归（离线，零网络）。

覆盖：
  - bv2av：已知样例与格式校验（纯计算）
  - 命中缓存：不发任何请求
  - 没命中的 TTL：8 天前会重试，1 天前不重试
  - 节流：两次真实请求间隔 ≥ MIN_INTERVAL
  - 非法 bvid：直接返回错误，不发请求

所有网络调用都被 monkeypatch 掉，跑测试不会碰到 ilibilib（个人站，不能打扰）。

用法（仓库根目录）:
    PYTHONUTF8=1 PYTHONPATH=src python tests/test_backup.py
退出码：全过 0，否则 1。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import unittest.mock
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from usable_search import backup

RUN_DIR = os.path.join(ROOT, "tests", ".run")
DAY = 86400


def _fresh_db(tag: str) -> str:
    """每个用例一个临时库（带 pid，避免并发互踩）。"""
    os.makedirs(RUN_DIR, exist_ok=True)
    db = os.path.join(RUN_DIR, f"backup_{os.getpid()}_{tag}.sqlite3")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    return db


def _drop_db(db: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass


def _prep(db: str) -> None:
    """先建表，后面才能直接写缓存行。"""
    backup._conn(db).close()


def _put(db: str, bvid: str, aid: int, ok: int, ts: int) -> None:
    """手动写一条缓存（模拟历史查询结果）。"""
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.execute("INSERT OR REPLACE INTO backup(bvid, aid, ok, ts) VALUES(?,?,?,?)",
                     (bvid, aid, ok, ts))
        conn.commit()
    finally:
        conn.close()


class _Resp:
    """假的 urlopen 返回值：只需 status 与上下文管理器。"""

    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def case_bv2av_known() -> tuple[bool, dict]:
    """三个已知 BV 号必须换出正确 av 号。"""
    got = {
        "BV17x411w7KC": backup.bv2av("BV17x411w7KC"),
        "BV1xx411c7mD": backup.bv2av("BV1xx411c7mD"),
        "BV1Jy4y1f7pV": backup.bv2av("BV1Jy4y1f7pV"),
    }
    ok = got == {"BV17x411w7KC": 170001, "BV1xx411c7mD": 2, "BV1Jy4y1f7pV": 779872886}
    return ok, got


def case_bv2av_bad() -> tuple[bool, dict]:
    """空串 / 太短 / av 号 / 长度不对都返回 None。"""
    bad = ["", "BV", "av170001", "BV123", "BV17x411w7K", "BV17x411w7KCX", None]
    got = [backup.bv2av(x) for x in bad]
    return all(v is None for v in got), {"got": got}


def case_cache_hit_no_network() -> tuple[bool, dict]:
    """库里已有 ok=1：直接用缓存，urlopen 一次都不许被调。"""
    db = _fresh_db("hit")
    calls: list = []
    try:
        _prep(db)
        _put(db, "BV17x411w7KC", 170001, 1, int(time.time()))
        with unittest.mock.patch.object(
                urllib.request, "urlopen",
                side_effect=lambda *a, **k: calls.append(a) or _Resp(200)):
            r = backup.lookup("BV17x411w7KC", db_path=db)
    finally:
        _drop_db(db)
    ok = (r.get("ok") is True and r.get("cached") is True and r.get("aid") == 170001
          and r.get("url") == "https://terminus2021.github.io/ilibilib/video/av170001.mp4"
          and len(calls) == 0)
    return ok, {"result": r, "calls": len(calls)}


def case_miss_ttl() -> tuple[bool, dict]:
    """8 天前的 miss 会重试；1 天前的 miss 直接用缓存。"""
    db = _fresh_db("ttl")
    calls: list = []
    now = int(time.time())

    def _fake(url, *a, **k):
        calls.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    try:
        _prep(db)
        _put(db, "BV1xx411c7mD", 2, 0, now - 8 * DAY)
        backup._last_at = 0.0
        with unittest.mock.patch.object(urllib.request, "urlopen", side_effect=_fake):
            old = backup.lookup("BV1xx411c7mD", db_path=db)
            after_retry = len(calls)
            # 重试后库里是刚写的新记录；手动改成 1 天前，测未过期的 miss
            _put(db, "BV1xx411c7mD", 2, 0, now - DAY)
            fresh = backup.lookup("BV1xx411c7mD", db_path=db)
            after_fresh = len(calls)
    finally:
        _drop_db(db)
    ok = (old.get("cached") is False and old.get("ok") is False
          and after_retry == 1
          and fresh.get("cached") is True and fresh.get("ok") is False
          and after_fresh == 1)
    return ok, {"old": old, "calls_after_retry": after_retry,
                "fresh": fresh, "calls_after_fresh": after_fresh}


def case_throttle() -> tuple[bool, dict]:
    """把间隔改成 0.3 秒，连发两次：两次请求之间的实际间隔必须 ≥ 0.3。"""
    db = _fresh_db("throttle")
    stamps: list = []
    old_interval = backup.MIN_INTERVAL

    def _fake(url, *a, **k):
        stamps.append(time.monotonic())
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    try:
        _prep(db)
        backup.MIN_INTERVAL = 0.3
        backup._last_at = 0.0
        with unittest.mock.patch.object(urllib.request, "urlopen", side_effect=_fake):
            backup.lookup("BV17x411w7KC", db_path=db)   # miss → 真实请求
            backup.lookup("BV1xx411c7mD", db_path=db)   # 另一个号，必然再请求
    finally:
        backup.MIN_INTERVAL = old_interval
        _drop_db(db)
    elapsed = round(stamps[1] - stamps[0], 4) if len(stamps) == 2 else -1
    ok = len(stamps) == 2 and elapsed >= 0.3
    return ok, {"calls": len(stamps), "elapsed": elapsed}


def case_bad_bvid_no_request() -> tuple[bool, dict]:
    """格式非法：返回 bad_bvid，urlopen 一次都不调。"""
    db = _fresh_db("bad")
    calls: list = []
    try:
        _prep(db)
        with unittest.mock.patch.object(
                urllib.request, "urlopen",
                side_effect=lambda *a, **k: calls.append(a) or _Resp(200)):
            r = backup.lookup("BV123", db_path=db)
    finally:
        _drop_db(db)
    ok = (r.get("error") == "bad_bvid" and r.get("aid") is None
          and r.get("ok") is False and r.get("url") == "" and len(calls) == 0)
    return ok, {"result": r, "calls": len(calls)}


CASES = [
    ("bv2av_known", "BV→AV 已知样例", case_bv2av_known),
    ("bv2av_bad", "BV 号格式校验", case_bv2av_bad),
    ("cache_hit_no_network", "命中缓存不发请求", case_cache_hit_no_network),
    ("miss_ttl", "未命中缓存的 7 天 TTL", case_miss_ttl),
    ("throttle", "两次请求最小间隔", case_throttle),
    ("bad_bvid_no_request", "非法 BV 不发请求", case_bad_bvid_no_request),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== 失效视频备份站回归（{len(CASES)} 条，离线）==")
    all_ok = True
    passed = 0
    for cid, label, fn in CASES:
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, {"error": repr(exc)}
        all_ok = all_ok and ok
        passed += 1 if ok else 0
        print(f"[{cid}] {label} —— {'PASS' if ok else 'FAIL'}"
              f"（{round(time.time() - t0, 2)}s）  {detail}")
    print(f"\n== 汇总: {passed}/{len(CASES)} PASS ==")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
