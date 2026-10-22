"""healthcheck 输出解析回归（不访问外网）。"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools.healthcheck as healthcheck


def _run(stdout: str, returncode: int = 0) -> tuple[str, str]:
    original = healthcheck.sh
    healthcheck.sh = lambda *args, **kwargs: SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=""
    )
    try:
        return healthcheck.run_suite("ignored.py", 1)
    finally:
        healthcheck.sh = original


def main() -> int:
    cases = [
        (
            "== 汇总: 10/12 PASS · SKIP 2 · FAIL 0 ==\n"
            "原始结果: tests/last_run_bili.json\n",
            0,
            "SKIP",
        ),
        (
            "== 汇总: 12/12 PASS · SKIP 0 · FAIL 0 ==\n"
            "原始结果: tests/last_run_bili.json\n",
            0,
            "PASS",
        ),
        ("== 汇总: 11/12 PASS · SKIP 0 · FAIL 1 ==\n", 1, "FAIL"),
    ]
    for stdout, returncode, expected in cases:
        status, detail = _run(stdout, returncode)
        assert status == expected, (expected, status, detail)
    print("[healthcheck-parser] PASS 3/3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
