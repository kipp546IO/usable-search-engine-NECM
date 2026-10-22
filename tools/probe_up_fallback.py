"""UP主投稿备用源探针（只读，不改主流程）。

背景：`x/space/wbi/arc/search`（UP投稿主入口）是 IP 级风控，高频访问会 -352。
本脚本只**读**网络接口，不写索引/治理/档案，也不改采集主流程：

- 控制组：`x/space/wbi/arc/search`（WBI 签名，与 `bilibili.user_videos` 同参数）
- 备用 A：`x/web-interface/newlist`（旧版投稿列表，无需签名）
- 备用 B：`x/polymer/web-dynamic/v1/feed/space`（动态流，视频动态里含 BVID）

每个入口只打一次，**不重试**——探针要的是当前真实状态，重试会加重风控。
输出 JSON（HTTP 状态、B站业务码、是否风控、耗时、条数、字段名），
只打印条数与字段名，**不打印标题/UP 名**，避免抓取内容落盘。

用法（仓库根目录）:
    PYTHONUTF8=1 python tools/probe_up_fallback.py --mid <UID>
    PYTHONUTF8=1 python tools/probe_up_fallback.py --mid <UID> --page 2 --out probe.json

退出码：0 = 至少一个备用入口可用；1 = 全部入口不可用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.bilibili.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://www.bilibili.com/",
}

# WBI 打乱表（与 src/usable_search/bilibili.py 的 _MIXIN_TAB 一致；官方前端同款）
_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5,
              49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24,
              55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
              57, 62, 11, 36, 20, 34, 44, 52]
RISK_CODES = {-352, -412, -799}


def _elapsed_ms(t0: float) -> int:
    return int(round((time.perf_counter() - t0) * 1000))


def _get_json(url: str, timeout: float) -> dict:
    """单次 GET，不重试。返回 {http, code, message, payload, ms, error}。"""
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 —— 仅本文件内固定 https 地址
            status = resp.status
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {"http": exc.code, "code": None, "message": "", "payload": {},
                "ms": _elapsed_ms(t0), "error": f"HTTPError {exc.code}"}
    except Exception as exc:  # noqa: BLE001 —— 探针要的是现象，任何网络异常都记下来
        return {"http": None, "code": None, "message": "", "payload": {},
                "ms": _elapsed_ms(t0), "error": type(exc).__name__}
    try:
        payload = json.loads(body)
    except ValueError:
        return {"http": status, "code": None, "message": "", "payload": {},
                "ms": _elapsed_ms(t0), "error": "non-json body"}
    return {"http": status, "code": payload.get("code"), "message": str(payload.get("message") or ""),
            "payload": payload, "ms": _elapsed_ms(t0), "error": ""}


def _wbi_sign(params: dict, timeout: float) -> tuple[str, dict]:
    """取 nav 里的 wbi_img 拼 mixinKey，返回 (签名查询串, nav 探针结果)。"""
    nav = _get_json(BASE + "/x/web-interface/nav", timeout)
    wbi = ((nav.get("payload") or {}).get("data") or {}).get("wbi_img") or {}
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        raise RuntimeError(f"wbi_img 获取失败: {nav.get('code')} {nav.get('error') or nav.get('message')}")
    raw = img + sub
    mixin = "".join(raw[i] for i in _MIXIN_TAB)[:32]
    p = dict(params)
    p["wts"] = int(time.time())
    items = sorted(p.items())
    query = urllib.parse.urlencode(items)
    p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return urllib.parse.urlencode(sorted(p.items())), nav


def _summarise(name: str, path: str, res: dict, items_key: str) -> dict:
    """统一成一条探针结果：HTTP / 业务码 / 风控 / 耗时 / 条数 / 字段名（不含内容）。"""
    payload = res.get("payload") or {}
    data = payload.get("data")
    items: list = []
    if items_key == "vlist":
        items = ((data or {}).get("list") or {}).get("vlist") or [] if isinstance(data, dict) else []
    elif items_key == "archives":
        items = (data or {}).get("archives") or [] if isinstance(data, dict) else []
    elif items_key == "items":
        items = (data or {}).get("items") or [] if isinstance(data, dict) else []
    code = res.get("code")
    risk = (code in RISK_CODES) if isinstance(code, int) else (res.get("http") in (403, 412))
    page = (data or {}).get("page") if isinstance(data, dict) else None
    return {
        "name": name,
        "path": path,
        "http": res.get("http"),
        "code": code,
        "message": (res.get("message") or res.get("error") or "")[:60],
        "risk": bool(risk),
        "ms": res.get("ms"),
        "items": len(items),
        "fields": sorted(items[0].keys())[:24] if items and isinstance(items[0], dict) else [],
        "total": (page or {}).get("count") if isinstance(page, dict) else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="UP投稿备用源探针（只读）")
    ap.add_argument("--mid", type=int, required=True, help="目标 UP 的 UID")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--ps", type=int, default=30, help="每页条数（newlist 上限 50）")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--out", help="把 JSON 写到这个文件（默认只打印到 stdout）")
    ap.add_argument("--pause", type=float, default=1.0, help="入口之间的礼貌间隔（秒）")
    args = ap.parse_args(argv)

    ps = max(1, min(50, args.ps))
    entries: list[dict] = []

    # 控制组：主入口（WBI 签名），用来确认「现在的风控状态」
    try:
        query, nav = _wbi_sign({"mid": args.mid, "pn": args.page, "ps": ps, "order": "pubdate"},
                               args.timeout)
        res = _get_json(f"{BASE}/x/space/wbi/arc/search?{query}", args.timeout)
    except RuntimeError as exc:
        nav = {}
        res = {"http": None, "code": None, "message": str(exc)[:60], "payload": {},
               "ms": 0, "error": str(exc)[:40]}
    entries.append(_summarise("primary_wbi_arc", "/x/space/wbi/arc/search", res, "vlist"))
    entries[-1]["note"] = "控制组：主入口（与 bilibili.user_videos 同参数）"
    if nav:
        entries[-1]["nav"] = {"http": nav.get("http"), "code": nav.get("code"), "ms": nav.get("ms")}
    time.sleep(max(0.0, args.pause))

    # 备用 A：旧版投稿列表（无需签名）
    url = f"{BASE}/x/web-interface/newlist?mid={args.mid}&ps={ps}&pn={args.page}&order=pubdate"
    entries.append(_summarise("fallback_newlist", "/x/web-interface/newlist",
                              _get_json(url, args.timeout), "archives"))
    entries[-1]["note"] = "备用A：旧版投稿列表，返回 archives[]，分页 page{count,num,size}"
    time.sleep(max(0.0, args.pause))

    # 备用 B：动态流（视频动态 DYNAMIC_TYPE_AV 里带 bvid）
    url = f"{BASE}/x/polymer/web-dynamic/v1/feed/space?host_mid={args.mid}&features=itemOpusStyle"
    entries.append(_summarise("fallback_dynamic", "/x/polymer/web-dynamic/v1/feed/space",
                              _get_json(url, args.timeout), "items"))
    entries[-1]["note"] = "备用B：动态流，视频动态里带 bvid"

    ok_fallback = [e["name"] for e in entries[1:]
                   if e["code"] == 0 and not e["risk"] and e["items"] > 0]
    report = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mid": args.mid,
        "page": args.page,
        "page_size": ps,
        "entries": entries,
        "fallback_available": ok_fallback,
        "verdict": "fallback_ok" if ok_fallback else "all_blocked",
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if ok_fallback else 1


if __name__ == "__main__":
    raise SystemExit(main())
