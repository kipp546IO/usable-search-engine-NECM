"""B站失效视频备份站（ilibilib）查询：BV→AV 本地换算 ＋ 本地缓存 ＋ 严格节流.

背景：ilibilib 是个人维护的 GitHub Pages 备份站，按 av 号存 mp4：
    https://terminus2021.github.io/ilibilib/video/av{aid}.mp4
存在返回 200，不存在返回 404。它是个人项目，不是大公司，所以这里：

- BV→AV 纯本地计算（异或＋偏移），不请求任何 B 站接口
- 只发 HEAD，不下载内容；超时 8 秒，绝不重试
- 进程级最小请求间隔 MIN_INTERVAL（1 秒），并发也串行排队
- 命中永久缓存；没命中也缓存，MISS_TTL（7 天）内不重复打扰
- 任何网络异常都内部吞掉，结果落库，绝不向上抛

库在 data/backup.sqlite3（gitignore，不入仓）。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

BACKUP_DB = "data/backup.sqlite3"
BACKUP_BASE = "https://terminus2021.github.io/ilibilib/video/av{aid}.mp4"
MIN_INTERVAL = 1.0          # 两次真实网络请求之间的最小间隔（秒）
MISS_TTL = 7 * 24 * 3600    # 没命中的缓存有效期：7 天后才允许重试
HIT_TTL = 0                 # 命中永久有效（0 = 不过期）
HEAD_TIMEOUT = 8.0          # HEAD 超时（秒）

# 官方 BV 号换算表：位置表 + 异或/偏移常量，纯计算，不做任何请求
_TABLE = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
_S = [11, 10, 3, 8, 4, 6]
_XOR, _ADD = 177451812, 8728348608

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backup (
    bvid TEXT PRIMARY KEY,
    aid  INTEGER,
    ok   INTEGER,
    ts   INTEGER
);
"""

_lock = threading.Lock()
_last_at = 0.0  # 上一次真实请求的 monotonic 时刻


def _conn(db_path: str) -> sqlite3.Connection:
    """打开（必要时建库建表）：父目录自动建，超时 5 秒。"""
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.executescript(_SCHEMA)
    return conn


def bv2av(bv: str) -> int | None:
    """BV 号 → av 号（纯计算）。格式不对返回 None，绝不抛异常。"""
    s = (bv or "").strip()
    if not s.startswith("BV") or len(s) != 12:
        return None
    try:
        r = sum(_TABLE.index(s[i]) * (58 ** n) for n, i in enumerate(_S))
    except ValueError:  # 出现表外字符
        return None
    aid = (r - _ADD) ^ _XOR
    return aid if aid > 0 else None


def _fresh(ok: bool, ts: int, now: int) -> bool:
    """缓存是否还能直接用：命中永久有效，没命中看 MISS_TTL。"""
    if ok:
        return HIT_TTL == 0 or now - ts < HIT_TTL
    return now - ts < MISS_TTL


def _throttle() -> None:
    """保证两次真实请求间隔 ≥ MIN_INTERVAL；拿锁睡觉，天然把并发排成队。"""
    global _last_at
    with _lock:
        while True:
            gap = time.monotonic() - _last_at
            if gap >= MIN_INTERVAL:
                break
            # 多睡 20ms：Windows 的 sleep 可能早醒几毫秒，宁可慢一点也不能超速
            time.sleep(MIN_INTERVAL - gap + 0.02)
        _last_at = time.monotonic()


def _head(url: str) -> bool:
    """发一次 HEAD：2xx 算有，其余（含 404/403/超时）一律算没有。"""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=HEAD_TIMEOUT) as resp:
            code = int(getattr(resp, "status", 0) or getattr(resp, "code", 0) or 0)
            return 200 <= code < 300
    except urllib.error.HTTPError:
        return False  # 404/403 是"没有这个备份"，不是故障：不重试
    except Exception as exc:  # noqa: BLE001 —— 网络问题一律当查不到
        print(f"[backup] head failed: {exc!r}", flush=True)
        return False


def lookup(bvid: str, db_path: str | None = None) -> dict:
    """查一个 BV 号有没有备份。缓存优先，必要时才发一次 HEAD。"""
    bv = (bvid or "").strip()
    aid = bv2av(bv)
    if aid is None:
        return {"bvid": bv, "aid": None, "ok": False, "url": "",
                "cached": False, "error": "bad_bvid"}

    path = db_path or BACKUP_DB
    url = BACKUP_BASE.format(aid=aid)
    now = int(time.time())

    row = None
    try:
        conn = _conn(path)
        try:
            row = conn.execute("SELECT aid, ok, ts FROM backup WHERE bvid=?", (bv,)).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 —— 读缓存失败就当没缓存，继续查
        print(f"[backup] read failed: {exc!r}", flush=True)

    if row is not None:
        r_aid, r_ok, r_ts = int(row[0] or aid), bool(row[1]), int(row[2] or 0)
        if _fresh(r_ok, r_ts, now):
            return {"bvid": bv, "aid": r_aid or aid, "ok": r_ok,
                    "url": url, "cached": True}

    _throttle()
    ok = _head(url)

    try:
        conn = _conn(path)
        try:
            conn.execute("INSERT OR REPLACE INTO backup(bvid, aid, ok, ts) VALUES(?,?,?,?)",
                         (bv, aid, 1 if ok else 0, int(time.time())))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 —— 写缓存失败不影响本次结果
        print(f"[backup] write failed: {exc!r}", flush=True)

    return {"bvid": bv, "aid": aid, "ok": ok, "url": url, "cached": False}
