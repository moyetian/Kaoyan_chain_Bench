# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed（审查整改，对应 `KaoyanBench-v1.1.0-审查报告.md`）

- **P0 回归门禁空转**：`smoke` 的 mock 基线入库于 `benchmark/baselines/`
  （版本化，CI 只读不写）；基线缺失时 `smoke.yml` 直接 `::error::` + 退出码 2，
  绝不静默自比对。`tools/release_check.sh` 同步改为缺基线即 FAIL。
  移除了写 `results/baseline/`（gitignore 下、CI 不提交）的无效「Update baseline」步骤，
  以及 `smoke.yml` 中与 `validate.yml` 重复的 `validate --suite core50` 步骤（P2-3）。
- **P1-2 CI 加 anti-echo 探针**：`validate.yml` 新增 echo 探针
  （`run --suite core50 --agent echo`，通过率红线 ≤8%，依据 `docs/02` §6.2），
  超限即 job FAIL。
- **P2-4 workflow 双触发**：`smoke.yml` / `validate.yml` 的 `push` 触发限定到 `main`，
  同仓库 PR 不再跑两遍。

### Changed（口径澄清）

- **P1-1 DoD #4**：README 明确为「自动评分覆盖率（含 semantic，100%）；
  其中 24 个（48%）为纯确定性，不依赖评测模型」。
- **P1-3 快照口径**：README / CHANGELOG / roadmap 明确 v1.1 的 23 题快照为
  **合成镜像**（虚构回放语料，用于 `--offline-replay` 链路验证）；
  真实网页快照仍需 `snapshot fetch` 抓取并版本化。
- **P2-1**：`docs/05-roadmap.md` 与 `nightly.yml` 的残留旧数字「19 道」已按实测改为 23。
- **P2-2**：pytest basetemp 由 `results/.pytest_tmp` 挪到仓库根 `.pytest_tmp/`
  （`.gitignore` 已忽略），不再与「运行留痕」目录混杂。

## [1.1.0] - 2026-09-24

首个公开发布版（仓库：`moyetian/Kaoyan_chain_Bench`）。在 v1.0 基础上完成两轮硬化，
公开仓库**不含 private 隐藏题**（见下）。

### Added（新增）

- **快照库补齐（合成镜像）**：23 个联网任务全部配齐离线回放用的**合成镜像快照**
  （35 页虚构回放语料，用于 `--offline-replay` 链路验证，不代表真实网页；
  真实网页快照仍需 `snapshot fetch` 抓取并版本化），`tools/build_snapshots.py`
  确定性生成（`--check` 可校验）；`validate --require-snapshots` 缺快照即 error；
  nightly 与 smoke CI 已启用该门禁。
- **Private 隐藏集机制**：`tools/build_private_tasks.py`（10 题生成器公开）、`--split private`
  隔离、私测可解性测试。**题目本身不入库**（`.gitignore` 忽略，`tests` 无题时自动跳过），
  只在受控环境本地生成运行。
- **回归可比性门禁**：`grader_mode` 不一致（full vs degraded）时追加 warn 门禁，
  含降级的分数不再静默参与比较。
- **报告可复现元数据**：`SuiteResult.source` 新增 `git_sha` + `trace_schema_version`。
- **`Run` 记录生效预算**：`time_limit` / `tool_limit` 落盘，`grade --run-id` 复评与首评同口径。

### Changed（变更）

- **预算优先级修正为 CLI > suite.defaults > 任务 > 全局**（此前任务恒胜，
  `suite.defaults` 是死配置；现 smoke 的 120s/30 与 core50 的 900s/100 真正生效）。
- **降级评分封顶**：`fallback_semantic_score` 上限 0.7，degraded 不可冒充 full。
- **冒烟集描述修正**：smoke 实为 5 类 6 题（此前误写“覆盖 8 类”）。

### Fixed（修复）

- **防泄题隔离**：`{task_dir}` 模板变量与 `KAOYANBENCH_TASK_DIR` 环境变量不再暴露真实任务目录
  （重定向到 workspace 隔离目录），Agent 无法直读 `task.json` 作弊；旧配置保持兼容。
- **回放来源恒 E0**：`--offline-replay` 下快照来源未经证据规则判定，`source_level` 恒失败；
  现回放来源经规则重判定（E0 → E4/E5），快照库真正可用。
- **Windows 控制台崩溃**：gbk 终端打印 `✅/❌` 曾抛 `UnicodeEncodeError` 导致整轮崩溃；
  现自动降级为 `[OK]/[FAIL]`（中文与 JSON 合法性不受影响）。
- **生成器跨平台字节稳定性**：`build_private_tasks.py` 改按字节写入（Windows 文本模式
  `\n→\r\n` 曾致 manifest sha256 全错）；`string_contains` 只认单值 `value`（曾误用 `any_of`）。

### Notes（说明）

- 公开仓库政策：private 题目不入库；`mock` Runner 不设私测预设（私测跑 0 分是预期行为）。
- 仓库地址：`https://github.com/moyetian/Kaoyan_chain_Bench`。

## [1.0.0] - 2026-09-23

KaoyanBench v1.0.0 —— 面向考研场景的 AI Agent 评测基准首个公开发布版。
**核心链路零第三方依赖**（纯 Python 标准库），Python ≥ 3.10。

### Added（新增）

- **CLI**：`kaoyanbench`（别名 `kyb`），子命令 `list / run / grade / report / compare / regression / validate / fixtures / snapshot / suite / agent`，契约见方案 5.10。
- **任务集 `core50`**：50 题 / 8 类（search 12、university 9、policy 5、exam 5、pdf 5、planning 5、research 4、hallucination 5），难度 Easy10 / Medium18 / Hard15 / Expert7。
- **任务集 `smoke`**：6 题全离线确定性冒烟集，供 CI 与「30 秒跑通」。
- **Runner**：`echo` / `mock`（离线 preset 答案）/ `command`（argv 模板 + stdin/cwd/env/超时/三级解析）/ `http`（OpenAI 兼容端点）。
- **Sandbox（进程级隔离）**：独立 workspace、env 白名单（默认不透传任何 `*_API_KEY`）、`HOME/TMPDIR/XDG` 重定向、超时 SIGTERM→SIGKILL。
- **Grader**：`deterministic` / `semantic`（含 SemanticClient）/ `hybrid` / `manual`；16 种声明式 check；E0~E5 证据等级。
- **Scorer / Metrics**：100 分制 7 维评分、11 项关键指标、按 category/difficulty 分组、Pass@1/Pass@3。
- **报告层**：JSON（事实源）/ Markdown / 单文件 HTML（内联 CSS/JS/SVG，**零外部请求**）/ CSV。
- **回归**：`compare` + `regression` 门禁（pp 口径）+ 退出码 0/1/2。
- **快照**：`snapshot fetch`（需联网）/ `snapshot verify`；`--offline-replay`（L1 评分层回放，不发网络请求）。
- **Store**：JSONL ↔ SQLite 幂等同步；SQLite 为派生物，可 `rm` 重建。
- **CI（C-04）**：`.github/workflows/{validate,smoke,nightly}.yml`
  - PR/push：`validate`（pytest + `validate --suite core50`，离线）、`smoke`（`run --suite smoke --agent mock` + 报告 + 回归门禁）。
  - nightly：完整 `core50`（可 `--concurrency`）+ 报告 artifact。
- **部署资产**：`.env.example`、`Dockerfile`、`.dockerignore`、`tools/release_check.sh`。
- **文档**：`docs/03-接入新Agent.md`、`docs/04-报告字段说明.md`、`docs/05-roadmap.md`、`docs/06-部署指南.md`、`docs/07-上线检查清单.md`、`docs/08-回滚预案.md`、本 CHANGELOG。
- **可观测性**：`sandbox.sensitive_env_warnings`（`env_passthrough` 敏感变量告警）、`ResultStore.resolve_suite_ref`（报告类命令的 agent/tag 反查）。

### Changed（变更）

- 报告层从初始骨架补齐 JSON/Markdown/HTML/CSV 四种格式。
- `validate` 改为**一次性汇总**全部任务的校验错误（含不可加载任务），不再被首个坏题中断。
- 基准可判别性整改：移除指令中的判定词，echo 通过率由 28% → 0/50；`anti-echo` 检查告警由 84 → 0。
- `grade` 子命令修复（复用 `utils/mini_yaml` 受限解析器语义，`error_codes` 从 `errors` 提取）。

### Fixed（修复）

- **CI 目录名错误**：仓库根目录曾误为 `_.github/`（GitHub Actions 只识别 `.github/`），
  导致 `validate` / `smoke` / `nightly` 三个 workflow **永不触发**。已重命名为 `.github/`。
- **报告类命令的默认 agent 回退错误**：`report` / `compare` / `regression` 未显式传 `--agent` 时
  把配置语义的 `agent.ref` 当成结果检索条件，导致 README 首屏示例 `kaoyanbench report --suite smoke --tag smoke-1`
  直接失败。改为「显式指定 > 配置 ref（仅当确有结果）> 按已有结果反查」，歧义时明确报错而非猜测。
- **`suite.defaults` 是死配置**：`core50.yaml` 声明 `defaults.runs: 3` 却零消费点，
  实测 `runs_per_task=1`，Pass@3 静默失效。现按 `CLI 显式 > 任务级 > suite.defaults > 全局` 合并
  （`runs` / `time_limit` / `tool_limit`）。
- **快照库双轨错位**：`tools/gen_fixtures.py` 把快照写到 `benchmark/fixtures/snapshots/`，
  而运行时 `SnapshotStore` 读 `benchmark/snapshots/`（`evaluation.snapshot_dir`），
  且生成的 `index.json` 里 `pages` 是字符串列表而非 `SnapshotPage` 对象，
  导致 `snapshot verify` 直接崩溃（`AttributeError`）、`--offline-replay` 读不到快照。
  现已统一到运行时目录并生成逐字段匹配的 `index.json`（`pages[].snapshot_path` 相对任务目录）。
- **`validate` 的快照存在性检查指向错误路径**：原检查任务目录下的 `snapshots/`，
  现改为检查运行时快照库 `benchmark/snapshots/<task_id>/index.json`。
- **`_sources_of` 未传 `task_id`**：`--offline-replay` 下会把快照目录下**所有任务**的来源
  算进当前题，使 `source_precision` / `source_level` / `citation_coverage` 偏乐观。
  现已限定 `task_id`；拿不到任务身份时不回放（宁可用 Agent 自报来源，也不跨任务污染）。
- **`is_sensitive_env` 定义了但从未被调用**：`env_passthrough` 里写 `OPENAI_API_KEY` 等敏感变量
  无任何告警。现由 `build_sandbox_env` 在透传前发出 `UserWarning`（显式声明仍生效，不拦截）。
- **语义评测无重试**：`invalid_response`（响应非 JSON / 缺字段）现在最多重试
  `grader_model.max_retries` 次（默认 1，方案 5.3）；网络类错误不重试，直接走降级。
- **`query_runs` 每次全量同步**：`sync_db` 改为按 `(mtime_ns, size)` 增量同步，
  未变化的 JSONL 不再重读；`write_grade` 追加导致的变更仍能被捕获。
- **`_run_from_records` 取第一条 `run_end`**：改为取最后一条，避免同一 JSONL 追加后读到旧状态。
- **`_persist_raw` 落盘位置隐式依赖 workspace 布局**：改为显式基于 `config.results_dir`，
  `stdout_path` 明确相对 `results/`。
- **两个平台适配测试**：`/bin/echo`（Windows 不存在）改用当前解释器；路径断言改为分隔符归一化。
- **`gen_demo_benchmark.py` 默认输出 `/tmp/...`**：改为 `tempfile.gettempdir()`，跨平台。
- `grade --run-id` 崩溃（读取不存在的 `GradeResult.error_codes`）。
- `--suite` 抛 `TypeError: 'Suite' object is not iterable`。
- `AgentSpec.from_dict` 的 `params:` 双层嵌套解析。
- `json_schema` 的 `array.minLength` 不生效（改按元素个数）。
- `cmd_report` 的 `tasks[].category/difficulty/network` 全为 `unknown`。
- fixture hash / manifest 非法文件名的 error 输出重复两遍。
- **`tools/check_report_html.py` 的 URL 扫描误报**：原实现对全文搜 `http(s)://`，
  把报告内嵌 JSON 数据里的来源 URL（如 `https://yz.chsi.com.cn/tj/`）误判为外部资源，
  导致含真实来源的 core50 报告 `26/27 FAIL`。现 URL 扫描排除 `<script>` 数据块，
  并新增「script 无主动外联」断言（fetch/XHR/WebSocket/EventSource/sendBeacon/new Image），
  真实外链仍会被捕获。core50 报告实测 `28/28 PASS`（新增 6 个回归测试）。
- **`release_check.sh` 硬编码 `python3` / `kaoyanbench`**：非 Unix 或未 `pip install -e .` 的环境
  直接无法运行；第 2 步还把 pytest 跑了两遍且管道吞掉退出码。现支持 `PY` / `KYB` 环境变量覆盖，
  路径归属检查改用 Python 归一化比较（兼容 Windows 正反斜杠），pytest 步骤改为单次捕获输出，
  并新增第 7 步 HTML 报告验收（`check_report_html.py`，原 7 步 → 8 步）。
- **pytest 默认 basetemp 依赖系统 `%TEMP%`**：受限环境（本机 `%TEMP%\pytest-of-<user>` 权限被拒）
  会在收集阶段直接 `PermissionError`（117 errors）。现于 `pyproject.toml` 固定
  `--basetemp=results/.pytest_tmp`（仓库内、已被 `.gitignore` 忽略），显式传参仍可覆盖。
- **`--runs` 无上界**（QA P3-1）：`--runs 999999` 无保护启动海量重复，实测挂起数分钟。
  现于 `cmd_run` 合并 `--runs` / `suite.defaults.runs` / `config.evaluation.runs` 后统一校验：
  `< 1` 与 `> MAX_RUNS_PER_TASK`（100）均报错并返回退出码 2；`--runs 0` 不再被静默当作 1 次。
- **方案 §6.4 汇总表两处笔误**：`DET/HYB` 写成 26/24（应为 **24/26**）、离线/联网写成 31/19（应为 **27/23**）。
  第 6.3 节逐题明细表与 `benchmark/tasks/public/**/task.json` 实测均为 `DET 24 / HYB 26`、`offline 27 / online 23`。
  已修正 `docs/01` 三处，并同步 README / `docs/05` / `docs/06` / `docs/07` 中的下游引用。

### Known Limitations（已知边界与遗留项 —— 如实记录，未粉饰）

- **Private 集内置 0 题**：v1.0 只交付 Public/Private 分离**机制**（`--split`、`benchmark/tasks/private/`），private 目录仅含 README，**无 private 内容**。这是方案 R6 明确划定的 v1.0 边界。
- **联网题快照未建立**：v1.0 有 23 道 `network=online` 题，**23 题的网页快照全部尚未建立**（T-07 遗留，需联网抓取）。因此联网题的**真实端到端语义评分未覆盖**，CI 默认只跑离线子集。
  已建立的仅有 3 道离线题的**本地固定假快照**（SEARCH-011 / SEARCH-012 / RES-002，由 `tools/gen_fixtures.py` 生成，绝不联网），可用于验证 `snapshot verify` 与 `--offline-replay` 链路。
- **真实 LLM Semantic Grader 未在 CI 验证**：CI 无 API key 时语义评测自动降级为 `grader_mode=degraded`（`degraded_reason=no_api_key`）；**degraded 结果不得写入回归 baseline**（R9）。
- **Docker 沙箱未实现**：`runtime.docker.enabled=true` 在 v1.0 为 P2 占位，显式报错而非静默降级；沙箱隔离级别明确为**进程级**，不防 `/tmp` 外写入与网络外泄。
- **L2（Agent 层回放代理）未进 v1.0**：仅保留 `KAOYANBENCH_REPLAY_DIR` 语义占位。
- **Runner 层任务级重试未实现**：v1.0 的重试只覆盖语义评测的 `invalid_response`（方案 5.3 明确要求的那一条）。
  被测 Agent 自身超时 / spawn 失败**不自动重跑**——因为方案未定义其重试次数与条件，贸然重试会改变评测语义（重复消耗额度、影响 Pass@k 口径）。
- **CI 真实触发待验证**：目录名已从 `_.github` 修正为 `.github`，三个 workflow 的 YAML 语法与 jobs 结构已静态校验通过；
  但 GitHub Actions 的**首次真实触发**仍需在推送到远端后确认一次（本机无法运行 Actions runtime，详见 `docs/07` 第 I 节）。

### Notes（说明）

- 所有 fixture 内院校/数据均为**虚构**，不代表任何真实机构。
- 涉及真实院校当年具体数字的任务，ground truth 一律为空，只做「要点命中 + 来源等级 + 年份标识」判定（R3）。
- 评测模型与被测模型**强制分离**（`allow_same_model` 默认 `false`）。
- License：MIT。

[1.1.0]: https://github.com/moyetian/Kaoyan_chain_Bench/releases/tag/v1.1.0
[1.0.0]: https://github.com/moyetian/KaoyanBench/releases/tag/v1.0.0
