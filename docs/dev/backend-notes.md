# 后端交付说明（KaoyanBench v1.0）

> 负责人：后端工程师「白客」
> 依据：`docs/01-需求拆解与方案定稿.md`（唯一开发依据）
> 状态：核心引擎与 CLI 全部落地，`pip install -e .` 通过，`pytest tests/` 全绿。

---

## 1. 已实现模块清单

### 1.1 工具层（零第三方依赖）

| 模块 | 职责 |
| --- | --- |
| `src/kaoyanbench/utils/mini_yaml.py` | 受限 YAML 子集解析器（递归下降）：注释 / 标量 / 序列 / 流式 `{}` `[]`；不支持锚点 / 块标量 / 多文档，出错带行号。 |
| `src/kaoyanbench/utils/hashing.py` | `sha256_text` / `sha256_bytes` / `hostname_hash`。 |
| `src/kaoyanbench/utils/text.py` | `get_by_path`（`$.a.b`）/ `normalize` / `coverage_ratio` / `contains_any` / `truncate` / `safe_str`。 |
| `src/kaoyanbench/utils/timex.py` | `utcnow_iso` 等时间工具（**只在 CLI 入口调用**，评分路径不读时钟）。 |

### 1.2 核心引擎 `src/kaoyanbench/core/`

| 模块 | 职责 |
| --- | --- |
| `models.py` | 全部数据模型 + `Serializable` 混入（`to_dict(keep_nulls=)` / `from_dict`，类型标记 `str/int/float/bool/json/dict/list/model/list_of/enum/list_model`）。 |
| `errors.py` | 异常层级 + `ErrorCode` 枚举 + `classify_or_unknown`。 |
| `config.py` | 5.11 配置契约解析、`load_config` / `load_agent_spec` / `load_source_levels` / `load_semantic_config` / `load_suite_manifest` / `check_weights` / `find_project_root`。 |
| `registry.py` | 任务注册表：目录扫描（兼容 `<split>/<category>/<id>` 与 `<category>/<id>` 两种布局）、`load_task`、`validate_tasks`、`validate_fixtures`、过滤 / 索引。 |
| `workspace.py` | 每 task×attempt 独立 workspace：`prepare_workspace` / `cleanup_workspace` / usage & answer 文件约定。 |
| `sandbox.py` | 进程级沙箱：环境变量白名单（**屏蔽 `*_API_KEY`**）、HOME/TMPDIR/XDG 重定向、SIGTERM→grace→SIGKILL。 |
| `runner.py` | `AgentRunner` 协议 + 插件注册表 + **三大铁律**基类；stdout 解析、usage 三级回退。 |
| `runners/{echo,mock,command,http}.py` | 4 个内置 Runner。 |
| `logger.py` | `RunLogger`：JSONL 流式事件（`event` 生命周期 + `record` 明细），`REDACTED` / `truncate_tool_result`。 |
| `checks.py` | 16 种声明式检查（`Check` dataclass + `params`），`run_check` 返回 `CheckResult`（`None` 表示 N/A）。 |
| `evidence.py` | 来源分级规则（E0~E4）、引用支撑判定、年份窗口。 |
| `graders/{deterministic,semantic,hybrid,manual}.py` | 4 种 Grader；`semantic` 含 `build_semantic_client`（无 key→`None`→降级）与 `fallback_semantic_score`。 |
| `grader.py` | Grader 注册表 + `GradeContext` + `run_checks`。 |
| `scorer.py` | 维度聚合、**维度中性再分配**（`reallocated=True`）、全部 neutral → `ScoringError`、`finalize_grade`。 |
| `metrics.py` | `aggregate`（含 `by_category` / `by_difficulty` / `error_counts` / `sample_counts`）、`score_breakdown_mean`。 |
| `evidence.py` / `snapshot.py` | 证据规则与网页快照存储（离线回放）。 |
| `store.py` | JSONL↔SQLite：`write_run` / `write_grade` / `write_suite` / `load_suite` / `sync_db` / `rebuild_db` / `db_counts`。 |
| `regression.py` | Δpp 回归门禁（**绝对值百分点**）、`build_regression_report` / `exit_code_for` / `summarize_verdict`。 |
| `reporter.py` | 5.6 报告编排：`ReportModel` + `Reporter` Protocol + `build_report`（**先落 JSON 事实源**，其余格式懒加载）。 |
| `engine.py` | 编排：`RunSpec` / `run_suite` / `run_single_task` / `build_suite_result` / `grade_existing_run`。 |

### 1.3 报告器

| 模块 | 归属 |
| --- | --- |
| `reporters/json_reporter.py` | **后端**（事实源，必须实现）。 |
| `reporters/json_reporter` 之外的 `markdown/html/csv` | **前端工程师**，后端仅保留 `register_reporter` 注册点；缺失时只告警、不崩。 |

### 1.4 CLI `src/kaoyanbench/cli.py`

子命令：`list / run / grade / report / compare / regression / validate / fixtures build|check / snapshot fetch|verify / suite / agent`。
入口：`kaoyanbench`、`kyb`（`kaoyanbench.cli:main`）。

---

## 2. 数据存储（JSONL ↔ SQLite ↔ 文件树）

- **文件树**（事实源）：`results/runs/<run_id>.jsonl` 流式事件；`results/suites/<suite>__<agent>__<tag>.suite.json`。
- **SQLite**（派生，可重建）：`results/benchmark.db`，`sync_db()` 幂等 upsert；`rebuild_db()` 可从 JSONL 全量重建。
- **workspace**：`results/workspaces/<run_id>/`（成功清理 / 失败保留 / `--keep-workspace` 一律保留）。
- **原始 stdout/stderr**：`results/workspaces/outputs/<run_id>.std{out,err}.txt`，库内只留 sha256。
- **报告**：`reports/<suite>__<agent>__<tag>/report.json`（事实源）。

无 schema 迁移（v1.0 初版）。

---

## 3. 配置项（仅名称与用途）

| 位置 | 键 | 用途 |
| --- | --- | --- |
| `config/default.yaml` | `benchmark.*` | 基准名 / 版本 / 任务与 suite 根目录。 |
| | `agent.ref` / `agent.version` | 默认 Agent。 |
| | `model.*` / `grader_model.*` | 被测模型与打分模型（`api_key_env` 仅存**变量名**，不落密钥）。 |
| | `runtime.*` | 超时 / grace / 工具上限 / 并发 / workspace / env 白名单 / docker（P2，默认关）。 |
| | `evaluation.*` | runs / seed / offline_replay / grader_type / pass_threshold / 7 维权重 / strict_warn。 |
| | `gates.*` | 回归门禁阈值（`max_delta_pp` 为**绝对值百分点**；`max_ratio` 为比值）。 |
| | `output.*` | 报告目录 / 格式 / 结果目录。 |
| 环境变量 | `KAOYANBENCH_ROOT` | 覆盖项目根。 |
| | `OPENAI_API_KEY` / `GRADER_API_KEY` / `OPENAI_BASE_URL` / `GRADER_BASE_URL` | 模型与打分模型凭据（**仅读环境，不落盘**；缺失时 semantic 自动降级）。 |
| | `runtime.env_passthrough` 白名单内变量 | 显式放行给子进程；默认屏蔽 `*_API_KEY`。 |

---

## 4. 与契约的偏差 / 补丁（本次修复，均已加回归测试）

| # | 位置 | 问题 | 修复 |
| --- | --- | --- | --- |
| 1 | `pyproject.toml` | `[tool.setuptools.packages.find] where = "src"` 字符串不合法（setuptools 80 要求数组）→ `pip install -e .` 失败 | 改为 `where = ["src"]`。 |
| 2 | `pyproject.toml` | `license = { text = "MIT" }` + `License ::` classifier 在 setuptools 80 冲突 | 改 SPDX `license = "MIT"`，删冗余 classifier。 |
| 3 | `core/graders/semantic.py:233` | `build_semantic_client` 只接受 1 参，CLI 传 2 参 → 运行时 `TypeError` | 签名扩为 `(cfg, prompt_config=None)`，并让 CLI 传 `config.grader_model`。 |
| 4 | `cli.py:404,510` | 同上，参数错误 | 改传 `config.grader_model`。 |
| 5 | `core/models.py` `AgentOutput` | Runner（command/http）写 `output.success`，但 dataclass 无此字段 → `AttributeError` | 补 `success: bool \| None` 及其 schema。 |
| 6 | `cli.py:475,528,537` | 读 `grade.total_score`，实际字段为 `score.total` | 改 `grade.score_total`；并在 `GradeResult` 增 `total_score` / `task_success` 便捷属性。 |
| 7 | `cli.py:667,730` | `summarize_verdict(report)` 传对象（签名要 str）→ `TypeError: unhashable` | 改 `summarize_verdict(report.verdict)`。 |
| 8 | `_select_tasks` | `--tag` 被当成**任务标签过滤**，与 `run --tag`（suite 标签）冲突 → `run --tag x` 报「没有匹配任务」 | `_select_tasks` 去掉 tag 过滤；任务标签过滤只在 `list` 内做。 |
| 9 | `cmd_regression` | 读 `gate.threshold_text`（不存在，实为 `threshold_delta_pp` / `threshold_ratio`） | 按字段计算阈值展示文本，新增 `_fmt_signed`。 |
| 10 | `cmd_validate` | `--strict` 下仅 warn 也返回 2 | 明确退出码：error→2，仅 strict-warn→1，否则 0。 |
| 11 | `core/models.py` `Check.from_dict` | 输入含嵌套 `params` 键时会被双层包裹 `params={'params':{...}}`，导致 `raw_check.get()` 取不到参数 | 兼容合并内层 `params`（规范形态仍是平铺）。 |
| 12 | `core/registry.py` `_validate_references` | manifest 文件名含 `../` 可越出 `references/`（路径穿越） | 增加 `relative_to` 越界校验，越界报 error。 |
| 13 | `core/registry.py` | `validate` 对「缺 references 目录」告警重复输出两次 | `validate_fixtures` 传入 `emit_missing_dir=False`，去重。 |
| 14 | `core/engine.py` | 评分阶段抛 `ScoringError`（如零 check 任务）时任务被 `run_suite` 静默丢弃，结果集少一条 | 新增 `_fallback_grade`：评分异常降级为 **FAIL 结果**（score=0 / `task_success=False` / `grader_mode=degraded` / 附 UNKNOWN 错误），任务不再消失。 |
| 15 | `core/registry.py` | 确定性 grader 但 `checks` 为空时 `validate` 不提示（运行时必失败） | 增补 warn：「确定性 grader 无 check → 运行时无法评分（将兜底为 FAIL）」。 |

### 4.1 QA 报告（秦戈，`docs/dev/qa-report.md`）缺陷修复记录

| # | 缺陷 ID | 位置 | 问题 | 修复 |
| --- | --- | --- | --- | --- |
| 16 | **P0-1** | `cli.py` `cmd_grade` / `core/models.py` | `grade --run-id` 读不存在的 `GradeResult.error_codes` → `AttributeError`、exit=2；`--json` 模式 stdout 全空，复评工作流 100% 不可用 | ① `GradeResult` 增只读属性 `error_codes`（由 `errors: list[ErrorItem]` 派生、去重保序）；② `cmd_grade` 改用 `_grade_error_codes()` 以 `errors` 为唯一事实源、字段缺失时安全兜底，**绝不抛裸 AttributeError**；③ `_find_task` 失败转为可读错误。 |
| 17 | **P1-1** | `cli.py` `cmd_validate` / `core/registry.py` | `validate` 遇「无法加载的 task.json」提前中断，后续任务的 schema 错误全被吞（有 BAD-001 时只报 1 条） | 新增 `load_tasks_tolerant()`：逐目录 `try/except`，把加载失败包装为 `ValidationIssue(level="error", file=...)` 后**继续扫描**；`cmd_validate` 用它替代裸列表推导，最后一次性汇总。 |
| 18 | **P1-3** | `cli.py` `build_parser` / `cmd_fixtures` | 文档 5.10 契约中 `fixtures check` 子命令不存在（`argparse` 报 invalid choice） | 在 `fixtures` 下新增 `check` 子命令（复用现有实现，与 `build --check` 等价）；`cmd_fixtures` 同时兼容两条入口并用 `load_tasks_tolerant` 容错。 |
| 19 | **P2-1** | `cli.py` `_add_json_flag` / `main` | `--json` 只能作全局参数，文档按子命令书写（`kyb list --json` 报 unrecognized arguments） | 新增 `_add_json_flag()`：为 list/run/grade/report/compare/regression/validate/fixtures/snapshot/suite/agent 各子命令注册 `--json`（dest=`json_sub`，不覆盖全局）；`main` 取二者之或。 |
| 20 | **P2-2** | `cli.py` `cmd_validate` / `core/registry.py` | fixture hash / manifest 非法文件名 的 error 输出重复两遍（`validate_tasks` 与 `validate_fixtures` 各收集一次） | 新增 `_dedupe_issues()`：按 `(level, task_id, field, message)` 去重，`cmd_validate` 汇总后统一去重。 |
| 21 | **P1-2 支撑** | `core/registry.py` `validate_tasks` | （任务集问题，非本模块修复）需「判定词不出现在 instruction」的校验能力 | 新增 **warn 级** anti-echo 检查 `_echo_leaked_tokens()`：检测 `string_contains/string_eq/set_includes/point_hit` 的判定词（`any_of/all_of/value/values/contains`，长度≥2）是否原样出现在 `instruction` 中并告警。仅提示，不阻断、不改退出码；已在 core50 上命中原 QA 举例的 HAL-001/HAL-002 等。 |

---

## 5. 关键行为核对（设计要点）

- **Runner 三大铁律**写在 `BaseRunner`：不补全、异常转 `ErrorItem`、超时给部分输出。
- **usage 三级回退**：`agent_reported` → `usage_file` → `estimated_from_chars` → `none`（**从不补 0**）。
- **降级**：`build_semantic_client` 无 key → `None` → `grader_mode="degraded"` + `degraded_reason`；`grader_mode: full|mixed|degraded`。
- **评分可复现**：评分路径无 `random` / 无系统时钟 / 无字典序依赖；`now` / `run_id_factory` 全部外部注入。
- **维度中性再分配**：接收方 max_scores 之和保持不变（`reallocated=True`）；全 neutral → `ScoringError`。
- **pp 语义**：所有 `> / < 5%` 的取舍均按**绝对值百分点（pp）**实现。
- **退出码**：pass→0；warn→0（`--strict-warn`→1）；fail→1；运行时错误→2。

---

## 6. 未完成项 / 已知边界

| 项 | 说明 |
| --- | --- |
| **Docker 沙箱（P2）** | `runtime.docker` 配置已解析，进程级沙箱已实现；容器隔离留待 P2。 |
| **markdown / html / csv 报告** | 归属前端工程师；后端提供 `Reporter` Protocol 与注册点，缺失时 `build_report` 仅告警。 |
| **`snapshot fetch`** | 需联网（P1）；`verify` 已实现 hash 校验。 |
| **`fixtures build`** | 委托 `tools/gen_fixtures.py`（任务集/工具工程师）；本命令做编排与校验。 |
| **50 个任务内容** | 属任务集工程师（T 类）；后端仅提供 `validate` 与临时 fixture 能力。 |
| **`grade --run-id` 复评** | 已实现（QA P0-1 修复后实测：人类可读/`--json` 均 exit=0、不存在 run_id 友好报错 exit=2）；依赖 `results/workspaces/outputs/*.stdout.txt` 存在。 |
| **QA P1-2（基准可判别性）** | 属**任务集**问题（echo 回显指令得 28%），非本模块；后端已提供 anti-echo 校验能力（`validate` 的 warn 级检查），实际整改由任务集工程师负责。 |

---

## 7. 给前端 / 任务集工程师的对接说明

### 7.1 前端工程师（报告渲染）

- 只消费 `report.json`（`build_report` 先落它，schema 稳定）：顶层键 `schema_version / suite / run / summary / tasks / by_category / by_difficulty / score_breakdown_mean / errors / comparison`。
- 在 `src/kaoyanbench/reporters/` 下新增 `markdown_reporter.py` / `html_reporter.py`，实现 `render(report: ReportModel, out_dir: Path) -> Path`，并在模块内调用 `register_reporter("markdown", ...)` / `register_reporter("html", ...)`。
- 资源放 `reporters/assets/`（已加入 `package-data`）。

### 7.2 任务集工程师（任务内容）

- 任务目录：`benchmark/tasks/<split>/<category>/<TASK_ID>/`，含 `task.json`、`README.md`、`references/`（离线题必须有 `references/manifest.json`，文件名→sha256）。
- `grader.checks[*]` 的专有参数**平铺在同一层**（例：`{"id":"c1","type":"string_contains","dimension":"factuality","any_of":["复试线"]}`）。
- 硬规则：`network="online"` 且涉及真实院校当年数字 → `ground_truth` 必须为 `null`；`validate` 会报错拦截。fixture 题一律用虚构院校。
- `config/agents/mock.yaml` 的 `answers` 段为每个被测 `task_id` 登记离线预设答案（或 `"*"` 兜底），保证 smoke 集离线可跑。

---

## 8. 自测记录（实测命令与结果）

```text
$ pip install -e .
Successfully installed kaoyanbench-1.0.0

$ kaoyanbench --version
1.0.0

$ python -m pytest tests/ -q
274 passed in 10.42s

# 端到端（临时任务 + mock runner，见 tests/test_e2e.py）
$ kaoyanbench run --agent mock --task SEARCH-001 --split public --seed 42
[run] suite=(tasks) agent=mock tasks=1 runs/task=1 concurrency=1
  SEARCH-001     PASS  score= 68.33  mode=full     latency=0.00s
task_id     category  difficulty  mode  score  result  halluc
----------  --------  ----------  ----  -----  ------  ------
SEARCH-001  search    easy        full  68.3   PASS    0.0%
suite=(tasks) tag=v1.0.0 agent=mock tasks=1 success_rate=100.0% score_mean=68.33 grader_mode=full

$ kaoyanbench report --suite "(tasks)" --agent mock --tag v1.0.0
[warn] 报告格式 'markdown' 暂不可用（负责方：前端工程师（F-02））…
[warn] 报告格式 'html' 暂不可用（负责方：前端工程师（F-04））…
报告目录：reports/(tasks)__mock__v1.0.0
  json      reports/(tasks)__mock__v1.0.0/report.json

$ kaoyanbench validate
suite=(all)  任务数=1  error=0  warn=3
validate 通过 ✅

$ kaoyanbench regression --suite "(tasks)" --agent mock --baseline base --current v1.0.0; echo $?
告警
0
$ kaoyanbench regression --suite "(tasks)" --agent mock --baseline base --current v1.0.0 --strict-warn; echo $?
1
$ kaoyanbench regression --suite "(tasks)" --agent mock --baseline v1.0.0 --current worse; echo $?
未通过
1

# 超时路径（command runner + sleep 999，time_limit=2s）
$ kaoyanbench run --agent sleeper --task SLOW-001 --split public --seed 1
  SLOW-001       FAIL  score=  0.00  mode=degraded latency=2.05s
# JSONL：timed_out=true, exit_code=-15(SIGTERM), error code=TIMEOUT；任务计为 FAIL 而非被丢弃。
```

（完整的安全 / 边界实测见 `tests/test_cli.py`、`tests/test_security.py`、`tests/test_e2e.py`。）

### 8.1 QA 缺陷修复自测（真实命令与输出）

```text
# P0-1 正常路径
$ kyb --root . run --agent mock --task HAL-001 --split public --seed 42
$ kyb --root . grade --run-id run_s42_00001
run_id:  run_s42_00001
task:    HAL-001
grader:  deterministic（full）
score:   31.7
结果:    FAIL          # exit=0（修复前 exit=2 + AttributeError）

# P0-1 --json 路径
$ kyb --root . --json grade --run-id run_s42_00001
{
  "run_id": "run_s42_00001",
  "task_id": "HAL-001",
  "grader_type": "deterministic",
  "grader_mode": "full",
  "total_score": 31.6667,
  "task_success": false,
  "error_codes": []
}                        # exit=0，stdout 为纯 JSON

# P0-1 不存在 run_id（错误路径，友好报错不崩栈）
$ kyb --root . grade --run-id no_such_run_999
[error] 找不到 run：no_such_run_999    # stderr，exit=2

# P1-1 不可加载任务不再中断（BAD-001 缺字段 + BAD-002 枚举错 → 两条都报出）
suite=(all)  任务数=1  error=3  warn=4
  ERROR [BAD-001] (difficulty) 缺少必填字段 | file=.../BAD-001/task.json | field=difficulty | expected=str
  ERROR [BAD-002] (difficulty) difficulty 不在枚举内：impossible
  ERROR [BAD-002] (time_limit) time_limit 必须为正整数，当前为 -5

# P1-3 fixtures check 子命令
$ kyb --root . fixtures check
  ...
fixtures 校验通过 ✅      # exit=0（修复前 argparse invalid choice）

# P2-1 --json 作子命令参数
$ kyb --root . list --json      # exit=0，合法 JSON
$ kyb --root . run --agent mock --task HAL-001 --split public --json   # exit=0
$ kyb --root . validate --json  # exit=0，合法 JSON

# P2-2 fixture issue 不重复
$ kyb --root . validate | grep -c "fixture hash 不匹配"   # → 1（修复前 2）

# P1-2 支撑：anti-echo warn
$ kyb --root . validate --suite core50 | grep -c "判定词"
84    # 仅 warn，不改退出码

# 全量回归
$ python -m pytest tests/ -q
274 passed in 10.42s    # 基线 252 + 新增 22（tests/test_backend_qa_fixes.py）
```
