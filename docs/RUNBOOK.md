# 运行手册 ledgerbank

## 启动与停止

```bash
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:18206/api/health
docker compose down -v --remove-orphans
```

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `ledgerbank-data` 保存批次、流水行、候选与匹配。重建后从「载入样例」重新开始。

## 主流程

1. `POST /api/demo/run`：读取两份脏样例，采信、质量、候选、匹配一次跑完。
2. `GET /api/quarantine`：查看隔离行与原因分布。
3. `GET /api/candidates`：查看已生成候选。
4. `GET /api/matches`：查看匹配组与手续费分摊。
5. `GET /api/differences`：查看两侧守恒与差异归因。
6. `GET /api/export/report.csv` 与 `GET /api/export/manifest.json`：导出。

## 核销记账

```bash
# 1. 并排比较两种策略（记账前先看取舍）
curl -s http://127.0.0.1:18206/api/settlement/quote

# 2. 按策略记账（默认 partial；也可 policy=strict）
curl -s -X POST 'http://127.0.0.1:18206/api/settlement?policy=partial'

# 3. 查看凭证台账：原始 / 往期已核销 / 本次核销 / 剩余 / 承担它的结算
curl -s http://127.0.0.1:18206/api/settlement
```

- 同一方案重复 POST 返回首次计划（`created: false`），不会新增分录。
- 同一批次换策略会返回 409；先撤销旧计划再重记：

```bash
curl -s -X POST http://127.0.0.1:18206/api/settlement/<plan_id>/revoke
```

- 撤销只把计划标记为 `revoked`，分录留作审计证据，不再参与余额。
- 中断后重试直接重复第 2 步即可；计划与分录同一事务，要么全有要么全无。
- 尾款跨月：下月只导银行结算款、或不重导凭证，核销仍会继续结清上期尾款。

## 单步重跑

```bash
curl -s -X POST http://127.0.0.1:18206/api/candidates
curl -s -X POST http://127.0.0.1:18206/api/matches
```

同一输入重跑命中同一 `run_id`，先删后插，行数与合计不变。

## 导入自有文件

```bash
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=bank -F file=@sample_data/bank_statement_dirty.csv
```

支持 UTF-8（含 BOM）、UTF-8 无 BOM、GB18030；列名按 `GET /api/contract` 的别名表映射。

## 失败处理

- 400：参数或文件不可用，页面顶部提示原因，保留已填内容。
- 404：批次或样例不存在，先执行 `POST /api/demo/run`。
- 409：还没有候选就求解匹配，先执行 `POST /api/candidates`；`E_PLAN_CONFLICT` 表示该批次已按另一策略记账，先撤销旧计划。
- 隔离行不是失败：按原因码修正源文件后重新导入。

## 日志

```bash
docker compose logs -f app
```
