# Private Set（隐藏测试集）

本目录为 KaoyanBench 的隐藏测试集（Private Set）。

## 公开仓库政策（重要）

**私测题目本身绝不进入公开仓库。** 本仓库仅公开：

- 加载与隔离机制（`--split private`、`benchmark/suites/private_demo.yaml`、`validate`）；
- 确定性生成器 `tools/build_private_tasks.py`（题目结构公开，生成的数据本地持有）；
- 本 README（规范说明）。

题目目录（`benchmark/tasks/private/<category>/…`）已被 `.gitignore` 忽略。
私测维护者在受控环境执行 `python tools/build_private_tasks.py` 本地生成 10 题
（planning 3 / hallucination 3 / exam 2 / university 2，全 offline + deterministic），
再跑 `kaoyanbench validate --suite private_demo` 验证。
`tests/test_p2.py::test_private_tasks_solvable_by_construction` 在题目存在时逐题做
可解性证明；题目不存在时自动跳过（公库 CI 即如此）。

注意：默认 `mock` Runner 对私测题无预设答案，跑 `private_demo` 会接近 0 分——
这正是隐藏集的意义（只测真实 Agent，不刷 mock 分）；不要为提分给 mock 加私测预设。

## 如何新增一个 Private 任务（规范）

1. 目录结构与 public **完全一致**：
   `benchmark/tasks/private/<category>/<TASK_ID>/{task.json, references/, README.md}`。
   推荐直接扩展 `tools/build_private_tasks.py`（确定性、可 `--check` 校验），
   而不是手写（手写易破坏跨平台字节稳定性，见该脚本 `_write_text` 注释）。
2. `task.json` 的 `split` 可省略（由目录推导为 `private`）；若显式写必须为 `"private"`，否则 `validate` 报 error。
3. 带 `ground_truth` 的 offline 题必须配 `references/` + `manifest.json`（sha256），
   满足 `validate_tasks` 硬规则；文件必须按字节写入（禁止文本模式换行符翻译）。
4. 每题附 README，写明考点、陷阱、ground truth 依据（同 public，见 `docs/02-任务集规范.md` §7）。
5. 判定词不得原样出现在 `instruction` 里（anti-echo，`validate` 会 warn）。
6. 提交 private 题时，PR 由 ≥2 名维护者 review，且 reviewer 不得是被测 Agent 的作者（避免针对性过拟合）。

## 发布流程与防污染要求

- private 题**只在受控环境/CI 私密仓库**中运行；不得随 public 仓库分发。
- `--split private` 只加载 private 目录，`kaoyanbench list --split private` 不应出现任何 public 任务。
- private 的 ground truth 同样遵守方案 6.5 硬规则（真实院校 → `ground_truth=null`）。
- 与 public 集保持同构但数据零重叠（过拟合探测：public 高分 + private 低分即告警）。

详见 `docs/05-roadmap.md`（Phase 2 规划）。
