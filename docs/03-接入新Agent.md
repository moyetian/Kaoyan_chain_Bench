# 03 · 接入新 Agent

> 面向：需要把 Claude Code / Codex / OpenCode / WorkBuddy / kaoyan_chain 等 Agent 接进 KaoyanBench 的工程师。
> 上游依据：`docs/01-需求拆解与方案定稿.md` 第 5.2 节（Runner 协议）、第 2.2 节（Runner 功能清单）。
> 状态：与 v1.0 实现一致（`src/kaoyanbench/core/runner.py`、`core/runners/*`、`core/sandbox.py`）。

---

## 1. 接入 = 一张 YAML 卡片

KaoyanBench **不要求**被测 Agent 提供 Python 适配器。接入动作只有两步：

1. 在 `config/agents/<name>.yaml` 写一张 **Agent 卡片**，声明「怎么调用、怎么解析输出」；
2. 用 `kaoyanbench run --agent <name>` 跑。

卡片由 `AgentSpec`（`core/models.py`）承载，`type` 字段决定用哪个内置 Runner：

| `type` | Runner | 用途 | 是否联网 |
|---|---|---|---|
| `command` | `CommandRunner` | 调用外部 CLI（主力：`ky` / `claude -p` / `codex exec` / `opencode run`） | 取决于被测 CLI |
| `http` | `HttpRunner` | OpenAI 兼容端点，用于无 CLI 的 harness | 是 |
| `mock` | `MockRunner` | 按预设答案输出，**离线跑通全链路**（CI 主力） | 否 |
| `echo` | `EchoRunner` | 回显指令，用于 pipeline 烟囱测试与 anti-echo 检查 | 否 |

> 四种 Runner 全部内置于 v1.0，无需注册即可使用。自定义 Runner 需实现 `BaseRunner` 子类并调用 `register_runner()`（P2 扩展点，v1.0 未提供外部插件加载）。

---

## 2. Agent 卡片字段手册

```yaml
name: kaoyan_chain            # 必填。结果目录名会用到，建议与文件名一致
type: command                 # command | http | mock | echo
version: "2.8.0"              # 写入 Run.agent_version，回归对比时用于区分版本
model: deepseek-chat          # 写入 Run.model（用于「评测模型 ≠ 被测模型」校验）
provider: openai_compatible   # 自由文本，仅记录

# ---- command 类型专用 ----
command:                      # argv 模板，逐元素渲染（见第 3 节占位符）
  - "kaoyan"
  - "run"
  - "--prompt-file"
  - "{prompt_file}"
stdin: none                   # none | prompt；prompt = 把任务指令写入子进程 stdin
cwd: "{workspace}"            # 工作目录模板，默认 {workspace}
env:                          # 固定注入的键值（不含密钥！密钥走 env_passthrough）
  KAOYAN_CHAIN_MODE: "batch"
env_passthrough:              # 从父进程环境「按名透传」的变量名
  - OPENAI_BASE_URL
timeout_grace_sec: 15         # 超时后 SIGTERM→SIGKILL 的宽限秒数

# ---- http 类型专用 ----
base_url: https://api.example.com/v1
api_key_env: OPENAI_API_KEY   # 只写变量名；值从运行环境读取，绝不落盘

# ---- 输出解析 ----
parse:
  format: json                # json | jsonl | text
  answer_field: final_answer
  toolcalls_field: tool_calls
  sources_field: sources
  citations_field: citations
  usage_field: usage
  search_trace_field: search_trace
  fallbacks: [jsonl, text]    # 主格式解析失败时的降级顺序

tool_call_mapping:            # 外部字段名 → 内部字段名（兼容不同 CLI 命名）
  name: name
  args: args
  result: result
  status: status
  started_at: started_at
  duration_ms: duration_ms

params:
  usage_file: true            # 允许 Agent 通过 KAOYANBENCH_USAGE_FILE 回报用量
  pricing:                    # 可选：token 单价，用于把 tokens 折算成 cost_usd
    input_per_1k: 0.001
    output_per_1k: 0.002

answers:                      # 仅 mock 类型使用：{task_id: 预设答案}
  "*":
    final_answer: "（兜底答案）"
```

---

## 3. 模板占位符（`command` / `cwd` 内可用）

渲染由 `CommandRunner.render_template()` 完成，**故意不用 `str.format`**，避免指令文本里的 `{}` 触发 `KeyError`。

| 占位符 | 含义 |
|---|---|
| `{prompt_file}` | `<workspace>/prompt.txt`，任务指令已写入该文件 |
| `{workspace}` | 本次运行的独立工作目录（只读隔离，含 `references/`） |
| `{answer_file}` | `<workspace>/answer.json`，Agent 可把结构化答案写这里 |
| `{usage_file}` | `<workspace>/usage.json`，Agent 可把 token/cost 写这里 |
| `{task_id}` | 任务 ID，如 `SEARCH-011` |
| `{instruction}` | 任务指令原文（短指令可直接内联到 argv） |
| `{task_dir}` | ⚠️ 已废弃：为向后兼容保留，解析到 workspace 隔离目录（不再是真实任务目录，防 Agent 直读 `task.json` 作弊；对标 METR 环境/评分分离）。新配置请用 `{workspace}` / `{references}` |
| `{references}` | `<workspace>/references`，fixture 副本 |
| `{output_dir}` | `<workspace>/outputs` |

同时注入以下环境变量，Agent 侧可直接读取：

| 环境变量 | 值 |
|---|---|
| `KAOYANBENCH_TASK_ID` | 任务 ID |
| `KAOYANBENCH_TASK_DIR` | ⚠️ 已废弃：指向 workspace 隔离目录（v1.1 起不再是真实任务目录） |
| `KAOYANBENCH_WORKSPACE` | workspace 隔离目录绝对路径 |
| `KAOYANBENCH_ANSWER_FILE` | `answer.json` 绝对路径 |
| `KAOYANBENCH_USAGE_FILE` | `usage.json` 绝对路径 |
| `KAOYANBENCH_REPLAY_DIR` | 离线回放目录（仅 `--offline-replay` 时设置，L2 占位） |

---

## 4. 输出解析：三级回退

`parse.format` 声明主格式；解析失败时按 `parse.fallbacks` 依次尝试，最终回退 `text`。
任何一次回退都会在 `Run.errors` 里留一条 `PARSING_FAILURE`，**不静默**。

| 格式 | 行为 |
|---|---|
| `json` | 解析整段 stdout 为单个 JSON 对象，按 `*_field` 取值 |
| `jsonl` | 逐行解析，取最后一条含 `*_field` 的记录 |
| `text` | 整段 stdout 作为 `final_answer`；`answer_file` 存在则优先用文件内容 |

`answer_field` 缺失或为空时，Runner 会再尝试读 `{answer_file}`（若 Agent 写了该文件）。

---

## 5. usage 采集：四级回退（绝不填 0）

`extract_usage()` 按顺序尝试，结果记录在 `Usage.usage_source`：

| 顺序 | `usage_source` | 来源 | `estimated` |
|---|---|---|---|
| 1 | `agent_reported` | stdout JSON 的 `usage_field` 段 | false |
| 2 | `usage_file` | `KAOYANBENCH_USAGE_FILE` 指向的 JSON | false |
| 3 | `estimated_from_chars` | `utf8_len(stdout + final_answer) / 4` 估算 | **true** |
| 4 | `none` | 都拿不到 → 全字段 `null` | — |

> **不编造数据**：拿不到就是 `null`，报告里 `usage_missing_count` 会如实计数。

---

## 6. 沙箱与安全边界

每次 run 都会：

- 创建独立 workspace（`results/workspaces/<run_id>/`），任务 `references/` 复制进去，Agent 只看得见自己那份；
- 环境变量**白名单**：只透传 `PATH / LANG / LC_ALL / LC_CTYPE / TZ / TERM / PYTHONPATH / SYSTEMROOT`，默认**不含任何 `*_API_KEY`**；
- `HOME / TMPDIR / XDG_CACHE_HOME` 重定向到 workspace，防缓存污染；
- 超时控制：`SIGTERM` → 宽限 `timeout_grace_sec` → `SIGKILL`；
- 需要密钥时必须在 `env_passthrough` 显式声明。

> ⚠️ **`env_passthrough` 写敏感变量会触发 `UserWarning`**（`sandbox.sensitive_env_warnings`）。
> 命中 `API_KEY / APIKEY / TOKEN / SECRET / PASSWORD / PASSWD / CREDENTIAL / PRIVATE_KEY / SESSION / COOKIE` 等模式即告警。
> 显式声明仍然生效（不拦截），但你必须确认该密钥确实需要传给被测进程。

---

## 7. 完整示例：kaoyan_chain

仓库内 `config/agents/kaoyan_chain.yaml` 就是一份可直接照抄的契约：

```yaml
name: kaoyan_chain
type: command
version: "2.8.0"
command: ["kaoyan", "run", "--prompt-file", "{prompt_file}", "--workspace", "{workspace}",
          "--answer-file", "{answer_file}", "--usage-file", "{usage_file}", "--json"]
stdin: none
env_passthrough: [OPENAI_BASE_URL, OPENAI_API_KEY, KAOYAN_CHAIN_CONFIG]
parse:
  format: json
  answer_field: final_answer
  toolcalls_field: tool_calls
  sources_field: sources
  citations_field: citations
  usage_field: usage
  fallbacks: [jsonl, text]
params:
  usage_file: true
```

它要求被测 CLI 满足：

1. 接受 `--prompt-file` / `--workspace` / `--answer-file` / `--usage-file` / `--json` 五个参数；
2. 把结构化结果以**单个 JSON 对象**打到 stdout，顶层含 `final_answer`（字符串）；
3. 可选回报 `tool_calls` / `sources` / `citations` / `usage` 四个数组/对象。

> 这五个参数是 KaoyanBench 与被测 CLI 的**契约**，不是 KaoyanBench 提供的功能。
> 接入前请先用第 8 节的冒烟步骤确认契约成立。

---

## 8. 接入冒烟（必做，5 分钟）

在跑 `core50` 之前，先确认「一张卡片 + 一次真实调用」能跑通：

```bash
# 1. 只跑 1 个任务、1 次，看是否能拿到非空 final_answer
kaoyanbench run --task EXAM-001 --agent kaoyan_chain --tag smoke-1

# 2. 看 run 明细：final_answer 是否非空？errors 里有没有 PARSING_FAILURE？
kaoyanbench suite --suite "(tasks)"

# 3. 生成报告，检查 grader_mode 与 degraded_task_count
kaoyanbench report --suite "(tasks)" --tag smoke-1
```

判读：

| 现象 | 含义 | 处理 |
|---|---|---|
| `success_rate=0%` 且 `errors` 含 `TOOL_FAILURE` / `spawn_error` | CLI 不存在或 argv 模板不对 | 检查 `command` 与 PATH |
| `errors` 含 `PARSING_FAILURE` | stdout 不是预期 JSON | 调整 `parse.format` / `*_field` |
| `final_answer` 为空但无 error | Agent 未按契约写 `answer_file` | 检查 `{answer_file}` 传参 |
| `grader_mode=degraded` | 无 `GRADER_API_KEY`，走了规则化降级评分 | 见 `docs/04` 第 6 节 |

---

## 9. 常见问题

**Q：Agent 是 HTTP 服务，没有 CLI 怎么办？**
用 `type: http` + `base_url` + `api_key_env`。`HttpRunner` 发 OpenAI 兼容的 `chat/completions` 请求，响应体按 `parse` 段解析。

**Q：Agent 输出多行 JSON 或带日志前缀？**
把 `parse.format` 设为 `jsonl`，或把 `text` 放在 `fallbacks` 首位。`text` 模式下整段 stdout 都算答案，评分器仍会按 checks 判定。

**Q：怎么让 Agent 知道该输出什么字段？**
`{prompt_file}` 里已经写好了任务指令与 `answer_format` 要求（由 `workspace._build_prompt` 生成），无需额外传参。

**Q：能不能同时评测多个 Agent？**
可以。同一 suite 用不同 `--agent` 跑，结果按 `(suite_id, agent, tag)` 分开存放；`compare` / `regression` 默认只在同一 agent 内比较。

**Q：为什么评测模型必须与被测模型不同？**
`config/default.yaml` 的 `model`（被测）与 `grader_model`（评测）必须不同源，避免自我偏差。违反时配置校验阶段直接抛 `ConfigError`，除非 `allow_same_model: true`（仅 mock/echo 自测允许）。
