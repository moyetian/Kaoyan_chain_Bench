"""ResultStore：JSONL ↔ SQLite 幂等同步与查询（方案 5.8 / B-19）。

★ 铁律：**SQLite 永远是派生数据，可随时重建**。
- ``write_run`` 把 Run 追加成 ``results/runs/<run_id>.jsonl``（每行一个字段块）。
- ``sync_db`` 从 JSONL 幂等导入（``run_id`` 为主键 upsert），返回新导入条数。
- 同一 JSONL 同步两次 → 行数不变；``rm results/benchmark.db`` 后可完全重建。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .errors import StoreError
from .logger import read_jsonl
from .models import GradeResult, Run, SuiteResult

__all__ = [
    "ResultStore",
    "RUNS_DIRNAME",
    "SUITES_DIRNAME",
    "DB_FILENAME",
    "suite_result_filename",
]

RUNS_DIRNAME = "runs"
SUITES_DIRNAME = "suites"
WORKSPACES_DIRNAME = "workspaces"
DB_FILENAME = "benchmark.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    suite_id      TEXT,
    agent         TEXT,
    agent_version TEXT,
    tag           TEXT,
    task_id       TEXT,
    attempt       INTEGER,
    seed          INTEGER,
    started_at    TEXT,
    ended_at      TEXT,
    latency_ms    INTEGER,
    tool_calls    INTEGER,
    tokens        INTEGER,
    cost_usd      REAL,
    task_success  INTEGER,
    score_total   REAL,
    grader_mode   TEXT,
    usage_source  TEXT,
    usage_estimated INTEGER,
    timed_out     INTEGER,
    payload       TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    run_id      TEXT,
    call_id     TEXT,
    name        TEXT,
    ok          INTEGER,
    duration_ms INTEGER,
    PRIMARY KEY (run_id, call_id)
);
CREATE TABLE IF NOT EXISTS grades (
    run_id      TEXT PRIMARY KEY,
    task_id     TEXT,
    score_total REAL,
    grader_mode TEXT,
    task_success INTEGER,
    metrics_json TEXT,
    payload     TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    run_id         TEXT,
    url            TEXT,
    domain         TEXT,
    evidence_level TEXT,
    PRIMARY KEY (run_id, url)
);
CREATE INDEX IF NOT EXISTS idx_runs_suite_agent_tag ON runs (suite_id, agent, tag);
CREATE INDEX IF NOT EXISTS idx_runs_task_id ON runs (task_id);
CREATE INDEX IF NOT EXISTS idx_grades_task_id ON grades (task_id);
CREATE INDEX IF NOT EXISTS idx_sources_run_id ON sources (run_id);
-- 增量同步状态：记录每个 JSONL 的 (mtime_ns, size)，未变化则跳过重读
CREATE TABLE IF NOT EXISTS sync_state (
    run_id   TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size     INTEGER NOT NULL
);
"""


def suite_result_filename(suite_id: str, agent: str, tag: str) -> str:
    """``<suite_id>__<agent>__<tag>.suite.json``（方案 3.3）。"""
    safe = lambda s: str(s or "unknown").replace("/", "-").replace("\\", "-")  # noqa: E731
    return f"{safe(suite_id)}__{safe(agent)}__{safe(tag)}.suite.json"


class ResultStore:
    """结果存储门面。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.runs_dir = self.root / RUNS_DIRNAME
        self.suites_dir = self.root / SUITES_DIRNAME
        self.workspaces_dir = self.root / WORKSPACES_DIRNAME
        self.db_path = self.root / DB_FILENAME

    # -- 目录 --------------------------------------------------------------
    def ensure_dirs(self) -> None:
        for folder in (self.root, self.runs_dir, self.suites_dir, self.workspaces_dir):
            folder.mkdir(parents=True, exist_ok=True)

    def run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.jsonl"

    # -- 写 JSONL ----------------------------------------------------------
    def write_run(self, run: Run) -> Path:
        """把 Run 追加为 JSONL（append-only）。每行一个「事件」以便崩溃安全。"""
        self.ensure_dirs()
        path = self.run_path(run.run_id)
        with path.open("a", encoding="utf-8") as fh:
            header = {
                "record": "run",
                "run_id": run.run_id,
                "suite_id": run.suite_id,
                "task_id": run.task_id,
                "agent": run.agent,
                "tag": run.tag,
                "attempt": run.attempt,
                "started_at": run.started_at,
            }
            fh.write(json.dumps(header, ensure_ascii=False) + "\n")
            for index, call in enumerate(run.tool_calls_detail or []):
                fh.write(
                    json.dumps(
                        {
                            "record": "tool_call",
                            "run_id": run.run_id,
                            "index": index,
                            "call_id": call.call_id,
                            "name": call.name,
                            "ok": call.ok,
                            "duration_ms": call.duration_ms,
                            "arguments": call.arguments,
                            "result": call.result,
                            "result_sha256": call.result_sha256,
                            "truncated": call.truncated,
                            "error": call.error,
                            "error_code": call.error_code,
                            "started_at": call.started_at,
                            "ended_at": call.ended_at,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            for source in run.sources or []:
                fh.write(
                    json.dumps(
                        {"record": "source", "run_id": run.run_id, **source.to_dict()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            for citation in run.citations or []:
                fh.write(
                    json.dumps(
                        {"record": "citation", "run_id": run.run_id, **citation.to_dict()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            for item in run.errors or []:
                fh.write(
                    json.dumps(
                        {"record": "error", "run_id": run.run_id, **item.to_dict()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            fh.write(
                json.dumps({"record": "run_end", **run.to_dict()}, ensure_ascii=False) + "\n"
            )
            fh.flush()
        return path

    def write_grade(self, grade: GradeResult) -> Path:
        """把评分追加到 run JSONL（同 run 一条 ``grade`` 记录）。"""
        self.ensure_dirs()
        path = self.run_path(grade.run_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"record": "grade", **grade.to_dict()}, ensure_ascii=False) + "\n"
            )
            fh.flush()
        return path

    def read_run(self, run_id: str) -> Run | None:
        """从事务日志重建 Run。"""
        records = read_jsonl(self.run_path(run_id))
        return _run_from_records(records)

    def read_grade(self, run_id: str) -> GradeResult | None:
        records = read_jsonl(self.run_path(run_id))
        for record in reversed(records):
            if record.get("record") == "grade":
                return GradeResult.from_dict(record)
        return None

    def list_run_ids(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(p.stem for p in self.runs_dir.glob("*.jsonl"))

    # -- JSONL → SQLite ----------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.ensure_dirs()
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA)
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise StoreError(f"数据库操作失败（{type(exc).__name__}）") from None
        finally:
            conn.close()

    def sync_db(self) -> int:
        """JSONL → SQLite **幂等增量**导入（``run_id`` 主键 upsert），返回新导入条数。

        以 ``(mtime_ns, size)`` 判断文件是否变化：未变化的 run 直接跳过，不再读内容。
        ``write_grade`` 追加到同一 JSONL 会改变 size/mtime，因此评分仍能同步。
        """
        imported = 0
        with self._connect() as conn:
            existing = {
                row["run_id"] for row in conn.execute("SELECT run_id FROM runs").fetchall()
            }
            known = {
                row["run_id"]: (int(row["mtime_ns"]), int(row["size"]))
                for row in conn.execute("SELECT run_id, mtime_ns, size FROM sync_state").fetchall()
            }
            for run_id in self.list_run_ids():
                path = self.run_path(run_id)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                stamp = (stat.st_mtime_ns, stat.st_size)
                if known.get(run_id) == stamp:
                    continue  # 未变化：跳过重读
                records = read_jsonl(path)
                run = _run_from_records(records)
                if run is None:
                    continue
                grade = None
                for record in reversed(records):
                    if record.get("record") == "grade":
                        grade = GradeResult.from_dict(record)
                        break
                if run_id not in existing:
                    imported += 1
                self._upsert(conn, run, grade, records)
                conn.execute(
                    "INSERT INTO sync_state (run_id, mtime_ns, size) VALUES (?, ?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET "
                    "mtime_ns = excluded.mtime_ns, size = excluded.size",
                    (run_id, stamp[0], stamp[1]),
                )
        return imported

    def _upsert(
        self,
        conn: sqlite3.Connection,
        run: Run,
        grade: GradeResult | None,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        success = None
        if grade is not None:
            success = 1 if grade.metrics.task_success else 0
        elif run.success is not None:
            success = 1 if run.success else 0

        usage = run.usage
        tokens = usage.total_tokens if usage and usage.usage_source != "none" else None
        cost = usage.cost_usd if usage and usage.usage_source != "none" else None

        conn.execute(
            """
            INSERT INTO runs (run_id, suite_id, agent, agent_version, tag, task_id, attempt, seed,
                              started_at, ended_at, latency_ms, tool_calls, tokens, cost_usd,
                              task_success, score_total, grader_mode, usage_source, usage_estimated,
                              timed_out, payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET
                suite_id=excluded.suite_id, agent=excluded.agent, agent_version=excluded.agent_version,
                tag=excluded.tag, task_id=excluded.task_id, attempt=excluded.attempt, seed=excluded.seed,
                started_at=excluded.started_at, ended_at=excluded.ended_at, latency_ms=excluded.latency_ms,
                tool_calls=excluded.tool_calls, tokens=excluded.tokens, cost_usd=excluded.cost_usd,
                task_success=excluded.task_success, score_total=excluded.score_total,
                grader_mode=excluded.grader_mode, usage_source=excluded.usage_source,
                usage_estimated=excluded.usage_estimated, timed_out=excluded.timed_out,
                payload=excluded.payload
            """,
            (
                run.run_id,
                run.suite_id,
                run.agent,
                run.agent_version,
                run.tag,
                run.task_id,
                int(run.attempt or 1),
                run.seed,
                run.started_at,
                run.ended_at,
                int(run.duration_ms or 0),
                int(run.tool_calls or 0),
                tokens,
                cost,
                success,
                grade.score_total if grade else None,
                (grade.grader_mode if grade else run.grader_mode),
                usage.usage_source if usage else "none",
                1 if (usage and usage.estimated) else 0,
                1 if run.timed_out else 0,
                json.dumps(run.to_dict(), ensure_ascii=False),
            ),
        )

        for index, call in enumerate(run.tool_calls_detail or []):
            conn.execute(
                """
                INSERT INTO tool_calls (run_id, call_id, name, ok, duration_ms)
                VALUES (?,?,?,?,?)
                ON CONFLICT(run_id, call_id) DO UPDATE SET
                    name=excluded.name, ok=excluded.ok, duration_ms=excluded.duration_ms
                """,
                (
                    run.run_id,
                    call.call_id or str(index + 1),
                    call.name,
                    1 if call.ok else 0,
                    int(call.duration_ms or 0),
                ),
            )

        # tool_calls 记录（来自 JSONL 明细，覆盖 Agent 未在 Run 内联的情况）
        for record in records:
            if record.get("record") != "tool_call":
                continue
            conn.execute(
                """
                INSERT INTO tool_calls (run_id, call_id, name, ok, duration_ms)
                VALUES (?,?,?,?,?)
                ON CONFLICT(run_id, call_id) DO UPDATE SET
                    name=excluded.name, ok=excluded.ok, duration_ms=excluded.duration_ms
                """,
                (
                    run.run_id,
                    str(record.get("call_id") or record.get("index") or "0"),
                    str(record.get("name") or "unknown"),
                    1 if record.get("ok") else 0,
                    int(record.get("duration_ms") or 0),
                ),
            )
            if record.get("record") == "source":
                conn.execute(
                    """
                    INSERT INTO sources (run_id, url, domain, evidence_level)
                    VALUES (?,?,?,?)
                    ON CONFLICT(run_id, url) DO UPDATE SET
                        domain=excluded.domain, evidence_level=excluded.evidence_level
                    """,
                    (
                        run.run_id,
                        str(record.get("url") or ""),
                        str(record.get("domain") or ""),
                        str(record.get("evidence_level") or "E0"),
                    ),
                )

        for source in run.sources or []:
            conn.execute(
                """
                INSERT INTO sources (run_id, url, domain, evidence_level)
                VALUES (?,?,?,?)
                ON CONFLICT(run_id, url) DO UPDATE SET
                    domain=excluded.domain, evidence_level=excluded.evidence_level
                """,
                (
                    run.run_id,
                    str(source.url or ""),
                    str(source.domain or ""),
                    str(source.evidence_level or "E0"),
                ),
            )

        if grade is not None:
            conn.execute(
                """
                INSERT INTO grades (run_id, task_id, score_total, grader_mode, task_success,
                                    metrics_json, payload)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    task_id=excluded.task_id, score_total=excluded.score_total,
                    grader_mode=excluded.grader_mode, task_success=excluded.task_success,
                    metrics_json=excluded.metrics_json, payload=excluded.payload
                """,
                (
                    run.run_id,
                    grade.task_id or run.task_id,
                    grade.score_total,
                    grade.grader_mode,
                    1 if grade.metrics.task_success else 0,
                    json.dumps(grade.metrics.to_dict(), ensure_ascii=False),
                    json.dumps(grade.to_dict(), ensure_ascii=False),
                ),
            )

    def rebuild_db(self) -> int:
        """删除并重建数据库（``rm benchmark.db`` 的等价操作）。"""
        if self.db_path.exists():
            try:
                self.db_path.unlink()
            except OSError as exc:
                raise StoreError(
                    f"无法删除数据库文件（{exc.strerror or '未知错误'}）"
                ) from None
        return self.sync_db()

    # -- 查询 --------------------------------------------------------------
    def query_runs(
        self,
        suite_id: str | None = None,
        agent: str | None = None,
        tag: str | None = None,
        task_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        *,
        sync: bool = True,
    ) -> list[Run]:
        """按条件查询 Run。默认先做一次幂等同步，保证查询到最新数据。"""
        if sync:
            self.sync_db()
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("suite_id", suite_id),
            ("agent", agent),
            ("tag", tag),
            ("task_id", task_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since:
            clauses.append("started_at >= ?")
            params.append(since)
        if until:
            clauses.append("started_at <= ?")
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT run_id FROM runs{where} ORDER BY started_at, run_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[Run] = []
        for row in rows:
            run = self.read_run(row["run_id"])
            if run is not None:
                out.append(run)
        return out

    def query_grades(self, **kwargs: Any) -> list[tuple[str, GradeResult]]:
        kwargs.setdefault("sync", True)
        runs = self.query_runs(**kwargs)
        out: list[tuple[str, GradeResult]] = []
        for run in runs:
            grade = self.read_grade(run.run_id)
            if grade is not None:
                out.append((run.run_id, grade))
        return out

    def db_counts(self) -> dict[str, int]:
        """各表行数（幂等性验证用）。"""
        with self._connect() as conn:
            return {
                "runs": conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"],
                "tool_calls": conn.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"],
                "grades": conn.execute("SELECT COUNT(*) AS n FROM grades").fetchone()["n"],
                "sources": conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"],
            }

    # -- Suite 结果 --------------------------------------------------------
    def write_suite(self, suite_result: SuiteResult, tag: str) -> Path:
        self.ensure_dirs()
        filename = suite_result_filename(suite_result.suite_id, suite_result.agent, tag)
        path = self.suites_dir / filename
        path.write_text(
            json.dumps(suite_result.to_dict(), ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        return path

    def suite_path(self, suite_id: str, agent: str, tag: str) -> Path:
        return self.suites_dir / suite_result_filename(suite_id, agent, tag)

    def load_suite(self, suite_id: str, agent: str, tag: str) -> SuiteResult:
        path = self.suite_path(suite_id, agent, tag)
        if not path.is_file():
            available = [s.get("tag") for s in self.list_suites(suite_id=suite_id)]
            hint = f"；已有 tag：{', '.join(sorted(t for t in available if t)) or '（无）'}"
            raise StoreError(f"找不到 suite 结果：{suite_id} / {agent} / {tag}{hint}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise StoreError(f"suite 结果文件损坏：{path.name}") from None
        return SuiteResult.from_dict(data)

    def list_suites(
        self,
        *,
        suite_id: str | None = None,
        agent: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出已有 suite 结果（供 compare / 选择 baseline）。"""
        if not self.suites_dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(self.suites_dir.glob("*.suite.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if suite_id and data.get("suite_id") != suite_id:
                continue
            if agent and data.get("agent") != agent:
                continue
            aggregates = data.get("aggregates") or {}
            out.append(
                {
                    "suite_id": data.get("suite_id"),
                    "agent": data.get("agent"),
                    "tag": data.get("tag"),
                    "created_at": data.get("created_at"),
                    "task_count": data.get("task_count"),
                    "runs_per_task": data.get("runs_per_task"),
                    "grader_mode": aggregates.get("grader_mode"),
                    "task_success_rate": aggregates.get("task_success_rate"),
                    "score_mean": aggregates.get("score_mean"),
                    "path": str(path),
                }
            )
        return out

    def latest_tag(self, suite_id: str, agent: str | None = None) -> str | None:
        """按 ``created_at`` 取最新 tag（不依赖文件系统顺序）。"""
        entries = [s for s in self.list_suites(suite_id=suite_id, agent=agent) if s.get("tag")]
        if not entries:
            return None
        entries.sort(key=lambda s: (str(s.get("created_at") or ""), str(s.get("tag"))))
        return str(entries[-1]["tag"])

    def resolve_suite_ref(
        self,
        suite_id: str,
        agent: str | None = None,
        tag: str | None = None,
    ) -> tuple[str, str]:
        """把可能缺省的 ``(agent, tag)`` 解析为**实际存在**的组合。

        解析顺序：显式指定 > 按已有结果反查（``tag`` 缺失取最新；``agent`` 缺失要求候选唯一）。
        无法唯一确定时抛 :class:`StoreError`，不做静默猜测。
        """
        entries = [
            s for s in self.list_suites(suite_id=suite_id) if s.get("agent") and s.get("tag")
        ]
        if not entries:
            raise StoreError(f"suite {suite_id} 没有任何结果；请先 run")

        def _hint(rows: list[dict[str, Any]]) -> str:
            tags = sorted({str(r["tag"]) for r in rows})
            return "；已有 tag：" + (", ".join(tags) if tags else "（无）")

        if agent is None:
            candidates = [s for s in entries if tag is None or str(s.get("tag")) == tag]
            if not candidates:
                raise StoreError(f"suite {suite_id} 下没有 tag={tag} 的结果{_hint(entries)}")
            agents = sorted({str(s["agent"]) for s in candidates})
            if len(agents) > 1:
                raise StoreError(
                    f"suite {suite_id} 下存在多个 agent 的结果：{', '.join(agents)}；请用 --agent 指定"
                )
            agent = agents[0]

        if tag is None:
            candidates = [s for s in entries if str(s["agent"]) == agent]
            if not candidates:
                raise StoreError(
                    f"agent {agent} 在 suite {suite_id} 下没有任何结果{_hint(entries)}"
                )
            candidates.sort(key=lambda s: (str(s.get("created_at") or ""), str(s["tag"])))
            tag = str(candidates[-1]["tag"])

        if not any(str(s["agent"]) == agent and str(s["tag"]) == tag for s in entries):
            raise StoreError(f"找不到 suite 结果：{suite_id} / {agent} / {tag}{_hint(entries)}")
        return agent, tag


def _run_from_records(records: Sequence[Mapping[str, Any]]) -> Run | None:
    """从事务日志重建 :class:`Run`（``run_end`` 优先，其次用 ``run`` 头 + 明细）。

    ``run_end`` 取**最后一条**：同一 JSONL 若被重跑/追加，最新记录才代表当前状态。
    """
    end_record = None
    header: Mapping[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for record in records:
        kind = record.get("record")
        if kind == "run_end":
            end_record = record
        elif kind == "run" and header is None:
            header = record
        elif kind == "tool_call":
            tool_calls.append(record)
        elif kind == "source":
            sources.append(record)
        elif kind == "citation":
            citations.append(record)
        elif kind == "error":
            errors.append(record)

    if end_record is not None:
        data = {k: v for k, v in end_record.items() if k != "record"}
        run = Run.from_dict(data)
    elif header is not None:
        run = Run.from_dict({k: v for k, v in header.items() if k != "record"})
    else:
        return None

    if not run.tool_calls_detail and tool_calls:
        from .models import ToolCall

        run.tool_calls_detail = [
            ToolCall.from_dict({k: v for k, v in rec.items() if k != "record"})
            for rec in tool_calls
        ]
        run.tool_calls = len(run.tool_calls_detail)
    if not run.sources and sources:
        from .models import Source

        run.sources = [
            Source.from_dict({k: v for k, v in rec.items() if k != "record"}) for rec in sources
        ]
    if not run.citations and citations:
        from .models import Citation

        run.citations = [
            Citation.from_dict({k: v for k, v in rec.items() if k != "record"})
            for rec in citations
        ]
    if not run.errors and errors:
        from .models import ErrorItem

        run.errors = [
            ErrorItem.from_dict({k: v for k, v in rec.items() if k != "record"}) for rec in errors
        ]
    return run


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """读取 run JSONL 的全部记录（供调试与测试）。"""
    return read_jsonl(path)


def iterate_suite_results(store: ResultStore) -> Iterable[tuple[str, SuiteResult]]:
    """遍历全部 suite 结果。"""
    for entry in store.list_suites():
        try:
            yield str(entry.get("tag")), store.load_suite(
                str(entry.get("suite_id")), str(entry.get("agent")), str(entry.get("tag"))
            )
        except StoreError:
            continue
