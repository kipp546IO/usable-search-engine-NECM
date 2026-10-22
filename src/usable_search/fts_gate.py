"""E4-P2c 阈值门：把 `USABLE_SEARCH_FTS=auto` 变成一条可复核的自动判断。

P2b 的开关只认 1/true/on，默认关。P2c 要的是「自动」——由数据规模决定，而不是人手拍：

    songs >= 100_000  或  库 >= 100MB  或  本地 LIKE p95 >= 50ms   任一成立才开

口径（与 P2b 保持一致，失败一律关闭）：
- 环境变量未设 / 无法识别 → off，不发一次查询、不看库（默认负载永远是 LIKE）。
- `auto` → 现取三项证据再判；任一异常按 0 处理（= 不触发），绝不因门自身故障把开关打开。
- 显式 `Index(use_fts=True/False)` 永远优先——本模块只服务环境变量这一条路。
- `store.py` 不动：P2c 只在入口处（`open_index()`）把 auto 解析成确定的布尔值。

证据来源都是真实库，不写死：
- songs：只读连接（`mode=ro`）`SELECT COUNT(*) FROM songs`
- 库容：主库 + 同名 `-wal`（WAL 里可能还有没落盘的数据）
- p95：现场跑 LIKE（与 store._LIKE_SQL 同一条 SQL 形状、同 limit），
  随机取若干真实歌名各跑几次，取 95 分位。样本量小且取的是「临近最大值」，
  所以这个 p95 偏保守——它报出来的数只会偏大，不会偏小。

auto 的结果按 (路径, 大小, mtime_ns) 缓存 60 秒：门是给每个请求前的入口用的，
不能每次构造 Index 都全库扫一遍。

用法（仓库根目录，只读，不建库）:
    PYTHONUTF8=1 PYTHONPATH=src python -m usable_search.fts_gate data/index.sqlite3
退出码：0 正常（无论门开不开）；2 用法错误。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time

ENV_KEY = "USABLE_SEARCH_FTS"

# 三个阈值（口径见 docs/DESIGN.md 的 FTS 启用门槛一节）
SONGS_MIN = 100_000                 # 10 万首
DB_BYTES_MIN = 100 * 1024 * 1024    # 100 MB
P95_MS_MIN = 50.0                   # 本地 LIKE 搜索 p95 50ms

# 与 store._env_fts_flag 相同的真值集合：1/true/on 才开
_TRUE_FLAGS = ("1", "true", "on")
_AUTO_FLAGS = ("auto",)

# auto 证据缓存：(abspath, size, mtime_ns) -> (cached_at, evidence)
_CACHE: dict[tuple, tuple[float, dict]] = {}
CACHE_TTL = 60.0  # 秒

# 与 store._LIKE_SQL 同形状（列序、ESCAPE、ORDER BY 全对齐）；
# 两边一旦漂移，tests/test_fts_gate.py 的 SQL 对齐用例会直接 FAIL。
_LIKE_SQL = (
    "SELECT source, id, title, artists, album, album_id, cover, duration, fee, origin, mine "
    "FROM songs "
    "WHERE title LIKE ? ESCAPE '\\' OR artists LIKE ? ESCAPE '\\' OR album LIKE ? ESCAPE '\\' "
    "ORDER BY fetched_at DESC LIMIT ?"
)

_METRIC_SAMPLES = 5   # 取几条真实歌名当查询
_METRIC_REPS = 3      # 每条跑几次（共 15 个样本）
_METRIC_LIMIT = 60    # 与搜索页第一屏的量级一致


def _mode(raw: str) -> str:
    """把环境变量原值翻成三态：on / off / auto（其它一律 off，失败关闭）。"""
    v = (raw or "").strip().lower()
    if v in _TRUE_FLAGS:
        return "on"
    if v in _AUTO_FLAGS:
        return "auto"
    return "off"


def evaluate(songs: int, db_bytes: int, p95_ms: float) -> dict:
    """纯判断：三项任一过阈值即开。不读环境、不碰库，单测直接调。"""
    songs = max(0, int(songs or 0))
    db_bytes = max(0, int(db_bytes or 0))
    p95_ms = max(0.0, float(p95_ms or 0.0))

    hits = []
    if songs >= SONGS_MIN:
        hits.append("songs")
    if db_bytes >= DB_BYTES_MIN:
        hits.append("db_bytes")
    if p95_ms >= P95_MS_MIN:
        hits.append("p95_ms")

    return {
        "enable": bool(hits),
        "values": {"songs": songs, "db_bytes": db_bytes, "p95_ms": round(p95_ms, 3)},
        "thresholds": {"songs": SONGS_MIN, "db_bytes": DB_BYTES_MIN, "p95_ms": P95_MS_MIN},
        "hit": hits,
    }


def _db_bytes(path: str) -> int:
    """库容 = 主库 + 同名 -wal（存在才算）。取不到按 0。"""
    total = 0
    for candidate in (path, path + "-wal"):
        try:
            total += os.path.getsize(candidate)
        except OSError:
            continue
    return total


def _songs_count(path: str) -> int:
    """只读连接数 songs；库不存在/表不存在/被锁一律 0（= 不触发）。"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("SELECT COUNT(*) FROM songs").fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return 0


def _p95(values: list[float]) -> float:
    """最近秩 p95（ceil(0.95n)-1）。空列表 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return float(ordered[idx])


def measure_p95(path: str, *, samples: int = _METRIC_SAMPLES, reps: int = _METRIC_REPS) -> float:
    """现场量 LIKE：随机真实歌名 × reps 次，返回 p95 毫秒。任何异常返回 0。"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except (sqlite3.Error, OSError):
        return 0.0
    try:
        rows = conn.execute(
            "SELECT title FROM songs WHERE title IS NOT NULL AND length(title) >= 3 "
            "ORDER BY random() LIMIT ?", (max(1, int(samples)),)
        ).fetchall()
        titles = [str(r[0]) for r in rows if r and r[0]]
        if not titles:
            return 0.0
        timings: list[float] = []
        for title in titles:
            like = "%" + title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for _ in range(max(1, int(reps))):
                t0 = time.perf_counter()
                conn.execute(_LIKE_SQL, (like, like, like, _METRIC_LIMIT)).fetchall()
                timings.append((time.perf_counter() - t0) * 1000.0)
        return _p95(timings)
    except (sqlite3.Error, OSError):
        return 0.0
    finally:
        conn.close()


def collect_evidence(path: str, *, cache: bool = True) -> dict:
    """现取三项证据；auto 模式的唯一数据入口。缓存 60 秒。"""
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), int(st.st_size), int(st.st_mtime_ns))
    except OSError:
        key = (os.path.abspath(path), -1, -1)

    now = time.time()
    if cache:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return dict(hit[1])

    evidence = {
        "songs": _songs_count(path),
        "db_bytes": _db_bytes(path),
        "p95_ms": measure_p95(path),
    }
    if cache:
        _CACHE[key] = (now, dict(evidence))
    return evidence


def clear_cache() -> None:
    """测试用：清掉 auto 证据缓存。"""
    _CACHE.clear()


def resolve(db_path: str, *, env=None, evidence: dict | None = None,
            cache: bool = True) -> dict:
    """解析环境变量 → {"mode", "enable", ...}。auto 时附带证据与命中项。

    evidence 参数给测试/离线复核注入用；不传就现取（带缓存）。
    """
    source = os.environ if env is None else env
    raw = "" if source is None else str(source.get(ENV_KEY, "") or "")
    mode = _mode(raw)

    if mode == "on":
        return {"mode": "on", "enable": True, "reason": "env_on", "raw": raw}
    if mode == "off":
        return {"mode": "off", "enable": False, "reason": "default_off" if not raw.strip()
                else "env_off", "raw": raw}

    data = dict(evidence) if evidence is not None else collect_evidence(db_path, cache=cache)
    verdict = evaluate(data.get("songs", 0), data.get("db_bytes", 0), data.get("p95_ms", 0.0))
    return {"mode": "auto", "enable": verdict["enable"], "reason": "threshold_met"
            if verdict["enable"] else "threshold_not_met", "raw": raw, **verdict}


def open_index(db_path: str, *, use_fts: bool | None = None, **kwargs):
    """构造 Index：显式表态最优先；否则 auto 才由门槛决定，on/off 仍交给 store 读环境。

    这是 P2c 唯一的接线点——store.py 一行不动。
    """
    from .store import Index

    if use_fts is None:
        decision = resolve(db_path)
        if decision["mode"] == "auto":
            use_fts = bool(decision["enable"])
    return Index(db_path, use_fts=use_fts, **kwargs)


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python -m usable_search.fts_gate <db_path>", file=sys.stderr)
        return 2
    path = argv[1]
    decision = resolve(path)
    print(json.dumps({"db": path, "env": os.environ.get(ENV_KEY, ""), **decision},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
