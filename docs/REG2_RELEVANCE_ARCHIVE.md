# REG-2 相关性与档案回归（2026-10-22）

## 基线

- 仓库 HEAD `cdea493`，开工前 `git status` 干净（本轮未碰 `store.py`，它当前也无未提交 diff）。
- `python tools/healthcheck.py --quick`：结论「基线可用」，**FAIL 0**；`up` SKIP（B站风控，6.7s）、`recall` SKIP（--quick 跳过），其余 13 套 PASS。

## 一、相关性（`tests/test_relevance.py`）

**6/6 PASS，退出码 0**，全部 <0.1s：

- `match_field_priority` 命中字段优先级：all→title / desc→desc / tag→tag / none→空串
- `match_field_case` 大小写不敏感：upper_q→title、tag→tag、miss→空串
- `governor_std2` std2 剔除仅标签命中：tag_only→phrase，dropped 1，title_hit None，enabled True
- `governor_std_still_allows_tag` std 档仍放行标签命中：result None，dropped 0，kept 1
- `sort_tag_last` 标签命中稳定降到最后：order `['H1','H2','D1','T1','T2','X1']`
- `platform_dur` 时长区间→平台时长档：`(None,600)→1`、`(600,900)→2`、`(900,1800)→2`、`(1800,3600)→3`、`(3600,7200)→4`、`(7200,None)→4`、`(300,1200)→None`（跨档不映射）、`(None,None)→None`

判定 PASS：v0.4.12 的相关性可见化契约（命中字段判定、std/std2 差异、标签命中降序、平台档映射）全部守住。

## 二、档案/下架（`tests/test_archive.py`）

**3/3 PASS，退出码 0**，合计 3.7s：

- `roundtrip`（0.0s）：写入 2 条 → 统计 total 2 / unknown 2 / last_check 0；按标题查询命中「测试视频·春日影」
- `status`（1.2s）：真实稿件 `BV1Yiz3YxEJM` → ok；伪造 `BV1Yiz3YxEJX` → gone，code -404
- `recheck`（2.5s）：复查 2 条落库，real ok / fake gone，flagged `['BV1Yiz3YxEJX']`

`status` / `recheck` 真实打到了 B站线上并未被风控跳过——同一时段 `up` 通道是 -352，说明 `x/web-interface/view` 仍可用，两套判定互不干扰。

## 三、线上现状（只读，不作为回归条目）

- `GET /api/archive?limit=0`：total 1725 / ok 24 / gone 0 / hidden 0 / error 0 / unknown 1701 / last_check 2026-10-18 20:51:38（+08）
- `data/archive.sqlite3` 的 `seen` 表 1725 行，按 status 分组 ok 24、unknown 1701，与 API 一致；即 **1701 条从未复查**（ISSUES 里记的 1376 条已涨到 1701，下架巡查仍在待办）
- `GET /api/governance?q=春日影`：最近一次 run 2026-10-22 06:37（本次体检 bilibili 套件触发），mode=title，scanned 679、kept 34、dropped push/live/phrase 全 0
- `data/governance.sqlite3` 的 `runs` 表 497 行

## 结论

PASS。三件验收全绿：relevance 6/6、archive 3/3、`healthcheck --quick` FAIL 0（up SKIP 风控、recall SKIP --quick）。本轮零代码改动。
