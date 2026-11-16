"""SQLite 存储：批次、流水行、候选、匹配。

约定：
- 金额与手续费一律存 Decimal 字符串，不落 float；
- 一次运行的所有行在同一个事务里写入，失败整体回滚；
- 同一 run_id 重跑先删后插，行数、合计与首跑一致；
- 大整数编号（流水号、凭证号）按字符串保形，不过 int()。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    bank_sha256 TEXT NOT NULL DEFAULT '',
    ledger_sha256 TEXT NOT NULL DEFAULT '',
    bank_file TEXT NOT NULL DEFAULT '',
    ledger_file TEXT NOT NULL DEFAULT '',
    bank_bytes INTEGER NOT NULL DEFAULT 0,
    ledger_bytes INTEGER NOT NULL DEFAULT 0,
    bank_raw INTEGER NOT NULL DEFAULT 0,
    ledger_raw INTEGER NOT NULL DEFAULT 0,
    bank_valid INTEGER NOT NULL DEFAULT 0,
    ledger_valid INTEGER NOT NULL DEFAULT 0,
    bank_quar INTEGER NOT NULL DEFAULT 0,
    ledger_quar INTEGER NOT NULL DEFAULT 0,
    segments INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'empty',
    input_states_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS records (
    run_id TEXT NOT NULL,
    side TEXT NOT NULL,
    seq INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    txn_no TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT '',
    direction INTEGER NOT NULL DEFAULT 0,
    amount TEXT NOT NULL DEFAULT '',
    fee TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'valid',
    reason TEXT NOT NULL DEFAULT '',
    flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '[]',
    raw_amount TEXT NOT NULL DEFAULT '',
    raw_date TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id, side, status);
CREATE TABLE IF NOT EXISTS candidates (
    run_id TEXT NOT NULL,
    cand_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    diff TEXT NOT NULL,
    day_gap INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id, tier);
CREATE TABLE IF NOT EXISTS matches (
    run_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    diff TEXT NOT NULL,
    day_gap INTEGER NOT NULL DEFAULT 0,
    fee_alloc TEXT NOT NULL DEFAULT '0',
    fee_actual TEXT NOT NULL DEFAULT '0',
    bank_signed TEXT NOT NULL DEFAULT '0',
    ledger_signed TEXT NOT NULL DEFAULT '0'
);
CREATE INDEX IF NOT EXISTS idx_matches_run ON matches(run_id, tier);
CREATE TABLE IF NOT EXISTS settlement_plans (
    plan_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    source_digest TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'posted',
    created_at TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    max_legs INTEGER NOT NULL DEFAULT 8,
    rule_version TEXT NOT NULL DEFAULT ''
);
-- 一个批次只允许一种核销策略落地，避免同一批流水被两套方案各记一次。
CREATE UNIQUE INDEX IF NOT EXISTS idx_settlement_plan_run
    ON settlement_plans(run_id) WHERE status = 'posted';
CREATE TABLE IF NOT EXISTS settlement_allocations (
    plan_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    account TEXT NOT NULL,
    direction INTEGER NOT NULL,
    bank_txn_no TEXT NOT NULL,
    ledger_txn_no TEXT NOT NULL,
    amount TEXT NOT NULL,
    bank_original TEXT NOT NULL,
    ledger_original TEXT NOT NULL,
    bank_posted_at TEXT NOT NULL DEFAULT '',
    ledger_posted_at TEXT NOT NULL DEFAULT '',
    carryover INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_settlement_alloc_bank
    ON settlement_allocations(account, bank_txn_no);
CREATE INDEX IF NOT EXISTS idx_settlement_alloc_ledger
    ON settlement_allocations(account, ledger_txn_no);
-- 凭证登记：每张进过核销视野的凭证都留一条原始额。没有任何分录的「整笔等待」
-- 凭证也必须留档，否则下一期无从知道它还欠着尾款。
CREATE TABLE IF NOT EXISTS settlement_vouchers (
    account TEXT NOT NULL,
    txn_no TEXT NOT NULL,
    original TEXT NOT NULL,
    direction INTEGER NOT NULL DEFAULT 0,
    posted_at TEXT NOT NULL DEFAULT '',
    first_run_id TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, txn_no, original)
);
CREATE INDEX IF NOT EXISTS idx_settlement_voucher_txn
    ON settlement_vouchers(account, txn_no);
"""

RUN_COLUMNS = (
    "run_id", "created_at", "rule_version", "params_json", "bank_sha256", "ledger_sha256",
    "bank_file", "ledger_file", "bank_bytes", "ledger_bytes", "bank_raw", "ledger_raw",
    "bank_valid", "ledger_valid", "bank_quar", "ledger_quar", "segments", "status",
    "input_states_json", "duration_ms",
)


def _plan_id(plan: dict, attempt: int) -> str:
    """计划号由批次、策略、方案指纹与落库尝试次数共同决定，冲突时仍可区分。"""
    seed = f"{plan['run_id']}|{plan['policy']}|{plan['source_digest']}|{attempt}"
    return "sp-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ---------- 批次 ----------
    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["params"] = json.loads(data.pop("params_json") or "{}")
        data["input_states"] = json.loads(data.pop("input_states_json") or "{}")
        return data

    def latest_run(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return self._run_row(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def save_run(self, run: dict, records: list[dict], candidates: list[dict],
                 matches: list[dict]) -> None:
        params_json = json.dumps(run.get("params", {}), ensure_ascii=False, sort_keys=True)
        states_json = json.dumps(run.get("input_states", {}), ensure_ascii=False)
        with self._conn() as conn:
            run_id = run["run_id"]
            conn.execute("DELETE FROM records WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM candidates WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM matches WHERE run_id = ?", (run_id,))
            conn.execute(
                """INSERT OR REPLACE INTO runs (
                    run_id, created_at, rule_version, params_json, bank_sha256, ledger_sha256,
                    bank_file, ledger_file, bank_bytes, ledger_bytes, bank_raw, ledger_raw,
                    bank_valid, ledger_valid, bank_quar, ledger_quar, segments, status,
                    input_states_json, duration_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, run["created_at"], run["rule_version"], params_json,
                    run.get("bank_sha256", ""), run.get("ledger_sha256", ""),
                    run.get("bank_file", ""), run.get("ledger_file", ""),
                    run.get("bank_bytes", 0), run.get("ledger_bytes", 0),
                    run.get("bank_raw", 0), run.get("ledger_raw", 0),
                    run.get("bank_valid", 0), run.get("ledger_valid", 0),
                    run.get("bank_quar", 0), run.get("ledger_quar", 0),
                    run.get("segments", 0), run.get("status", "empty"),
                    states_json, run.get("duration_ms", 0),
                ),
            )
            conn.executemany(
                """INSERT INTO records (run_id, side, seq, row_no, txn_no, account, posted_at,
                    direction, amount, fee, memo, counterparty, status, reason, flags_json,
                    raw_json, raw_amount, raw_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, r["side"], r["seq"], r["row_no"], r["txn_no"], r.get("account", ""),
                        r.get("posted_at", ""), r.get("direction", 0), str(r.get("amount", "")),
                        str(r.get("fee", "")), r.get("memo", ""), r.get("counterparty", ""),
                        r.get("status", "valid"), r.get("reason", ""),
                        json.dumps(r.get("flags", []), ensure_ascii=False),
                        json.dumps(r.get("raw", []), ensure_ascii=False),
                        str(r.get("raw_amount", "")), str(r.get("raw_date", "")),
                    )
                    for r in records
                ],
            )
            conn.executemany(
                """INSERT INTO candidates (run_id, cand_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap) VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, c["cand_id"], c["tier"],
                        json.dumps(c["bank_ids"], ensure_ascii=False),
                        json.dumps(c["ledger_ids"], ensure_ascii=False),
                        str(c["bank_sum"]), str(c["ledger_sum"]), str(c["diff"]), c["day_gap"],
                    )
                    for c in candidates
                ],
            )
            conn.executemany(
                """INSERT INTO matches (run_id, match_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap, fee_alloc, fee_actual,
                    bank_signed, ledger_signed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, m["match_id"], m["tier"],
                        json.dumps(m["bank_ids"], ensure_ascii=False),
                        json.dumps(m["ledger_ids"], ensure_ascii=False),
                        str(m["bank_sum"]), str(m["ledger_sum"]), str(m["diff"]), m["day_gap"],
                        str(m.get("fee_alloc", "0")), str(m.get("fee_actual", "0")),
                        str(m.get("bank_signed", "0")), str(m.get("ledger_signed", "0")),
                    )
                    for m in matches
                ],
            )

    def set_status(self, run_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))

    # ---------- 行 ----------
    def records(self, run_id: str, side: str | None = None,
                status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM records WHERE run_id = ?"
        args: list = [run_id]
        if side:
            sql += " AND side = ?"
            args.append(side)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY side, row_no, seq"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["flags"] = json.loads(item.pop("flags_json") or "[]")
            item["raw"] = json.loads(item.pop("raw_json") or "[]")
            out.append(item)
        return out

    def quarantine(self, run_id: str) -> list[dict]:
        return self.records(run_id, status="quarantine")

    def candidates(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE run_id = ? ORDER BY tier, cand_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def matches(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM matches WHERE run_id = ? ORDER BY tier, match_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def replace_candidates(self, run_id: str, candidates: list[dict]) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM candidates WHERE run_id = ?", (run_id,))
            conn.executemany(
                """INSERT INTO candidates (run_id, cand_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap) VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, c["cand_id"], c["tier"],
                        json.dumps(c["bank_ids"], ensure_ascii=False),
                        json.dumps(c["ledger_ids"], ensure_ascii=False),
                        str(c["bank_sum"]), str(c["ledger_sum"]), str(c["diff"]), c["day_gap"],
                    )
                    for c in candidates
                ],
            )

    def replace_matches(self, run_id: str, matches: list[dict]) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM matches WHERE run_id = ?", (run_id,))
            conn.executemany(
                """INSERT INTO matches (run_id, match_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap, fee_alloc, fee_actual,
                    bank_signed, ledger_signed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, m["match_id"], m["tier"],
                        json.dumps(m["bank_ids"], ensure_ascii=False),
                        json.dumps(m["ledger_ids"], ensure_ascii=False),
                        str(m["bank_sum"]), str(m["ledger_sum"]), str(m["diff"]), m["day_gap"],
                        str(m.get("fee_alloc", "0")), str(m.get("fee_actual", "0")),
                        str(m.get("bank_signed", "0")), str(m.get("ledger_signed", "0")),
                    )
                    for m in matches
                ],
            )

    # ---------- 核销台账 ----------
    def posted_plan(self, run_id: str) -> dict | None:
        """该批次已落地的核销计划；没有则返回 None。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM settlement_plans WHERE run_id = ? AND status = 'posted'",
                (run_id,),
            ).fetchone()
        return self._plan_row(row) if row else None

    def get_plan(self, plan_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM settlement_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return self._plan_row(row) if row else None

    def list_plans(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM settlement_plans ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._plan_row(row) for row in rows]

    def prior_allocations(self, exclude_plan_id: str | None = None) -> list[dict]:
        """所有已落地计划的分录；用于延续尾款，确保历史金额不被再次核销。"""
        sql = ("SELECT a.* FROM settlement_allocations a "
               "JOIN settlement_plans p ON p.plan_id = a.plan_id "
               "WHERE p.status = 'posted'")
        args: list = []
        if exclude_plan_id:
            sql += " AND a.plan_id != ?"
            args.append(exclude_plan_id)
        sql += " ORDER BY a.created_at, a.plan_id, a.seq"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def register_vouchers(self, run_id: str, vouchers: list[dict], created_at: str) -> None:
        """登记本批次的每张有效凭证；已存在则保持首次登记的原值。"""
        with self._conn() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO settlement_vouchers (
                    account, txn_no, original, direction, posted_at,
                    first_run_id, first_seen_at
                ) VALUES (?,?,?,?,?,?,?)""",
                [
                    (
                        row["account"], row["txn_no"], str(row["amount"]),
                        int(row["direction"]), row.get("posted_at", ""),
                        run_id, created_at,
                    )
                    for row in vouchers
                ],
            )

    def registered_vouchers(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM settlement_vouchers ORDER BY posted_at, txn_no"
            ).fetchall()
        return [dict(row) for row in rows]

    def allocations(self, plan_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM settlement_allocations WHERE plan_id = ? ORDER BY seq",
                (plan_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def post_plan(self, plan: dict, vouchers: list[dict] | None = None) -> tuple[dict, bool]:
        """原子记账：凭证登记 + 计划 + 全部分录一次提交。

        幂等键是 (run_id, policy, source_digest)：
        - 同一批次已落地同一方案 → 直接返回已有计划，created=False；
        - 同一批次已落地不同方案 → 抛 DomainError，绝不重复记账；
        - 两个请求同时到达 → 部分唯一索引只放行一个，落败方回读首次结果；
        - 中途失败 → 事务回滚，不留半份分录或半份登记，重试从头再来。
        """
        from .errors import DomainError

        with self._conn() as conn:
            existing = self._posted_for(conn, plan["run_id"])
            if existing is not None:
                return self._same_or_conflict(existing, plan)

            attempt = conn.execute(
                "SELECT COUNT(*) AS n FROM settlement_plans WHERE run_id = ?",
                (plan["run_id"],),
            ).fetchone()["n"] + 1
            plan_id = plan.get("plan_id") or _plan_id(plan, attempt)
            try:
                if vouchers:
                    conn.executemany(
                        """INSERT OR IGNORE INTO settlement_vouchers (
                            account, txn_no, original, direction, posted_at,
                            first_run_id, first_seen_at
                        ) VALUES (?,?,?,?,?,?,?)""",
                        [
                            (
                                row["account"], row["txn_no"], str(row["amount"]),
                                int(row["direction"]), row.get("posted_at", ""),
                                plan["run_id"], plan["created_at"],
                            )
                            for row in vouchers
                        ],
                    )
                conn.execute(
                    """INSERT INTO settlement_plans (
                        plan_id, run_id, policy, source_digest, summary_json, status,
                        created_at, posted_at, max_legs, rule_version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        plan_id, plan["run_id"], plan["policy"],
                        plan["source_digest"],
                        json.dumps(plan["summary"], ensure_ascii=False, sort_keys=True),
                        "posted", plan["created_at"], plan["created_at"],
                        plan["max_legs"], plan.get("rule_version", ""),
                    ),
                )
                conn.executemany(
                    """INSERT INTO settlement_allocations (
                        plan_id, seq, run_id, policy, account, direction, bank_txn_no,
                        ledger_txn_no, amount, bank_original, ledger_original,
                        bank_posted_at, ledger_posted_at, carryover, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            plan_id, item["seq"], plan["run_id"], plan["policy"],
                            item["account"], int(item["direction"]), item["bank_txn_no"],
                            item["ledger_txn_no"], str(item["amount"]),
                            str(item["bank_original"]), str(item["ledger_original"]),
                            item.get("bank_posted_at", ""), item.get("ledger_posted_at", ""),
                            1 if item.get("carryover") else 0,
                            plan["created_at"],
                        )
                        for item in plan["allocations"]
                    ],
                )
            except sqlite3.IntegrityError:
                # 并发请求抢先落库：回滚本次写入，回读首次结果。
                conn.rollback()
                winner = self._posted_for(conn, plan["run_id"])
                if winner is not None:
                    return self._same_or_conflict(winner, plan)
                raise
            row = conn.execute(
                "SELECT * FROM settlement_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return self._plan_row(row), True

    @staticmethod
    def _posted_for(conn, run_id: str):
        return conn.execute(
            "SELECT * FROM settlement_plans WHERE run_id = ? AND status = 'posted'",
            (run_id,),
        ).fetchone()

    def _same_or_conflict(self, existing, plan: dict) -> tuple[dict, bool]:
        from .errors import DomainError

        if (existing["policy"] == plan["policy"]
                and existing["source_digest"] == plan["source_digest"]):
            return self._plan_row(existing), False
        raise DomainError(
            "E_PLAN_CONFLICT",
            f"批次 {plan['run_id']} 已按「{existing['policy']}」核销；"
            "换策略前请先撤销该计划，同一批流水不能记两次",
            409,
        )

    def revoke_plan(self, plan_id: str) -> bool:
        """撤销（标记作废）一份计划；分录保留为审计证据，不再参与余额计算。"""
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE settlement_plans SET status = 'revoked' "
                "WHERE plan_id = ? AND status = 'posted'",
                (plan_id,),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _plan_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["summary"] = json.loads(data.pop("summary_json") or "{}")
        return data
