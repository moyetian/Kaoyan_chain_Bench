# KaoyanBench

> **KaoyanBench is an open benchmark for evaluating AI agents on graduate-entrance-examination research, information retrieval, document analysis, evidence verification, planning, and long-horizon workflows.**
>
> **KaoyanBench 是面向考研场景的 AI Agent 综合评测基准，用于测试信息检索、院校研究、招生政策分析、资料处理、证据验证、学习规划以及长流程自动化能力。**

配套项目：[`kaoyan_chain`](https://github.com/moyetian/kaoyan_chain)（考研学习链）。KaoyanBench 是**独立项目**，不依赖其源码，仅通过插件式 Runner 适配层接入。

> 当前版本 **v1.1.0**（见 [CHANGELOG](CHANGELOG.md)）：23 个联网任务配齐**合成镜像快照**
> （虚构回放语料，用于 `--offline-replay` 链路验证；真实网页快照待 `snapshot fetch`
> 抓取并版本化，`validate --require-snapshots` 门禁）、Agent 与任务目录隔离防作弊、
> 预算口径与复评一致性修复。Private 隐藏题**不在公开仓库中**
> （仅机制与生成器公开，见 `benchmark/tasks/private/README.md`）。

## 设计原则

1. **可重复**：固定任务、固定 fixture、固定 seed、网页快照版本化。
2. **可自动评分**：v1.0 的 50 个任务 100% 可自动评分（DoD #4 口径为**自动评分覆盖率**，
   含 semantic；其中 24 个（48%）为纯确定性评分，不依赖评测模型）。
3. **可持续回归**：`compare` + `regression` 门禁，失败即 CI FAIL。
4. **零第三方依赖**：核心链路（任务加载 / Runner / Logger / Grader / Reporter / CLI / 回归）纯标准库实现，Python ≥3.10 可跑。
5. **不编造数据**：涉及真实院校当年具体数字的任务，ground truth 一律为空，只做要点命中 + 来源等级 + 年份标识判定；所有 fixture 内院校均为虚构。

## v1.0 范围

| 项 | 内容 |
|---|---|
| 主测试集 | `core50`：50 题 / 8 类 / Public |
| 类别 | 搜索 12、院校 9、政策 5、真题 5、PDF 5、规划 5、多步骤研究 4、抗幻觉 5 |
| 难度 | Easy 10 / Medium 18 / Hard 15 / Expert 7 |
| 评分 | 100 分制 7 维 + 11 项关键指标 |
| 报告 | JSON（事实源）+ Markdown + 单文件 HTML |
| 不做 | Leaderboard 网站、SWE-bench 全量接入、扫描件 OCR、Human Grader UI |

完整方案见 [`docs/01-需求拆解与方案定稿.md`](docs/01-需求拆解与方案定稿.md)。

## 快速开始

```bash
pip install -e .

# 1. 校验任务集（CI 必跑）
kaoyanbench validate

# 2. 生成 fixtures（离线任务自带数据）
kaoyanbench fixtures build

# 3. 离线烟囱测试（不需要任何 Agent / API Key）
kaoyanbench run --suite smoke --agent mock --tag smoke-1
kaoyanbench report --suite smoke --tag smoke-1

# 4. 接入真实 Agent（示例：kaoyan_chain CLI）
kaoyanbench run --suite core50 --agent kaoyan_chain --tag v2.8.0 --runs 3

# 5. 版本回归
kaoyanbench regression --baseline v2.7.0 --current v2.8.0 --suite core50
```

## 目录结构

```
config/      配置（default.yaml + agents/*.yaml + graders/*.yaml）
benchmark/   任务集（tasks/{public,private}、suites/、fixtures/、snapshots/）
src/kaoyanbench/  源码（core/、reporters/、utils/）
tools/       fixtures 生成器、快照抓取、Phase3 外部基准导入占位
docs/        文档（01 方案定稿 / 02 任务集规范 / 03 接入新 Agent / 04 报告字段 / 05 roadmap）
results/     运行留痕（JSONL + SQLite，gitignore）
reports/     报告产物（gitignore）
```

## 评测模型与被测模型必须分离

KaoyanBench 强制 `config/default.yaml` 中的 `model`（被测）与 `grader_model`（评测）不同源，
避免自我偏差。若评测模型不可用（无 key / 无网络），结果会显式标注 `grader_mode: "degraded"`
并在报告中显示红色告警横幅，**绝不静默假装评过**。

## License

MIT，见 [LICENSE](LICENSE)。
