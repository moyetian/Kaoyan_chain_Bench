# 03 · Onboarding a New Agent

> **English** | [中文](03-接入新Agent.md)

> Audience: engineers plugging agents such as Claude Code / Codex / OpenCode / WorkBuddy /
> kaoyan_chain into KaoyanBench.
> Upstream basis: `docs/01-需求拆解与方案定稿.md` §5.2 (Runner protocol), §2.2 (Runner feature list) — Chinese-only.
> Status: matches the v1.0 implementation (`src/kaoyanbench/core/runner.py`, `core/runners/*`, `core/sandbox.py`).

---

## 1. Onboarding = one YAML card

KaoyanBench does **not** require the agent under test to ship a Python adapter. Onboarding is two steps:

1. Write an **agent card** at `config/agents/<name>.yaml` declaring "how to invoke, how to parse output";
2. Run it with `kaoyanbench run --agent <name>`.

The card is carried by `AgentSpec` (`core/models.py`); the `type` field selects which built-in Runner to use:

| `type` | Runner | Purpose | Network |
|---|---|---|---|
| `command` | `CommandRunner` | Invoke an external CLI (primary: `ky` / `claude -p` / `codex exec` / `opencode run`) | Depends on the CLI under test |
| `http` | `HttpRunner` | OpenAI-compatible endpoint, for harnesses without a CLI | Yes |
| `mock` | `MockRunner` | Emit canned answers, **run the whole pipeline offline** (CI workhorse) | No |
| `echo` | `EchoRunner` | Echo the instruction back, for pipeline smoke tests and anti-echo checks | No |

> All four Runners are built into v1.0, no registration needed. A custom Runner must subclass
> `BaseRunner` and call `register_runner()` (a P2 extension point; v1.0 offers no external plugin loading).

---

## 2. Agent card field manual

```yaml
name: kaoyan_chain            # Required. Used in result directory names; keep it equal to the file name
type: command                 # command | http | mock | echo
version: "2.8.0"              # Written to Run.agent_version; distinguishes versions in regression
model: deepseek-chat          # Written to Run.model (used for the "grader model ≠ model under test" check)
provider: openai_compatible   # Free text, record only

# ---- command type only ----
command:                      # argv template, rendered element-wise (see §3 placeholders)
  - "kaoyan"
  - "run"
  - "--prompt-file"
  - "{prompt_file}"
stdin: none                   # none | prompt; prompt = write the task instruction to the child stdin
cwd: "{workspace}"            # Working-directory template, default {workspace}
env:                          # Fixed key/values to inject (no secrets! secrets go via env_passthrough)
  KAOYAN_CHAIN_MODE: "batch"
env_passthrough:              # Variable names passed through by name from the parent environment
  - OPENAI_BASE_URL
timeout_grace_sec: 15         # Grace seconds between SIGTERM and SIGKILL on timeout

# ---- http type only ----
base_url: https://api.example.com/v1
api_key_env: OPENAI_API_KEY   # Variable name only; the value is read from the runtime environment, never persisted

# ---- output parsing ----
parse:
  format: json                # json | jsonl | text
  answer_field: final_answer
  toolcalls_field: tool_calls
  sources_field: sources
  citations_field: citations
  usage_field: usage
  search_trace_field: search_trace
  fallbacks: [jsonl, text]    # Fallback order when the primary format fails to parse

tool_call_mapping:            # External field name → internal field name (tolerates CLI naming differences)
  name: name
  args: args
  result: result
  status: status
  started_at: started_at
  duration_ms: duration_ms

params:
  usage_file: true            # Allow the agent to report usage via KAOYANBENCH_USAGE_FILE
  pricing:                    # Optional: per-token prices to convert tokens into cost_usd
    input_per_1k: 0.001
    output_per_1k: 0.002

answers:                      # mock type only: {task_id: canned answer}
  "*":
    final_answer: "(fallback answer)"
```

---

## 3. Template placeholders (usable inside `command` / `cwd`)

Rendering is done by `CommandRunner.render_template()`, which **deliberately avoids `str.format`**
so that `{}` in instruction text never triggers `KeyError`.

| Placeholder | Meaning |
|---|---|
| `{prompt_file}` | `<workspace>/prompt.txt`; the task instruction is already written there |
| `{workspace}` | This run's isolated working directory (read-only isolation, contains `references/`) |
| `{answer_file}` | `<workspace>/answer.json`; the agent may write its structured answer here |
| `{usage_file}` | `<workspace>/usage.json`; the agent may write tokens/cost here |
| `{task_id}` | Task ID, e.g. `SEARCH-011` |
| `{instruction}` | Raw task instruction (short ones can be inlined into argv) |
| `{task_dir}` | ⚠️ Deprecated: kept for backward compatibility, resolves to the workspace isolation dir (no longer the real task directory — prevents agents from reading `task.json` directly to cheat; mirrors the METR environment/grading split). New configs should use `{workspace}` / `{references}` |
| `{references}` | `<workspace>/references`, a copy of the fixtures |
| `{output_dir}` | `<workspace>/outputs` |

The following environment variables are also injected for the agent to read:

| Variable | Value |
|---|---|
| `KAOYANBENCH_TASK_ID` | Task ID |
| `KAOYANBENCH_TASK_DIR` | ⚠️ Deprecated: points at the workspace isolation dir (no longer the real task dir since v1.1) |
| `KAOYANBENCH_WORKSPACE` | Absolute path of the workspace isolation dir |
| `KAOYANBENCH_ANSWER_FILE` | Absolute path of `answer.json` |
| `KAOYANBENCH_USAGE_FILE` | Absolute path of `usage.json` |
| `KAOYANBENCH_REPLAY_DIR` | Offline replay dir (set only with `--offline-replay`, L2 placeholder) |

---

## 4. Output parsing: three-level fallback

`parse.format` declares the primary format; on failure each of `parse.fallbacks` is tried in order,
with `text` as the final fallback. Every fallback leaves a `PARSING_FAILURE` entry in `Run.errors` —
**never silent**.

| Format | Behavior |
|---|---|
| `json` | Parse the whole stdout as one JSON object, extract values via `*_field` |
| `jsonl` | Parse line by line, take the last record containing `*_field` |
| `text` | The whole stdout becomes `final_answer`; if `answer_file` exists its content wins |

When `answer_field` is missing or empty, the Runner also tries reading `{answer_file}` (if the agent wrote it).

---

## 5. Usage collection: four-level fallback (never fills in 0)

`extract_usage()` tries in order; the outcome is recorded in `Usage.usage_source`:

| Order | `usage_source` | Source | `estimated` |
|---|---|---|---|
| 1 | `agent_reported` | The `usage_field` section of the stdout JSON | false |
| 2 | `usage_file` | JSON pointed to by `KAOYANBENCH_USAGE_FILE` | false |
| 3 | `estimated_from_chars` | `utf8_len(stdout + final_answer) / 4` estimate | **true** |
| 4 | `none` | Nothing available → all fields `null` | — |

> **No fabricated data**: unavailable means `null`, and reports count the gap honestly via `usage_missing_count`.

---

## 6. Sandbox and safety boundaries

Every run:

- creates an isolated workspace (`results/workspaces/<run_id>/`); the task's `references/` are copied
  in, and the agent only ever sees its own copy;
- uses an environment-variable **allowlist**: only `PATH / LANG / LC_ALL / LC_CTYPE / TZ / TERM /
  PYTHONPATH / SYSTEMROOT` pass through, **no `*_API_KEY` by default**;
- redirects `HOME / TMPDIR / XDG_CACHE_HOME` into the workspace to prevent cache pollution;
- enforces timeouts: `SIGTERM` → `timeout_grace_sec` grace → `SIGKILL`;
- requires secrets to be explicitly declared in `env_passthrough`.

> ⚠️ **Writing sensitive variables in `env_passthrough` raises `UserWarning`**
> (`sandbox.sensitive_env_warnings`). Anything matching `API_KEY / APIKEY / TOKEN / SECRET /
> PASSWORD / PASSWD / CREDENTIAL / PRIVATE_KEY / SESSION / COOKIE` triggers the warning.
> An explicit declaration still takes effect (nothing is blocked), but you must confirm the key
> really needs to reach the process under test.

---

## 7. Full example: kaoyan_chain

The in-repo `config/agents/kaoyan_chain.yaml` is a contract you can copy verbatim:

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

It requires the CLI under test to:

1. accept the five flags `--prompt-file` / `--workspace` / `--answer-file` / `--usage-file` / `--json`;
2. print the structured result to stdout as a **single JSON object** with a top-level `final_answer` (string);
3. optionally report the `tool_calls` / `sources` / `citations` / `usage` arrays/objects.

> These five flags are the **contract** between KaoyanBench and the CLI under test, not features
> KaoyanBench provides. Before onboarding, confirm the contract holds using the §8 smoke steps.

---

## 8. Onboarding smoke test (required, 5 minutes)

Before running `core50`, confirm "one card + one real call" works end to end:

```bash
# 1. Run just 1 task once; check for a non-empty final_answer
kaoyanbench run --task EXAM-001 --agent kaoyan_chain --tag smoke-1

# 2. Inspect the run: is final_answer non-empty? Any PARSING_FAILURE in errors?
kaoyanbench suite --suite "(tasks)"

# 3. Generate a report; check grader_mode and degraded_task_count
kaoyanbench report --suite "(tasks)" --tag smoke-1
```

Reading the signs:

| Symptom | Meaning | Fix |
|---|---|---|
| `success_rate=0%` with `TOOL_FAILURE` / `spawn_error` in `errors` | CLI missing or argv template wrong | Check `command` and PATH |
| `PARSING_FAILURE` in `errors` | stdout is not the expected JSON | Adjust `parse.format` / `*_field` |
| Empty `final_answer` with no error | Agent didn't write `answer_file` per contract | Check the `{answer_file}` argument |
| `grader_mode=degraded` | No `GRADER_API_KEY`, fell back to rule-based degraded grading | See `docs/04` §6 (field reference) / `04-报告字段说明.en.md` §11 |

---

## 9. FAQ

**Q: My agent is an HTTP service with no CLI. What now?**
Use `type: http` + `base_url` + `api_key_env`. `HttpRunner` sends OpenAI-compatible
`chat/completions` requests and parses the response body per the `parse` section.

**Q: My agent prints multi-line JSON or log prefixes?**
Set `parse.format` to `jsonl`, or put `text` first in `fallbacks`. In `text` mode the whole stdout
counts as the answer, and the grader still judges it against the checks.

**Q: How does the agent know which fields to output?**
`{prompt_file}` already contains the task instruction and the `answer_format` requirements
(generated by `workspace._build_prompt`) — no extra arguments needed.

**Q: Can I evaluate multiple agents at once?**
Yes. Run the same suite with different `--agent` values; results are stored separately under
`(suite_id, agent, tag)`. `compare` / `regression` only compare within the same agent by default.

**Q: Why must the grader model differ from the model under test?**
`model` (under test) and `grader_model` (grader) in `config/default.yaml` must come from different
sources to avoid self-grading bias. A violation raises `ConfigError` at config-validation time,
unless `allow_same_model: true` (allowed only for mock/echo self-tests).
