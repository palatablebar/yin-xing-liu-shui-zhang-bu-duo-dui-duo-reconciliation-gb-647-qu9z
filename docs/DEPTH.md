# 深度登记 ledgerbank

- 规模：中型（8 个功能模块 / 8 个页面 / 104 条测试）
- 核心规则：方向相同、金额差 ≤ 0.02 元、日期差 ≤ 3 天的候选；一对一、一对多、多对一按层级与金额差确定性择一；多对一用最多 4 条的受限子集和；手续费单独计入银行侧并按最大余额法分摊。
- 使用者与工作流：连锁零售财务共享组会计每月把 12 个账户的银行流水与账簿凭证放在一起核对；一笔付款拆多张凭证、多笔流水合并付一张发票，原先逐条手工比对，现在一次运行得到匹配组、未匹配清单、隔离证据与两侧守恒。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 流水采信 | 导入银行流水或账簿凭证，看到编码、分段与逐行诊断 | POST /api/statements/import、页面 / | `app/modules/m1_ingest/reader.py` `app/modules/m1_ingest/decode.py` `app/modules/m1_ingest/segments.py` | `tests/test_m1_ingest.py` |
| M2 金额日期规则 | 查看列定义、借贷符号、容差与日期窗口 | GET /api/contract | `app/modules/m2_contract/contract.py` `app/modules/m2_contract/coerce.py` | `tests/test_m2_contract.py` |
| M3 质量隔离 | 按原因码看隔离行并导出 CSV | GET /api/quarantine、页面 /quarantine | `app/modules/m3_quality/rules.py` `app/modules/m3_quality/quarantine.py` | `tests/test_m3_quality.py` |
| M4 候选匹配 | 生成一对一、一对多、多对一候选 | POST /api/candidates、页面 /candidates | `app/modules/m4_candidates/candidate.py` `app/modules/m4_candidates/window.py` | `tests/test_m4_candidates.py` |
| M5 多对多求解 | 求解不重叠匹配组，看手续费分摊 | POST /api/matches、页面 /matches | `app/modules/m5_matching/subset.py` `app/modules/m5_matching/fees.py` `app/modules/m5_matching/solve.py` | `tests/test_m5_matching.py` |
| M6 差异与批次 | 看两侧守恒、差异归因与历史批次 | GET /api/differences、GET /api/batches、页面 /batches | `app/modules/m6_diff/differences.py` `app/modules/m6_diff/history.py` | `tests/test_m6_differences.py` |
| M7 报告导出 | 导出报告 CSV 与运行清单 JSON | GET /api/export/report.csv、页面 /export | `app/modules/m7_export/csv_out.py` `app/modules/m7_export/manifest_out.py` | `tests/test_m7_export.py` |
| M8 核销记账 | 比较整笔等待与部分核销，记账、跟踪尾款、跨期延续 | GET /api/settlement/quote、POST /api/settlement、页面 /settlement | `app/modules/m8_settlement/model.py` `app/modules/m8_settlement/service.py` | `tests/test_m8_settlement.py` |

端到端：`tests/test_e2e.py`（样例 → 采信 → 质量 → 候选 → 匹配 → 核销 → 差异 → 导出，含 7 个页面 200、幂等重跑与失败输入恢复）；不变量：`tests/test_invariants.py`（含核销金额恒等、重跑不重复记账、流水行不被改写）。

## 核销设计取舍（M8）

| 取舍 | 选择 | 理由与代价 |
|---|---|---|
| 组合来源 | 直接复用 M5 匹配组，核销层不再算一套 | 全系统只有一处组合真相；代价是核销无法修正 M5 的配对，只能记账或不记 |
| 核销层职责 | 只做记账、留尾款、跨期延续、幂等 | 与匹配解耦，原始流水保持只读 |
| 整笔 vs 部分 | 两种策略都保留，记账前并排比较 | 出纳要尽快记账选 `partial`；审计要整齐无尾款选 `strict`；差别只有「剩余额度是否落地」一处 |
| 整笔等待的代价 | 会产生尾款的整组暂缓，本次记账少 | 用旧尾款换新尾款，避免用一笔结算撕碎多张凭证 |
| 部分核销的代价 | 会留下尾款，需下一期继续结 | 一次结算最多补一张凭证，碎片有上限 |
| 拆分上限 | 一张凭证最多被 `partial_max_legs` 次结算承担 | 防止小额结算把大凭证切成无法复核的碎片 |
| 容差 | 只提示「剩余已在容差内」，不自动抹尾 | 原始 − 已核销 = 剩余恒等，尾差正是要监督的对象 |
| 幂等键 | 批次 + 策略 + 方案指纹，部分唯一索引 | 同一批重试不重复记账；换策略须先撤销，避免一水两记 |
| 跨期延续 | 凭证登记表 + 按业务身份汇总已核销额 | 「整笔等待」中一分未记的凭证下期仍出现；未用尽的结算款不自动跨期配对，避免绕开 M5 另造组合 |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-06 | 金额写成 `1,200.50`、`２，１００．００` | `Decimal()` 直接抛错或把全角字符当单位 | NFKC 全角转半角、去千分位与币种符号、括号负数，再 Decimal 到分 | `app/modules/m2_contract/coerce.py` | `tests/test_m2_contract.py::test_thousands_and_fullwidth_amount` |
| TRAP-07 | 金额列出现 `-` / `N/A` / 空串 | 被当成 0 参与合计，合计虚低 | 哨兵表集中定义，金额返回 None 并进隔离，手续费哨兵按 0 计 | `app/config.py`、`app/modules/m2_contract/coerce.py` | `tests/test_m2_contract.py::test_sentinel_amount_returns_none` |
| TRAP-09 | 流水号 9007199254740993 超过 2^53 | JSON 往返或 CSV 输出变成科学计数法 | 业务号全程按字符串保存，导出逐字保留 | `app/modules/m7_export/csv_out.py`、`app/store.py` | `tests/test_m7_export.py::test_big_txn_no_scientific_notation` |
| TRAP-10 | 22 笔金额用 float 累加 | 合计与逐笔复算差 0.01 | 金额一律 Decimal，合计用同一条路径量化到分 | `app/modules/m5_matching/solve.py`、`app/modules/m6_diff/differences.py` | `tests/test_m5_matching.py::test_bank_signed_total_matches_decimal_sum` |
| TRAP-11 | 同一时刻写成 `2026-03-03T10:00:00+08:00` 与 `2026-03-03T02:00:00Z` | 落进两个日期桶，日期差多一天 | 先归一 UTC 再派生本地日；纯日期按 +08:00 当日 00:00 | `app/modules/m2_contract/coerce.py` | `tests/test_m2_contract.py::test_instant_offsets_collapse_to_same_utc` |
| TRAP-14 | 同一流水号两条不同金额（1250.50 / 999.00） | 后写覆盖，匹配金额随文件顺序变化 | 保留发生时间最早一条，时间相同取行号小者，其余进冲突隔离 | `app/modules/m3_quality/rules.py` | `tests/test_m3_quality.py::test_duplicate_txn_keeps_earliest` |
| TRAP-16 | 文件末行没有换行且列数不足 | 半行被当成有效流水 | 末行缺列且无换行时判 `TRUNCATED_TAIL` 进隔离 | `app/modules/m1_ingest/segments.py` | `tests/test_m1_ingest.py::test_truncated_tail_rejected` |
| TRAP-18 | 0 字节 / 仅表头 / 全空行 | 都返回「成功 0 行」，用户不知道文件有没有内容 | 三种空态分别返回 `EMPTY_FILE`、`HEADER_ONLY`、`ALL_BLANK` | `app/modules/m1_ingest/reader.py` | `tests/test_m1_ingest.py::test_empty_states_are_three_outcomes` |
| TRAP-19 | 同一对流水同时命中一对一与子集候选 | 结果取决于候选生成顺序 | 层级优先 + 金额差 + 日期差 + 业务号排序，输入顺序变化不影响结果 | `app/modules/m5_matching/subset.py` | `tests/test_m5_matching.py::test_result_independent_of_candidate_order` |
| TRAP-23 | 日期差正好 3 天与 4 天；23:59:59 与次日 00:00 | 端点重复计入或跨日归错桶 | 本地日桶左闭右开 `[日初, 次日初)`；窗口按自然日差 ≤3 | `app/modules/m4_candidates/window.py` | `tests/test_m4_candidates.py::test_window_boundary_three_days_included` |
| TRAP-24 | 手续费按比例分摊后合计差一分 | 各自四舍五入，分摊合计不等于实际合计 | 最大余额法分到 0.01 元，按组号确定性分配尾差 | `app/modules/m5_matching/fees.py` | `tests/test_m5_matching.py::test_fee_allocation_sums_to_actual` |
| TRAP-31 | 同一批流水重复点「记账」，或中断后重试 | 分录写两遍，分户账虚增 | 批次 + 策略 + 方案指纹做幂等键，部分唯一索引兜底，并发落败方回读首次结果 | `app/store.py`、`app/modules/m8_settlement/service.py` | `tests/test_m8_settlement.py::test_post_then_retry_is_idempotent`、`::test_concurrent_posts_write_once` |
| TRAP-32 | 记账写到一半进程挂掉 | 半份分录留在库里，余额对不上 | 计划与全部分录同一事务提交，失败整体回滚，重试从头再来 | `app/store.py` | `tests/test_m8_settlement.py::test_failed_post_rolls_back_and_retry_posts_once` |
| TRAP-33 | 尾款被「四舍五入抹平」，复核人看不到 0.01 | 原始额 − 已核销 ≠ 剩余，审计追不回 | 容差只提示不抹尾，恒等式在页面/导出/测试三处校验 | `app/modules/m8_settlement/model.py` | `tests/test_m8_settlement.py::test_remaining_identity_holds`、`test_invariants.py::test_settlement_voucher_identity_and_journal` |
| TRAP-34 | 部分核销把凭证切成几十条碎片 | 复核人要逐条拼回一张凭证 | 整笔优先 + 一次结算最多补一张 + `partial_max_legs` 上限 | `app/modules/m8_settlement/model.py` | `tests/test_m8_settlement.py::test_partial_does_not_steal_rows_from_whole_clear`、`::test_max_legs_bounds_splits` |
| TRAP-35 | 下月只来了尾款，凭证不重导 | 尾款凭空消失或对上期已结清凭证重复记账 | 凭证登记表跨批延续，按业务身份扣减历史已核销额 | `app/modules/m8_settlement/service.py`、`app/store.py` | `tests/test_m8_settlement.py::test_carryover_tail_settled_by_later_period` |
| TRAP-36 | 已核销一次后换策略再记 | 同一批流水被两种方案各记一次 | 每批次只允许一份 posted 计划；换策略须先撤销，否则 409 | `app/store.py`、`app/modules/m8_settlement/handler.py` | `tests/test_m8_settlement.py::test_switch_policy_without_revoke_conflicts` |

## 对账规则

- 银行合计 = 银行匹配金额 + 银行未匹配金额 − 手续费合计。
- 账簿合计 = 账簿匹配金额 + 账簿未匹配金额。
- 银行笔数 = 匹配涉及银行行数 + 未匹配银行行数；账簿同理。
- 手续费分摊合计 = 匹配组实际手续费合计。
- 匹配差额 = 银行匹配金额 − 账簿匹配金额，容差 0.02 元内允许非 0，并在差异清单与页面显式列出。
- 以上五账在 `/api/differences`、页面 `/batches` 与报告 CSV 中同时展示。

## 反例与不变量

- 核销不变量：`tests/test_invariants.py::test_settlement_voucher_identity_and_journal`（金额恒等）、`::test_settlement_rerun_does_not_double_post`（重放不重复）、`::test_settlement_ledger_is_append_only`（原始流水只读）。
- 反例（朴素实现会挂）：`tests/test_m5_matching.py::test_bank_signed_total_matches_decimal_sum`（float 累加对不上分）、`tests/test_m1_ingest.py::test_truncated_tail_rejected`（`readlines()` 会把半行当数据）、`tests/test_m3_quality.py::test_duplicate_txn_keeps_earliest`（后写覆盖会得到 999.00）、`tests/test_m5_matching.py::test_many_to_one_subset_bounded_to_four`（5 笔合并会被错误凑成一笔）、`tests/test_m4_candidates.py::test_window_four_days_excluded`（宽窗口会多配一对）。
- 不变量：`tests/test_invariants.py::test_bank_conservation`（守恒）、`tests/test_invariants.py::test_disjoint_assignment`（一组不占两行）、`tests/test_invariants.py::test_fee_conservation`（分摊守恒）、`tests/test_invariants.py::test_rerun_row_counts_stable`（幂等）、`tests/test_invariants.py::test_quarantine_excluded_from_totals`（隔离不计入合计）。
- 端到端：`tests/test_e2e.py::test_full_pipeline_from_samples`。

## 已知不覆盖

- 凭证科目层拆分不做，只按银行账户与金额对账。
- 多币种不折算；外币金额若带币种代码以外字符会进隔离区，样例只覆盖人民币。
- 手续费只按组内银行金额比例分摊，不区分银行收费与第三方收费。
- 跨月批次不合并，一次运行对应一份月度流水；核销尾款跨批延续，但未用尽的结算款不自动跨期配对。
- 核销不做科目层分摊，也不生成会计凭证号；只记「结算款 → 凭证」的金额对应关系。
