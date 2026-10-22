"""项目体检：一条命令验证「仓库基线 + 服务 + 八套回归」。

用法（仓库根目录）：
    python tools/healthcheck.py            # 全量（含最慢的网易云召回）
    python tools/healthcheck.py --quick    # 跳过召回套件

退出码：有 FAIL 返回 1；只有 PASS/SKIP 返回 0。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
ENV = dict(os.environ, PYTHONUTF8="1", PYTHONPATH="src")

SUITES = [
    # space 系接口最容易吃风控，排在搜索套件之前先跑
    ("up", "tests/test_up.py", 300),
    ("archive", "tests/test_archive.py", 300),
    ("netease-paging", "tests/test_netease_paging.py", 120),
    ("netease-dur", "tests/test_netease_dur.py", 300),
    ("netease-pub", "tests/test_netease_pub.py", 120),
    ("netease-priv", "tests/test_netease_priv.py", 120),
    ("bilibili-risk", "tests/test_bilibili_risk.py", 60),
    ("healthcheck-parser", "tests/test_healthcheck.py", 60),
    ("store-cache", "tests/test_store_cache.py", 60),
    ("store-fts", "tests/test_store_fts.py", 60),
    ("store-fts-shadow", "tests/test_store_fts_shadow.py", 60),
    ("store-fts-query", "tests/test_store_fts_query.py", 60),
    ("fts-gate", "tests/test_fts_gate.py", 60),
    ("fuzzy", "tests/test_fuzzy.py", 120),
    ("probe", "tests/test_probe.py", 60),
    ("relevance", "tests/test_relevance.py", 60),
    ("backup", "tests/test_backup.py", 60),
    ("bilibili", "tests/test_bilibili.py", 600),
    ("recall", "tests/test_recall.py", 900),
]
SUITE_GAP = 5  # 套件之间留冷却，降低风控串扰


def sh(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, env=ENV, capture_output=True, check=False,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def git_baseline() -> dict:
    head = sh(["git", "rev-parse", "--short", "HEAD"], 30).stdout.strip()
    status = sh(["git", "status", "--porcelain"], 30).stdout.strip()
    branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], 30).stdout.strip()
    return {"branch": branch, "head": head,
            "clean": not bool(status), "dirty": status.splitlines()[:5]}


def ping() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:18788/api/ping", timeout=5) as r:
            return True, r.read().decode("utf-8", "replace")[:40]
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)[:80]


def run_suite(path: str, timeout: int) -> tuple[str, str]:
    try:
        p = sh([PY, path], timeout)
    except subprocess.TimeoutExpired:
        return "FAIL", "超时"
    tail = (p.stdout or "").strip().splitlines()
    last = tail[-1] if tail else ""
    summary = next((line for line in reversed(tail) if "汇总:" in line), last)
    if p.returncode == 0:
        # 汇总后可能还有“原始结果”路径，不能只看最后一行。
        skip_match = re.search(r"SKIP\s+(\d+)", summary)
        if skip_match and int(skip_match.group(1)) > 0 and "FAIL 0" in summary:
            return "SKIP", summary
        return "PASS", summary
    return "FAIL", summary or (p.stderr or "")[-120:]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过网易云召回套件")
    args = ap.parse_args()

    print("== 项目体检 ==")
    g = git_baseline()
    print(f"分支 {g['branch']} · HEAD {g['head']} · 工作区{'干净' if g['clean'] else '有改动'}")
    if g["dirty"]:
        for line in g["dirty"]:
            print("   ", line)

    ok, msg = ping()
    print("服务 18788:", "OK" if ok else "DOWN", msg)

    fails = 0
    results = []
    for name, path, timeout in SUITES:
        if args.quick and name == "recall":
            print(f"[{name}] SKIP（--quick）")
            results.append((name, "SKIP", ""))
            continue
        t0 = time.time()
        status, detail = run_suite(path, timeout)
        results.append((name, status, detail))
        fails += 1 if status == "FAIL" else 0
        print(f"[{name}] {status}（{round(time.time()-t0,1)}s）  {detail}")
        if name != SUITES[-1][0]:
            time.sleep(SUITE_GAP)

    print()
    summary = " · ".join(f"{n}:{s}" for n, s, _ in results)
    print("汇总:", summary)
    print("结论:", "基线可用" if fails == 0 and ok else "有问题，先修再用")
    return 0 if fails == 0 and ok else 1


if __name__ == "__main__":
    sys.exit(main())
