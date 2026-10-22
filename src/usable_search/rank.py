"""自定义排序（v0.1）.

回答一个问题：同样的候选里，什么才配排前面。
原则（可读、可改——这是本项目的"排序层"起点）：
1. 标题匹配程度：精确 > 前缀 > 包含 > 其他；
2. 版本分类：原版候选 > 其他版本 > 伴奏 > 翻唱/改编；
3. 平局时短标题优先（越接近完整标题越靠前）。
不透传平台排序。
"""
from __future__ import annotations

KIND_PENALTY = {"原版候选": 0, "其他版本": 40, "伴奏": 140, "翻唱/改编": 260}


def variant_kind(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ("instrumental", "off vocal", "カラオケ", "inst ver", "inst.ver", "伴奏")):
        return "伴奏"
    if any(k in t for k in ("cover", "翻唱", "piano", "钢琴", "改编", "remix", "mashup", "【ai")):
        return "翻唱/改编"
    if any(k in t for k in ("acoustic", "live", "演唱会", "现场")):
        return "其他版本"
    return "原版候选"


def score_record(rec: dict, q: str) -> tuple[int, str]:
    title, ql = rec["title"].lower(), q.lower()
    if title == ql:
        base = 1000
    elif title.startswith(ql):
        base = 700
    elif ql in title:
        base = 500
    else:
        base = 150
    kind = variant_kind(rec["title"])
    return base - KIND_PENALTY[kind], kind


def _pref(rec: dict, prefer_artists) -> int:
    """原唱线索：条目的艺人是否命中「同名专辑的艺人」（去混淆用）。"""
    if not prefer_artists:
        return 0
    artists = rec.get("artists") or ""
    return 1 if any(p and p in artists for p in prefer_artists) else 0


def dedupe(records: list) -> list:
    seen, out = set(), []
    for r in records:
        key = (r.get("source"), r.get("id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def rank(records: list, q: str, prefer_artists=None) -> list:
    """返回 [(score, kind, record), ...]，按自定规则降序。

    排序链（v0.2）：
      1. 分数：标题匹配程度 − 版本类型罚分（原版候选/伴奏/翻唱…）；
      2. mine：在我的歌单/收藏里 ⇒ 置顶（「我歌单里有的，永远排最前」）；
      3. 原唱线索：艺人命中同名专辑艺人 ⇒ 压过同名翻唱；
      4. 平局：短标题优先，再按标题。
    """
    scored = []
    for r in records:
        s, kind = score_record(r, q)
        scored.append((s, kind, r))
    scored.sort(key=lambda x: (
        -x[0],
        -int(x[2].get("mine") or 0),
        -_pref(x[2], prefer_artists),
        len(x[2]["title"]),
        x[2]["title"],
    ))
    return scored
