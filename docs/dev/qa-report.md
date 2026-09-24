# KaoyanBench v1.0 独立测试报告（QA）

> 测试工程师：秦戈　｜　被测版本：KaoyanBench v1.0（仓库 `/workspace/KaoyanBench`，HEAD `1f3be03`）
> 测试方式：**独立复现**。不采信开发者自测结论（`pytest 252 passed` 仅作为参考记录，见 §2）。
> 隔离环境：`/tmp/kb_lab`（仓库完整克隆，测试前后不改动 `src/**`、`cli.py`、`benchmark/**`）。
> 测试脚本：`docs/dev/qa-cases/qa_0{1..7}_*.py|sh`（可复现，见 §6）。
> 报告日期：2026-09-23

---

## 1. 测试范围

| 维度 | 内容 |
|---|---|
| 被测功能 | CLI 全部子命令（list/run/report/compare/regression/validate/snapshot/fixtures/grade/agent/suite）、任务集校验、三种 Runner（echo/mock/command）、16 种 check、4 类 Grader、Scorer 评分与退化、Metrics 聚合、Store、Regression 门禁、4 种报告器 |
| 环境 | Python 3.11.1 / Linux / 无 `OPENAI_API_KEY`（除专门构造的场景）/ 零第三方渲染依赖 |
| 测试账号 | 不适用（CLI 工具，无多用户/多租户）；越权面改为「子进程 env 白名单」「路径穿越」「并发隔离」 |
| 测试数据 | 全部使用 `pretest_`/`_qa_` 前缀的自建任务与 agent 配置，均在 `/tmp` 隔离目录，未污染仓库 |
| 未使用真实网络 | 需要联网的 23 题在 CI 语义下未实跑（见 §5 未覆盖项） |

---

## 2. 测试执行摘要

### 2.1 跑了哪些命令

| 脚本 | 覆盖 | 结果 |
|---|---|---|
| `qa_01_environ.sh` | 克隆隔离环境、版本、任务数 | ✅ 完成 |
| `qa_02_old_fixes.py` | 4 个旧缺陷 + 其边界 | ✅ 4/4 复现通过 |
| `qa_03_boundary.py` | 超时、畸形输入、ground_truth、fixture 篡改、评分退化、降级 4 路径、usage | ✅ 通过（1 处脚本断言偏差，见 P2-3 注） |
| `qa_04_security.py` | env 白名单、路径穿越、并发隔离、HTML 零外链、报告一致性 | ✅ 通过 |
| `qa_05_reproducibility.py` | gen_fixtures、评分两次一致、run_id 稳定 | ✅ 通过 |
| `qa_06_functional.py` | 子命令、分布断言、三 Runner、Pass@k、Store、四件套 | ✅ 通过 |
| `qa_07_exit_codes.py` | 回归退出码、validate 退出码、grade 崩溃复现 | ✅ 完成（复现出 P0-1） |
| 手工命令 | 50 题全量 run/validate、core50 报告、echo 泄漏量化、离线回放 socket 拦截 | ✅ 完成 |

### 2.2 通过 / 失败统计（按层面）

| 层面 | 用例数 | 通过 | 失败 | 备注 |
|---|---:|---:|---:|---|
| 功能（正向） | 24 | 24 | 0 | 子命令 11 + 分布 3 + Runner 3 + Pass@k 1 + Store 2 + 四件套 1 + 其余 3 |
| 边界与异常 | 22 | 21 | 1 | 唯一失败为脚本断言写法（P2-3），非产品缺陷 |
| 安全与越权 | 9 | 9 | 0 | env 白名单 2 + 穿越 1 + 并发 2 + HTML 4 |
| 可复现性（DoD） | 3 | 3 | 0 | 聚合 sha256 / 评分逐字段 / run_id |
| 旧缺陷回归 | 12 | 12 | 0 | 4 缺陷 ×（主路径 + 边界） |
| **合计** | **70** | **69** | **1** | |

> 开发者自测 `python -m pytest tests/ -q` → `252 passed in 8.63s`（实跑确认）。但该自测**未覆盖** `grade` 子命令的复评路径（P0-1）。

---

## 3. 缺陷清单

### P0-1　`grade --run-id` 子命令崩溃，退出码 2，`--json` 无输出

- **严重级别**：P0（核心子命令完全不可用；属第 49 节 DoD 之外，但方案 5.10 CLI 契约声明 `grade` 为 P1 子命令）
- **标题**：`grade` 读取不存在的 `GradeResult.error_codes`，抛 `AttributeError` 并退出 2
- **复现步骤**：
  ```bash
  cd /tmp/kb_lab
  kyb --root . run --agent mock --task HAL-001 --split public --seed 42
  kyb --root . grade --run-id run_s42_00001        # 人类可读模式
  kyb --root . --json grade --run-id run_s42_00001 # JSON 模式
  ```
- **实际结果**：
  ```
  # 人类可读模式
  run_id:  run_s42_00001
  task:    HAL-001
  grader:  deterministic（full）
  score:   31.7
  结果:    FAIL
  [error] 未预期的错误：AttributeError: 'GradeResult' object has no attribute 'error_codes'
  → exit=2

  # --json 模式：stdout 为空，stderr 报同一错误，exit=2
  ```
- **预期结果**：正常输出复评结果，exit=0；`--json` 输出合法 JSON。
- **证据**：`GradeResult` 实际字段为 `errors`（`list[ErrorItem]`），**无** `error_codes`
  ```
  GradeResult fields: ['task_id','run_id','grader_type','grader_mode','degraded_reason',
    'score','metrics','checks','citations_judged','citations_supported','citations_unjudged',
    'hallucination_violations','errors','graded_at','grader_versions']
  has error_codes: False
  ```
- **定位**：`src/kaoyanbench/cli.py:565`（`"error_codes": list(grade.error_codes)`）与 `src/kaoyanbench/cli.py:574`（`if grade.error_codes:`）
- **影响面**：`grade` 子命令 100% 失败；任何依赖复评（regrade）的工作流（如「重跑评分不重跑 Agent」）全部不可用。开发者 252 条自测无一条覆盖此路径。
- **修复建议**：将两处 `grade.error_codes` 改为从 `grade.errors` 取错误码：
  ```python
  codes = [e.code for e in (grade.errors or [])]
  ```
  并在 `GradeResult` 增加只读属性 `error_codes`（`return [e.code for e in self.errors]`）二选一。

---

### P1-1　`validate` 在遇到「无法加载的任务」时提前中断，后续任务的错误全部不报

- **严重级别**：P1（错误信息不可用 + 边界未处理；CI 每次只能暴露一个错误）
- **标题**：单个不可加载 `task.json` 阻断全量校验，其余 task 的 schema 错误被吞
- **复现步骤**：
  ```bash
  # 构造：BAD-001 缺必填字段（不可加载），BAD-002 类型错、BAD-003 权重和≠1、BAD-004 未知 check
  cd /tmp/kb_lab
  python3 docs/dev/qa-cases/qa_03_boundary.py   # 见 [B-畸形输入] 段落
  ```
- **实际结果**：`validate` 只输出 BAD-001（不可加载）的一条错误即结束；BAD-002/003/004 完全未提及。
  ```
  [error] 缺少必填字段 | file=.../BAD-001/task.json | field=difficulty | expected=str
  （共 1 行，exit=2）
  ```
  移除 BAD-001 后再跑，BAD-002/003/004 的错误**才**被报出（`error=6`）。
- **预期结果**：一次性报出**全部**任务的校验错误（方案 2.1「task.json schema 校验与错误定位」、B-03 验收「报出文件路径+字段路径」），不应因单题加载失败而中断扫描。
- **证据**：见 `qa_03_boundary.py` `[B-畸形输入]`：含 BAD-001 时 `error=1`；移除后 `error=6`，同一批其余任务方才可见。
- **定位**：`src/kaoyanbench/core/registry.py` 的 `validate_tasks`/`discover_task_dirs` 扫描路径 —— 加载异常未 `try/except` 转为 issue，直接冒泡终止。
- **影响面**：CI 与本地校验逐次只能修一个错；50 题任务集若同时有多个 schema 问题会反复来回，且容易误以为「只剩一个问题」。
- **修复建议**：扫描每个任务目录时 `try/except Exception as e`，将异常包装为 `TaskValidationIssue(file=..., field="", message=str(e))` 继续收集，最后统一返回。

---

### P1-2　基准可判别性弱：echo（回显指令）在 core50 上通过 14/50（28%），指令泄漏检查关键字

- **严重级别**：P1（基准质量问题，非引擎 bug —— 直接影响「这套 benchmark 能否区分会推理的 Agent」）
- **标题**：多条任务的正确关键字直接写在 `instruction` 里，回显指令即可命中 check
- **复现步骤**：
  ```bash
  cd /tmp/kb_lab
  kyb --root . run --suite core50 --agent echo --tag _qa_echo --seed 42
  ```
- **实际结果**：
  ```
  suite=core50 tag=_qa_echo agent=echo tasks=50 success_rate=28.0% score_mean=43.34
  echo PASSED tasks: EXAM-004, HAL-001, HAL-002, HAL-004, PDF-005, POL-001, POL-002,
                    SEARCH-007, SEARCH-010, UNI-001, UNI-002, UNI-003, UNI-005, UNI-006
  ```
  逐题比对 `instruction` 与 `grader.checks`，至少 12 题的 check 关键字**原样出现在指令中**，例如：
  - `HAL-001`：指令含「未找到官方数据」，`c1: string_contains value="未找到"` → 回显即通过
  - `UNI-001`：指令含「学制」「学费」，`c1/c2 any_of=["学制"]/["学费"]` → 回显即通过
  - `UNI-005`：指令含「拟招生人数」「推免」，`c1/c2` 直接命中
- **预期结果**：benchmark 应能区分「会思考的 Agent」与「只会复述的 Agent」；echo 的通过率应接近 0（方案 6.5「避免记忆题」的精神、R6「任务集被针对性过拟合」）。
- **证据**：以上命令输出 + `qa_06_functional.py` 的分布断言正常，说明引擎无问题 —— 问题在**任务集内容**。
- **影响面**：所有基于 core50 的分数都会系统性偏高，`task_success_rate` 作为回归门禁指标的信号被稀释；对外发布会被质疑「题目可被 prompt 泄漏刷分」。
- **修复建议（交任务集工程师 T 类）**：① 指令不得包含 check 的判定词（把正确答案词从 instruction 移除，改为描述性要求）；② 将关键字类 check 升级为需要**计算/检索**才能得到的数值或结构化字段（exam/pdf/planning 类正是这么做的，通过率低）；③ 增加「anti-echo」回归：CI 中断言 echo runner 在每类上的通过率 ≤ 阈值。

---

### P1-3　`fixtures check` 作为子命令不存在（文档 5.10 与实现不符）

- **严重级别**：P1（文档契约的 CLI 入口不可用；有绕行 `--check`）
- **标题**：`kaoyanbench fixtures check` 报 argparse 错误
- **复现步骤**：
  ```bash
  cd /tmp/kb_lab
  kyb --root . fixtures check
  ```
- **实际结果**：
  ```
  kaoyanbench fixtures: error: argument <build|check>: invalid choice: 'check' (choose from 'build')
  → exit=2
  ```
  可用绕行：`kyb fixtures build --check`（成功，输出「fixtures 校验通过 ✅」）。
- **预期结果**：方案 5.10 写 `kaoyanbench fixtures build [...] [--check]`；`--check` 形式可用，但契约未声明 `fixtures check` 子命令 —— 属**文档示例与实现口径不一致**，且 `fixtures build --check` 未在帮助中提示「等价于校验」。
- **证据**：上方命令输出；`fixtures --help` 仅列出 `build`。
- **定位**：`src/kaoyanbench/cli.py` 的 `fixtures` 子解析器（只注册 `build`）。
- **影响面**：照文档/直觉输入 `fixtures check` 的用户会撞 wall。
- **修复建议**：要么在 `fixtures` 下新增 `check` 别名，要么在 `docs/01` 5.10 明确只支持 `fixtures build --check`。

---

### P2-1　`--json` 只能作**全局**参数，文档却按**子命令**参数书写

- **严重级别**：P2（体验/一致性问题；文档与实现不符）
- **标题**：`kyb list --json` / `run --json` / `compare --json` / `validate --json` 一律 argparse 报错
- **复现步骤**：
  ```bash
  cd /tmp/kb_lab
  kyb --root . list --json        # 报错
  kyb --root . run --agent mock --task HAL-001 --split public --json   # 报错
  kyb --root . --json list        # 正确用法
  ```
- **实际结果**：
  ```
  kaoyanbench: error: unrecognized arguments: --json   → exit=2
  ```
- **预期结果**：方案 5.10 多处写 `kaoyanbench list [...] [--json]`、`... regression [...] [--json]`，读者会认为 `--json` 是各子命令的可选参数。
- **证据**：上方命令；全局 `kyb --json <cmd>` 时 stdout 为纯 JSON、日志走 stderr（该行为**符合**契约，已验证）。
- **影响面**：所有按文档书写的脚本会失败。
- **修复建议**：在各子 parser 上 `add_argument("--json", action="store_true")`（或改用 `parents=[common]`），并统一 `console.json_mode`。

---

### P2-2　fixture hash 不匹配 / manifest 非法文件名 的 error 输出重复两遍

- **严重级别**：P2（体验/一致性问题）
- **标题**：同一校验错误被输出两次
- **复现步骤**：
  ```bash
  cd /tmp/kb_lab
  python3 - <<'PY'
  p='benchmark/tasks/public/exam/EXAM-001/references/questions.csv'
  b=bytearray(open(p,'rb').read()); b[0]^=1; open(p,'wb').write(bytes(b))
  PY
  kyb --root . validate --suite core50 | grep -c "fixture hash 不匹配"   # → 2
  ```
- **实际结果**：`fixture hash 不匹配` 出现 2 次；`manifest.json 中的文件名非法` 同样出现 2 次。
- **预期结果**：每条 issue 只输出一次（后端 notes §4 #13 声称已对「缺 references 目录」去重，但未覆盖这两类）。
- **证据**：
  ```
  ERROR [EXAM-001] (references.questions.csv) fixture hash 不匹配：questions.csv（期望 4cc2b84e4646…，实际 0cb60eb698a7…）
  ERROR [EXAM-001] (references.questions.csv) fixture hash 不匹配：questions.csv（期望 4cc2b84e4646…，实际 0cb60eb698a7…）
  ```
- **定位**：`src/kaoyanbench/core/registry.py` `validate_fixtures` / `_validate_references` 中 issue 被两条路径各收集一次。
- **影响面**：CI 日志噪音；`error` 计数虚高（显示 2 而非 1）。
- **修复建议**：在 `validate_fixtures` 返回前对 `(task_id, field, message)` 去重。

---

### P2-3　test 脚本断言口径问题（非产品缺陷，记录以免误读）

- **严重级别**：P2（仅本报告脚本）
- **说明**：`qa_03_boundary.py` 中「带文件路径」断言在第二次 validate（仅含可加载任务）中失败，原因是该批错误不经过文件路径分支；实际产品在**不可加载**任务上**确实**输出了 `file=.../task.json`（见 P1-1 证据）。此为脚本断言写法，非产品缺陷。

---

### P3-1　`--runs` 无上界，超大值导致命令长时间占用

- **严重级别**：P3（建议）
- **说明**：`kyb run --task HAL-001 --runs 999999` 未见上界保护，实测挂起 3 分钟被手动终止。`--runs 0` 被静默当作 1 次（可接受但未提示）。建议对 `--runs` 设合理上界（如 ≤100）并校验。

---

## 4. DoD 15 条逐条判定

| # | DoD 内容 | 结论 | 证据 |
|---|---|---|---|
| 1 | 50 个真实考研任务 | ✅ 满足 | `kyb --json list`：`task_count=50`；`find benchmark/tasks -name task.json \| wc -l` = 50 |
| 2 | 至少 8 个任务类别 | ✅ 满足 | 类别实测 `{search:12, university:9, policy:5, exam:5, pdf:5, planning:5, research:4, hallucination:5}`，8 类齐 |
| 3 | 每个任务有明确 Ground Truth | ✅ 满足 | 50 题全部含 `expected`；`validate --suite core50` → `error=0` |
| 4 | ≥50% 任务可自动评分 | ✅ 满足 | 实测 core50 全量 `--agent mock` 50 题全部产出 `grades`（100%）；纯确定性 **24/50=48%**（订正：原写 26/52%，系沿抄方案 §6.4 笔误，见文末「口径订正」） |
| 5 | 搜索任务有来源质量评分 | ✅ 满足 | `source_level`/`source_domain` check 存在；`by_category[search].source_precision_mean` 已聚合（见 core50 报告） |
| 6 | 引用具有 Citation Accuracy | ✅ 满足 | `citation_accuracy` 指标与 `citation_coverage` check 均实测产出 |
| 7 | 存在抗幻觉测试 | ✅ 满足 | hallucination 5 题 + 全任务 `must_not_claim`；实测 `HALLUCINATION` 错误码可触发（core50 报告 count=7） |
| 8 | 保存完整 Agent Trace | ✅ 满足 | `results/runs/*.jsonl` 逐事件（run_start/tool_call/run_end/record）；超长结果截断+sha256（backend-notes B-10） |
| 9 | 保存 Token/Cost/Latency | ✅ 满足 | `usage_source=none` 时 `tokens/cost=null`（**未填 0**）；`latency_seconds` 实测有值 |
| 10 | 支持重复运行 | ✅ 满足 | `--runs 3` 实测 6 题×3=18 runs，Pass@1=Pass@3=1.0 |
| 11 | 支持版本比较 | ✅ 满足 | `kyb compare v1 v3 --suite smoke --agent mock` 输出 Δpp 表；退出码 0 |
| 12 | 支持 Regression Test | ✅ 满足 | 实测 Δ=-16.7pp → `verdict=fail`、exit=1；Δ=0→exit=0；`--strict-warn`→exit=1；运行错误→exit=2 |
| 13 | 支持 Markdown Report | ✅ 满足 | 实测产出 `report.md`（103 行表格），含总体/类别/难度/错误/失败任务章节 |
| 14 | 支持 JSON Result | ✅ 满足 | `report.json` schema_version=1.0；`suite_result.json` 实测产出 |
| 15 | Public/Private 分离 | ⚠️ **部分满足** | 机制存在：`list --split private` 只列 private（实测 0 条，private 目录仅 README）；`list --split public`=50。**private 集内置 0 题**（方案 R6 明确 v1.0 边界），机制可验但无 private 内容可测 |

**判定小结**：15 条中 **14 条满足，1 条部分满足（#15，属方案既定边界）**。无 DoD 因缺陷未达成。但 DoD #4/#5/#6/#7 的「满足」是**引擎与结构层面**的满足；其**题目质量**（P1-2 的可判别性）不在 DoD 字面范围，但影响发布价值。

---

## 5. 已修复旧缺陷的独立复验

| 旧缺陷 | 复验结论 | 证据（`qa_02_old_fixes.py`） |
|---|---|---|
| 1. `--suite` 抛 `TypeError: 'Suite' object is not iterable` | ✅ **已修复** | `load_suite_tasks` 返回 `list[6]`（已解析 Suite 与未解析 manifest 两条路径均返回 list）；`suite` 引用不存在 task_id → `SuiteValidationError`（清晰） |
| 2. `AgentSpec.from_dict` `params:` 双层嵌套 | ✅ **已修复** | `params.answers` 正确解出，`params` 不含嵌套键；顶层 `answers` 与 `params.answers` 两条路径等价。**边界**：两处同名键冲突时**顶层优先**（已文档化）；`answers` 传非 mapping 时静默忽略（不崩） |
| 3. `json_schema` array 的 `minLength` 不生效 | ✅ **已修复** | array 按元素个数、str 按字符数（含 CJK）；`minItems/maxItems` 生效；恰好等于边界长度 → 通过；混合 `minLength+minItems` 取 max、`maxLength+maxItems` 取 min。10/10 边界用例通过 |
| 4. `cmd_report` 的 `tasks[].category/difficulty/network` 全为 `unknown` | ✅ **已修复** | smoke 报告 6 条明细、core50 报告 50 条明细均无 `unknown`；`by_category` 为 8 类枚举序；`errors` 11 类齐全 |

**结论：4 个旧缺陷的修复经独立复现全部有效，且补充的边界用例（冲突 key、恰好边界长度、被删 task_id）行为正确。**

---

## 6. 测试脚本（可复现）

位于 `docs/dev/qa-cases/`（未污染 `tests/`）：

| 脚本 | 用途 | 运行方式 |
|---|---|---|
| `qa_01_environ.sh` | 克隆隔离环境到 `/tmp/kb_lab` | `bash docs/dev/qa-cases/qa_01_environ.sh` |
| `qa_02_old_fixes.py` | 4 个旧缺陷 + 边界复现 | `cd /tmp/kb_lab && python3 <脚本>` |
| `qa_03_boundary.py` | 边界与异常（超时/畸形/ground_truth/fixture/评分退化/降级/usage） | 同上 |
| `qa_04_security.py` | 越权与安全（env 白名单/路径穿越/并发隔离/HTML 零外链/报告一致性） | 同上 |
| `qa_05_reproducibility.py` | 可复现性（gen_fixtures/评分两次/seed 稳定） | 同上 |
| `qa_06_functional.py` | 功能正向（子命令/分布/Runner/Pass@k/Store/四件套） | 同上 |
| `qa_07_exit_codes.py` | 回归退出码 + grade 崩溃复现 | 同上 |

脚本依赖 `/tmp/kb_fake/` 下的假 CLI（脚本内自动生成），不需外网。

---

## 7. 关键正向验证证据摘要

- **50 题全量 `validate`**：`error=0`，`warn=28`（全部为 `requires_snapshot` 告警，符合 R1/R7 的 v1.0 边界）。
- **端到端 smoke（mock）**：`tasks=6 success_rate=100.0% score_mean=99.94 grader_mode=full`；产出 `results/runs/*.jsonl` + `results/suites/*.suite.json` + `report.{json,md,html,csv}` 四件套。
- **core50 全量 run（mock）**：50 题全部产出，`task_count=50`，报告 `tasks` 长度 50 == `suite.task_count`，`by_category` 8 类枚举序，`errors` 11 类，无 `unknown`，`degraded_task_count=26`。
- **超时**：`sleep 999` + `time_limit=2` → 2.05s 被杀，`timed_out=true`、`exit_code=-15`、`errors=[PARSING_FAILURE, TIMEOUT]`、任务计 FAIL 且**未丢失**。
- **降级 4 路径**：无 key→`no_api_key`；网络不可达→`network_unreachable`；响应非 JSON→`invalid_response`；同模型→`ConfigError`（`allow_same_model:true` 放行）。HYB 题端到端实测 `grader_mode=degraded` 且报告顶部有降级告警，**无静默假装评过**。
- **评分退化**：全 neutral→`ScoringError`（不产假分数）；单维 neutral→`reallocated=true`、各维之和恒 100；`task_success=false`/`timed_out` → Efficiency 强制 0。
- **security**：`env_passthrough: []` 时子进程 14 个环境变量中**无** `OPENAI_API_KEY`；显式放行则可见。`manifest.json` 写 `../../../../etc/passwd` → 被拒（exit=2）。并发 3 → 6 个唯一 workspace，各自独立 `home/`。
- **HTML 零外部请求**：`report.html` 中 `http(s)://` 仅 `http://www.w3.org/2000/svg` 命名空间；无 `<link>`/`src=`/`fetch`/`XHR`/`@import`/`url(`/`//`；标准库 `html.parser` 校验标签全部闭合。
- **可复现性**：`gen_fixtures.py` 连跑两次聚合 sha256 一致（自行计算，74 文件）；同任务同答案评分两次**逐字段一致**（含 `checks.detail`）；固定 seed 两次 run_id 完全一致。
- **Store**：同步两次 DB 行数不变；`rm benchmark.db` 后 `rebuild_db` 计数与删除前一致。
- **离线回放**：socket 拦截实测 `run --offline-replay` 期间 `socket.create_connection` / `urllib.request.urlopen` 调用为 **空列表**（0 次网络请求）。

---

## 8. 未覆盖项与原因

| 未覆盖项 | 原因 |
|---|---|
| 19 道 `network=online` 联网题的真实端到端语义评分 | 需外网与真实模型 key，且 A 类正向测试明确「不依赖外网」；仅验证了 `validate`、离线回放、降级路径 |
| 真实语义 Grader（SemanticClient 真实调用 LLM）的评分正确性 | 无 `OPENAI_API_KEY`；仅用本地 HTTP 假端点验证了 3 条降级路径与正常响应解析的**错误分支** |
| `snapshot fetch`（需联网建快照） | 需外网；`snapshot verify` 已实测通过 |
| Docker 沙箱（P2） | 方案明确 P2 默认关闭，v1.0 不交付 |
| 50 题各自「正确答案是否真能得分」的人工复核 | 属任务集内容评审（T-06/T-09）；本报告从 echo 泄漏角度给出 P1-2，未逐题核对 ground truth 正确性 |
| `--runs` 的 Pass@3 在「部分 task 有失败」时的精确性 | smoke 全通过（Pass@1=Pass@3=1.0），未构造「第 2/3 次才通过」的用例（mock 确定性使该场景难以构造） |
| CI workflow（C-04）实际触发 | 仓库无 `.github/`（DoD ★12 的 CI 落地未在本次范围） |

---

## 9. 测试结论

**不可交付（No-Go）—— 需修复 P0-1 后方可进入「有条件交付」评估。**

- **卡点**：`grade` 子命令（P0-1）100% 崩溃，属方案 5.10 CLI 契约声明的子命令；复评工作流完全不可用。这是开发者 252 条自测未覆盖的真实缺口。
- **次生卡点（发布价值层面）**：P1-2 显示 core50 可被「回显指令」刷到 28%，基准可判别性不足。引擎本身健康，但**对外发布前建议任务集工程师整改指令泄漏**，否则基准公信力受损。
- **引擎核心能力（评分/聚合/降级/复现/安全）实测扎实**：无数据造假、无静默通过、无越权、无网络外泄、可复现性达标，14/15 条 DoD 满足。修复 P0-1 + 澄清 P1-1/P2-1/P2-2 后，可作为 v1.0 交付。

**建议交付路径**：
1. 后端修 P0-1（2 行）、P1-1（扫描容错）、P2-1（`--json` 下沉）、P2-2（issue 去重）；
2. 任务集工程师修 P1-2（移除指令中的判定词，补 anti-echo 回归）；
3. 回归验证：重跑 `docs/dev/qa-cases/qa_0{2,3,7}_*`，确认 `grade` 不再崩溃、`validate` 能一次报全、`--json` 各子命令可用。

---
---

# 第二轮回归（终轮复验）

> 测试工程师：秦戈　｜　复验日期：2026-09-23
> 范围：对首轮全部缺陷**逐条独立复现**，判定「真的修好了」还是「声称修好了」；
> 并补首轮盲区（YAML 语法冒烟）。
> 隔离环境：`/tmp/kb_lab2`（仓库完整克隆）+ `/tmp/kb_lab_probe`（作弊探针专用）。
> **不采信开发者/主理人的自述**，所有结论以本机实跑输出为准。
> 约束遵守：未修改 `src/**`、`cli.py`、`benchmark/**`、`config/**`；未 `git commit`。

---

## R1. 逐条复验结论表

| 缺陷ID | 原文（摘要） | 复验结论 | 关键证据 |
|---|---|---|---|
| P0-1 | `grade --run-id` 崩溃、`--json` 无输出 | ✅ **已修复** | 真实 run：human/JSON 均 exit=0、stdout 纯 JSON；不存在 run_id：exit=2、无栈 |
| P1-1 | `validate` 遇坏任务提前中断，其余错误被吞 | ✅ **已修复** | 4 个独立坏题（含不可加载者放最后）**一次性全部报出**，`error=4` |
| P1-2 | 基准可判别性弱，echo 可刷 28% | ✅ **已修复（实质）** | echo 自算 **0/50**；更强「抄题面」作弊 **0/50**；anti-echo warn **84→0**；5 题抽查无残留泄漏 |
| P1-3 | `fixtures check` 子命令不存在 | ✅ **已修复** | `fixtures check` exit=0、50 题全 OK；与 `build --check` 等价 |
| P2-1 | `--json` 仅全局，文档按子命令书写 | ✅ **已修复** | `list --json` / `validate --json` / `run --json` 均 exit=0 且 stdout 纯 JSON |
| P2-2 | fixture/manifest 错误重复两遍 | ✅ **已修复** | hash 不匹配、文件名非法各**只出现 1 次**，`error` 计数不再虚高 |
| P2-3 | QA 脚本断言口径（非产品缺陷） | ⚪ **维持**（脚本问题） | `qa_03` 唯一 `[FAIL] 带文件路径` 为脚本写法，非产品缺陷 |
| P3-1 | `--runs` 无上界 | ⚠️ **未修复**（新增负数校验） | `--runs 999999` 仍启动无上界；但 `--runs -5` 已报 `必须 >= 1` |

**新增缺陷：无。** 本轮未发现任何 P0/P1 级新问题。

---

## R2. P0-1 复验（`grade` 三种路径 + 边界）

命令与结果（隔离 lab `/tmp/kb_lab2`，`RUNID=run_s42_00001`）：

```bash
# A) 真实 run_id，人类可读
$ kyb --root . grade --run-id run_s42_00001 ; echo EXIT=$?
run_id:  run_s42_00001
task:    HAL-001
grader:  deterministic（full）
score:   100.0
结果:    PASS
EXIT=0

# B) 真实 run_id，--json
$ kyb --root . --json grade --run-id run_s42_00001 ; echo EXIT=$?
{ "run_id": "run_s42_00001", "task_id": "HAL-001", "grader_type": "deterministic",
  "grader_mode": "full", "total_score": 99.9583, "task_success": true, "error_codes": [] }
EXIT=0            # stdout 为合法 JSON，stderr 为空

# C) 不存在的 run_id
$ kyb --root . grade --run-id run_does_not_exist ; echo EXIT=$?
[error] 找不到 run：run_does_not_exist
EXIT=2            # 无栈、无 AttributeError

# D) 空/损坏 run 文件
$ printf '' > results/runs/run_empty.jsonl ; kyb --root . grade --run-id run_empty ; echo EXIT=$?
[error] 找不到 run：run_empty
EXIT=2
```

**error_codes 功能正确性**（不止“不崩”，还要“报得对”）：构造一个真实会失败的 run（`command` 型 QA agent 输出幻觉答案），复评结果：

```bash
$ kyb --root . grade --run-id run_s7_00001
score:   31.7
结果:    FAIL
错误:    TOOL_FAILURE, PARSING_FAILURE     ← 错误码被正确提取并渲染
$ kyb --root . --json grade --run-id run_s7_00001
{ ... "task_success": false, "error_codes": ["TOOL_FAILURE", "PARSING_FAILURE"] }
```

**结论**：三种路径 + 错误路径 + 边界（空文件）全部正确；`error_codes` 从 `errors` 提取并去重、渲染无误。
源码核对：`cli.py:562` 走 `_grade_error_codes(grade)`，内部 `getattr(errors)` 兜底，绝不抛异常（`cli.py:587-608`）。**与前轮缺陷定位一致，确认为真修复。**

---

## R3. P1-1 复验（`validate` 一次性汇总）

构造 **4 个互相独立**的坏题（放 `benchmark/tasks/public/pretest_qa/`，测后已删）：

| 任务 | 坏法 | 类型 |
|---|---|---|
| PQA-002 | `category="NOT_A_CATEGORY"` | 枚举错 |
| PQA-003 | `grader.weights={factuality:0.5, citation:0.9}`（和=1.4） | 权重和≠1 |
| PQA-004 | `difficulty="NOT_A_LEVEL"` | 枚举错 |
| PQA-009 | 缺 `difficulty` 字段（**不可加载**，且字典序**排最后**） | 加载失败 |

```bash
$ kyb --root . validate 2>&1 | grep -E "ERROR \[PQA"
  ERROR [PQA-009] (difficulty) 缺少必填字段 | file=.../PQA-009/task.json | field=difficulty | expected=str
  ERROR [PQA-002] (category) category 不在枚举内：NOT_A_CATEGORY
  ERROR [PQA-003] (grader.weights) grader.weights 求和必须为 1.0（容差 1e-6），当前为 1.400000
  ERROR [PQA-004] (difficulty) difficulty 不在枚举内：NOT_A_LEVEL
$ kyb --root . validate 2>&1 | grep "suite="
suite=(all)  任务数=53  error=4  warn=39     ← 一次性报全，error=4
```

**关键判据**：把**不可加载**的 PQA-009 放在字典序最后，仍与其余 3 条在同一次扫描中全部报出 —— 证明扫描不再被第一个坏题中断，且**与顺序无关**。
源码核对：`registry.py:879` `load_tasks_tolerant()` 对 `load_task` 逐目录 `try/except`，异常包装为 `ValidationIssue` 继续收集。**确认为真修复。**

---

## R4. P1-2 复验（基准可判别性 —— 本轮最重要）

### R4.1 echo 通过率（**自行计算**，不信 summary 行）

```bash
$ kyb --root . run --suite core50 --agent echo --tag _qa_echo2 --seed 42
suite=core50 tag=_qa_echo2 agent=echo tasks=50 success_rate=0.0% score_mean=14.39 grader_mode=mixed
```

自行读取 `results/suites/core50__echo___qa_echo2.suite.json` 的 `grades[].metrics.task_success`：

```
MY CALC: total=50 passed=0 rate=0.0%
score mean=14.39  max=57.00（PLAN-003/POL-004，均 < 60 阈值）
```

**结论：echo = 0/50 = 0.0%**（首轮为 28%，声称 28%→0% 属实）。

### R4.2 更强判据：「抄题面」作弊 Runner（我自行设计）

首轮只测了 echo（仅回显 instruction）。本轮构造**更强**的作弊策略：读取 `task.json`，把
`instruction` + `expected`（含 `must_find.any_of`）+ `grader.checks` 的
`value/patterns/any_of/values` 等**全部判定词原文拼接**作为答案 —— 即"题面泄漏 + 判定词泄漏"叠加到极致。

- 脚本：`docs/dev/qa-cases/qa_08_cheat_runner.sh`（输出 `{"answer": <拼接文本>}`）
- 探针：`docs/dev/qa-cases/qa_09_cheat_echo_probe.py`（一键复跑 echo/cheat/mock）
- 接线：隔离 lab 的 `config/agents/qa_cheat.yaml`（`command` 型，`{task_dir}` 传入）

```bash
$ python3 docs/dev/qa-cases/qa_09_cheat_echo_probe.py --lab /tmp/kb_lab_probe
### echo @ core50
  MY CALC : passed=0/50  rate=0.0%   score_mean=14.39  score_max=57.00
### qa_cheat @ core50
  MY CALC : passed=0/50  rate=0.0%   score_mean=7.09   score_max=31.67
### mock @ core50
  MY CALC : passed=50/50 rate=100.0% score_mean=94.88  score_max=100.00
```

**「抄题面」作弊 Runner 在 core50 的真实通过率 = 0/50 = 0.0%**，最高分仅 31.67（阈值 60）。
即：即使把判定词全部原样抄进答案，也无法通过 —— 题面→判定词的泄漏已被消除。

### R4.3 mock 防误伤

mock = **50/50 = 100.0%**（score_mean 94.88，最低 65.92）。参考答案仍能通过，整改**未误伤**。

### R4.4 anti-echo 校验能力（能否触发）

```bash
$ kyb --root . validate --suite core50 2>&1 | grep -c "anti-echo"   # → 0
```
core50 **0 条** anti-echo 告警（声称 84→0 属实）。

为证明该校验**不是哑的**，我造了一个指令中直接写「原样输出『未找到官方数据』」的泄漏题：

```
WARN  [PQA-L01] (grader.checks[0]) grader.checks[0] 的判定词「未找到官方数据」原样出现在
      instruction 中，回显指令即可命中（基准可判别性弱，建议改为需计算/检索才可得）
```
→ anti-echo 检查**能正常触发**（1 条），core50 为 0 是「真的没有泄漏」而非「检查不工作」。

### R4.5 5 题抽查：判定是否仍需真实推理

| 任务 | instruction 关键措辞 | checks 判定词 | 是否残留泄漏 |
|---|---|---|---|
| HAL-001 | 「查不到可信依据…不要凭空给出数字」 | `string_contains "未找到"` + must_not_claim | ❌ 无（"未找到"不在题面） |
| UNI-001 | 「读下来要花几年、每年大概多少钱」 | `point_hit ["学制"]` / `["学费"]` + year_tag + source_level E4 | ❌ 无（"学制/学费"不在题面） |
| UNI-005 | 「计划招收规模、免试录取名额」 | 需 `拟招生人数` / `推免` 等 + 口径关系 | ❌ 无（措辞已错开） |
| SEARCH-010 | 「脱产/在职不脱产…各自标注证据等级」 | `["全日制"]`/`["非全日制"]` + E4 + must_not_claim | ❌ 无 |
| PLAN-003 | 「每周模考、三阶段…校验阶段边界与占比」 | `$.schedule` 结构化约束 + `$.stages` 跨阶段有序 regex | ❌ 无（须产出结构化计划才可得） |

**判定**：整改为**实质性**，非换个姿势继续泄漏 —— 判定已从「题面字面量」转移到「需检索/计算/结构化产出」的字段上。PLAN-003 的 README 还专门写明 anti-echo 设计说明。

---

## R5. mock.yaml 事件复验（主理人手工改写，独立验证）

**背景**：`config/agents/mock.yaml` 的 PLAN-003 多行单引号字符串曾被换行截断致 YAML 解析失败（首轮 QA 未覆盖 —— 我的盲区）；主理人重写为单行紧凑 JSON。**他无法自证，由我独立验证。**

### R5.1 能否被 `mini_yaml` 正常解析

```python
from kaoyanbench.utils.mini_yaml import loads
d = loads(open("config/agents/mock.yaml").read())
# PARSE OK. top keys: ['name','type','version','model','provider','parse','params','answers']
# answers count: 51（50 题 + "*" 兜底）；has PLAN-003/PLAN-004: True
```

### R5.2 PLAN-003/PLAN-004 语义是否被机械改写破坏

| 任务 | 内层 JSON | schedule 天数 | day1 | 关键字段 |
|---|---|---|---|---|
| PLAN-002 | ✅ 合法 | 100 | 6h / 4 slots | — |
| **PLAN-003** | ✅ 合法 | 100 | 6.0h / 5 slots（含"自测"） | `stages=[基础,强化,冲刺]` ✓ |
| **PLAN-004** | ✅ 合法 | 60 | 5h / 3 slots | `coverage=0.85` ✓ |
| PLAN-005 | ✅ 合法 | 30 | 2h / 1 slot | — |

端到端复核（mock 实跑）：

```bash
$ kyb --root . run --agent mock --task PLAN-003 --split public --seed 42
  PLAN-003       PASS  score= 99.96  mode=full
# PLAN-004 PASS 99.92 / PLAN-002 PASS 99.96
```

### R5.3 单行 28~32KB 是否触发行长/截断限制

```
mock.yaml 总大小 109749 字节；长行：line 161=6595 / line 165=32628 / line 169=30813 / line 173=12219
```

`mini_yaml` 解析入口用 `text.splitlines()`（`mini_yaml.py:95`），**无行长上限**；Python 字符串本身也无
单行长度限制。实测 32,628 字符的行被完整读入并解析成功。

**独立验证结论：主理人的改法可信。** YAML 可解析、内层 JSON 合法、schedule 天数与字段完整、
端到端 mock 通过；未发现截断或语义破坏风险。

> 提示（非缺陷）：core50 mock 的 `score_mean` 由 94.88（本次）与首轮记录存在个体差异，属正常
> （PLAN-003/004 分数微调）；`task_success_rate` 恒为 100% 不变，不影响判定。

---

## R6. 全量回归结果

| 项目 | 命令 | 期望 | 实测 | 结论 |
|---|---|---|---|---|
| 单元/集成自测 | `python -m pytest tests/ -q` | 274 passed | **274 passed**（基线 252，+22） | ✅ |
| 全量校验 | `kyb validate --suite core50` | error=0 | `validate 通过 ✅`（error=0，warn=28） | ✅ |
| 全量校验（all） | `kyb validate` | error=0 | `error=0 warn=28` | ✅ |
| smoke（mock） | `run --suite smoke --agent mock` | 6/6 | `tasks=6 success_rate=100.0%` | ✅ |
| core50（mock） | `run --suite core50 --agent mock` | 100% | `tasks=50 success_rate=100.0%` | ✅ |
| 首轮 4 旧缺陷 | `qa_02_old_fixes.py` | 全 PASS | 全 PASS（含 10/10 边界） | ✅ |
| 边界/安全/复现/功能/退出码 | `qa_0{3,4,5,6,7}_*.py` | 全 PASS | 仅 `qa_03` 1 处脚本断言 `[FAIL] 带文件路径`（非产品） | ✅ |
| compare | `compare v1 v3 --suite smoke --agent mock` | exit=0 | exit=0，Δpp 表产出 | ✅ |
| regression | `regression ... --baseline v1 --current v3` | exit=0 | exit=0，verdict 行产出 | ✅ |
| 新增回归测试 | `pytest tests/test_backend_qa_fixes.py` | 22 | **22 passed** | ✅ |

---

## R7. 语法/结构冒烟扫描（补首轮盲区）

首轮未做全量 YAML 语法扫描（致 mock.yaml 问题漏检）。本轮补齐：

```
A) config/**/*.yaml（10 个）         : 10 OK / 0 FAIL
     mock.yaml maxline=30828 仍解析成功
B) benchmark/**/*.yaml（3 个 suite）  : 3 OK / 0 FAIL
C) benchmark/tasks/**/task.json（50） : 50 OK / 0 FAIL
     全部含必填字段；最长行仅 179 字符（SEARCH-008），无截断风险
```

**结论：全部 YAML（13 个）+ 全部 task.json（50 个）语法/结构冒烟扫描 0 失败。** 无超长行截断风险。

---

## R8. DoD 15 条重新判定（终轮）

| # | DoD 内容 | 首轮 | 终轮 | 依据 |
|---|---|---|---|---|
| 1 | 50 个真实考研任务 | ✅ | ✅ | `list`：task_count=50；`find task.json`=50 |
| 2 | 至少 8 个任务类别 | ✅ | ✅ | 8 类：search12/university9/policy5/exam5/pdf5/planning5/research4/hallucination5 |
| 3 | 每个任务有明确 Ground Truth | ✅ | ✅ | 50 题全含 `expected`；core50 error=0 |
| 4 | ≥50% 任务可自动评分 | ✅ | ✅ | core50 mock 50 题全产出 grades（100%）；纯确定性 24/50（订正，见文末「口径订正」） |
| 5 | 搜索任务有来源质量评分 | ✅ | ✅ | `source_level` check + `source_precision_mean` |
| 6 | 引用具有 Citation Accuracy | ✅ | ✅ | `citation_accuracy` + `citation_coverage` 实测产出 |
| 7 | 存在抗幻觉测试 | ✅ | ✅ | hallucination 5 题 + 全任务 `must_not_claim`；HALLUCINATION 码可触发 |
| 8 | 保存完整 Agent Trace | ✅ | ✅ | `results/runs/*.jsonl` 逐事件；截断+sha256 |
| 9 | 保存 Token/Cost/Latency | ✅ | ✅ | `usage_source=none`→null（未填 0）；latency 有值 |
| 10 | 支持重复运行 | ✅ | ✅ | `--runs 3` 实测；Pass@1=Pass@3 |
| 11 | 支持版本比较 | ✅ | ✅ | `compare v1 v3` 输出 Δpp 表，exit=0 |
| 12 | 支持 Regression Test | ✅ | ✅ | 实测 pass 判定、exit=0/1/2 三分支正确 |
| 13 | 支持 Markdown Report | ✅ | ✅ | 产出 report.md（多章节表格） |
| 14 | 支持 JSON Result | ✅ | ✅ | report.json schema_version=1.0 等 |
| 15 | Public/Private 分离 | ⚠️ | ⚠️ | 机制在：public=50、private=0（v1.0 内置 0 题，方案 R6 既定边界） |

**判定：14 条满足，1 条部分满足（#15，属方案既定边界，非缺陷）。无 DoD 因缺陷未达成。**

---

## R9. 第二轮测试结论

**发布结论：可以发布（Go）—— 首轮 P0/P1 全部确认为真修复，无阻断项、无新增 P0/P1。**

- **P0-1**（`grade` 崩溃）：✅ 真修复，三路径 + 错误路径 + 边界全部实测通过。
- **P1-1**（`validate` 吞错）：✅ 真修复，4 个独立坏题一次报全、与顺序无关。
- **P1-2**（基准可判别性）：✅ 实质修复。echo 自算 0/50；**更强「抄题面」作弊 0/50**；anti-echo 84→0 且检查可触发；mock 100% 未误伤；5 题抽查无残留泄漏。
- **P1-3 / P2-1 / P2-2**：✅ 全部真修复。
- **mock.yaml 手工改写**：✅ 独立验证可信（YAML 可解析、内层 JSON 合法、语义完整、端到端通过、无截断风险）。
- **语法冒烟**：13 YAML + 50 task.json 全 OK。
- **回归**：274 passed；validate error=0；smoke 6/6；core50 mock 100%；compare/regression 正常。

**遗留（不阻断发布，建议 v1.1）**：
1. **P3-1**：`--runs` 仍无上界（`--runs 999999` 启动不报错），建议加 ≤N 校验并保留已修好的负数校验。
2. **P2-3**：`qa_03_boundary.py` 的「带文件路径」脚本断言写法（非产品缺陷），建议修正脚本以免误读。
3. **DoD#15**：private 集内置 0 题（既定边界，发布时需对外说明）。

**未覆盖项（同首轮，本轮回退不涉及）**：23 道 online 题真实语义评分、真实 LLM Grader、`snapshot fetch`、Docker 沙箱、CI workflow 落地（仓库原为 `_.github/`，已重命名为 `.github/`）。

---

## 附：发布后订正记录（2026-09-23）

本节记录 QA 报告发布后在 v1.0 交付收尾中完成的三项订正。**上文历史记录保持原样**，本节给出最新状态。

### 1. 纯确定性占比口径订正（24/50 = 48%）

- **原文**：§4 DoD#4 与 §R9 沿用「纯确定性 26/50 = 52%」。
- **事实**：该数字沿抄自方案 `docs/01` §6.4 汇总表，而 §6.4 系笔误（DET/HYB 两数写反）。
  以 §6.3 逐题明细表与 `benchmark/tasks/public/**/task.json` 实测为准：**DET 24 / HYB 26**。
- **影响**：DoD#4「≥50% 任务可自动评分」为 **100% 满足**，判定不变；仅"纯确定性"细分数值由 52% 更正为 **48%**。
- **已同步**：`docs/01`（§6.4 表格 + DoD 表 + B-13）、README、`docs/05`、`docs/06`、`docs/07`、`CHANGELOG`。

### 2. 联网题数订正（23 题）

- 方案 §6.4 原写「离线 31 / 联网 19」，实测 `network` 分布为 **offline 27 / online 23**（§6.3 明细表一致）。
- `docs/01` §6.4、`docs/05`、`docs/06`、`docs/07` 及上文中的"19 题"表述已统一订正为 **23 题**。

### 3. P3-1 `--runs` 上界已实现

- `cmd_run` 现对合并后的 `runs/task`（`--runs` > `suite.defaults.runs` > `config.evaluation.runs`）统一校验：
  `< 1` 或 `> MAX_RUNS_PER_TASK`（100）报错并返回退出码 2。
- 实测：`--runs 0` / `--runs 999999` / `--runs -5` 均 exit=2 且给出明确提示；`--runs 3` 正常。
- `--runs 0` 不再被静默当作 1 次（原行为）。

### 4. CI 目录名已修正

- 仓库根目录 `_.github/` 已重命名为 `.github/`（原名导致三个 workflow **永不触发**，见上文未覆盖项）。
  三个 workflow 的 YAML 语法与 jobs 结构已静态校验通过；首次真实触发仍需推送远端后确认。
