"""模糊搜索兜底（X3-3）：零依赖 stdlib difflib。

两条腿，都不碰 store.py：
1. 本地库兜底：精确结果为空时，对 songs 表做一次只读候选扫描，
   按相似度挑出候选并给每条打 ``fuzzy=1``。
2. 治理层判据：``Governor`` 的 phrase 规则加 fuzzy 分支——词与标题/简介/标签
   的滑窗相似度达到阈值即判「模糊命中」**保留**（只加保留，不新增剔除）。

开关：``USABLE_SEARCH_FUZZY=1/true/on`` 才开，默认关（与 USABLE_SEARCH_FTS 同风格）；
阈值：``FUZZY_RATIO``（默认 0.66），非法值回落默认。

为什么是滑窗：标题通常比查询长（「【春庭雪】...」），整串 ratio 会被长度稀释，
必须按查询长度在候选文本上滑动取最优窗口。
为什么还要 OSA：difflib ratio 对相邻颠倒（日春影→春日影 vs 初日影）给同分，
排序平局改由「相邻交换算一步」的对齐距离裁决，颠倒类命中才排得上来。
"""
from __future__ import annotations

import difflib
import os

_TRUE_FLAGS = ("1", "true", "on")
DEFAULT_RATIO = 0.66
# 与 store._SELECT_COLS 同一契约（列顺序一致），但这里是独立只读语句，不 import store。
_SELECT_COLS = ("source, id, title, artists, album, album_id, cover, duration, fee, origin, mine")


def enabled() -> bool:
    """USABLE_SEARCH_FUZZY=1/true/on 才开；其它（含未设）一律关。"""
    return os.environ.get("USABLE_SEARCH_FUZZY", "").strip().lower() in _TRUE_FLAGS


def threshold() -> float:
    """相似度阈值：默认 0.66，非法值（非数/超出 (0,1]）回落默认。"""
    try:
        val = float((os.environ.get("FUZZY_RATIO") or "").strip() or DEFAULT_RATIO)
    except ValueError:
        return DEFAULT_RATIO
    return val if 0.0 < val <= 1.0 else DEFAULT_RATIO


def best_window(q: str, text: str) -> tuple[float, str]:
    """查询与文本的最优滑窗：返回 (相似度, 该窗口文本)；完全包含直接 (1.0, 查询)。"""
    ql = (q or "").strip().lower()
    tl = (text or "").lower()
    if not ql or not tl:
        return 0.0, ""
    if ql in tl:
        return 1.0, ql
    n = len(ql)
    if n >= len(tl):
        return difflib.SequenceMatcher(None, ql, tl).ratio(), tl
    sm = difflib.SequenceMatcher()
    sm.set_seq2(ql)                      # 固定查询侧，窗口在长文本上滑
    best, best_win = 0.0, ""
    for i in range(len(tl) - n + 1):
        window = tl[i:i + n]
        if not (set(window) & set(ql)):  # 零字符交集必然低分，省一次精算
            continue
        sm.set_seq1(window)
        if sm.quick_ratio() <= best:     # quick_ratio 是上界：不超当前最优就不精算
            continue
        r = sm.ratio()
        if r > best:
            best, best_win = r, window
            if best >= 0.999:
                break
    return best, best_win


def score(q: str, text: str) -> float:
    """查询与文本的最优滑窗相似度（0~1）。"""
    return best_window(q, text)[0]


def osa(a: str, b: str) -> int:
    """最优字符串对齐距离（相邻字符交换算一步）——纯 stdlib，输入都很短。"""
    la, lb = len(a), len(b)
    if not la:
        return lb
    if not lb:
        return la
    prev2 = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            val = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ai == b[j - 2] and a[i - 2] == b[j - 1]:
                val = min(val, prev2[j - 2] + 1)
            cur[j] = val
        prev2, prev = prev, cur
    return prev[lb]


def fuzzy_score(q: str, rec: dict) -> float:
    """一条记录三列取最高相似度（与 LIKE 三列同口径：title/artists/album）。"""
    return max(score(q, rec.get("title") or ""), score(q, rec.get("artists") or ""),
               score(q, rec.get("album") or ""))


def fallback(conn, q: str, limit: int = 500, min_score: float | None = None) -> list:
    """只读候选扫描：返回按相似度降序的行 dict，每条带 ``fuzzy=1``。

    ``conn`` 由调用方给（Index.conn 或独立只读连接）；本函数只执行 SELECT，
    不建表、不写、不改 store.py。排序键：相似度 → OSA 归一化（颠倒/替换更近者优先）
    → 标题长度 → 标题。
    """
    thr = threshold() if min_score is None else float(min_score)
    ql = (q or "").strip().lower()
    rows = conn.execute(f"SELECT {_SELECT_COLS} FROM songs").fetchall()
    scored = []
    for row in rows:
        rec = dict(row)
        fields = (rec.get("title") or "", rec.get("artists") or "", rec.get("album") or "")
        best, win = max((best_window(q, f) for f in fields), key=lambda t: t[0])
        if best < thr:
            continue
        win = win or ""
        osa_norm = 1.0 - osa(ql, win) / max(1, len(ql) + len(win))
        if osa_norm < thr:      # 位置对齐也过线才算：2/3 字符错位的「假命中」不收
            continue
        rec["fuzzy"] = 1
        rec["fuzzy_score"] = round(best, 4)
        rec["fuzzy_osa"] = round(osa_norm, 4)
        scored.append((best, osa_norm, rec))
    scored.sort(key=lambda t: (-t[0], -t[1], len(t[2].get("title") or ""),
                               t[2].get("title") or ""))
    return [rec for _s, _o, rec in scored[:limit]]
