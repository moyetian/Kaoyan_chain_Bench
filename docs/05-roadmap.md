# 05 · Roadmap（KaoyanBench 发展路线）

> 任务：D-03　｜　范围：Phase 2 / Phase 3 / Phase 4
> 本文承接方案 `docs/01-需求拆解与方案定稿.md` 第 6.1 节的取舍说明。

---

## 0. 总览

| Phase | 主题 | 任务规模 | 状态 |
|---|---|---|---|
| **v1.0（MVP）** | 8 类 50 题 Public 集 + 完整评测引擎 + CI 门禁 | 50 题 / Public | ✅ 已交付 |
| **Phase 2** | 扩到 100 Public + 100 Private，回归完整 baseline 配比 | 100 + 100 | 规划中 |
| **Phase 3** | 接入 GeneralBench（SWE-bench / Terminal-Bench / Browser） | 外部基准 | 规划中 |
| **Phase 4** | 公开 Leaderboard | 服务化 | 规划中 |

---

## 1. v1.0 基线（已交付）

- 主测试集 `core50`：**50 题 / 8 类 / Public**。
- 类别：search 12、university 9、policy 5、exam 5、pdf 5、planning 5、research 4、hallucination 5。
- 难度：Easy 10 / Medium 18 / Hard 15 / Expert 7。
- 自动评分：100%（纯确定性 24/50 = 48%）。
- CI：PR 门禁只跑 `smoke`（6 题全离线）+ 回归比对；完整 `core50` 作 nightly / 发版门禁。

### 1.1 与《原方案》第 36 节 MVP 配比的差异（必须留档）

原 MVP 采用 **6 类**，配比为 **20 / 10 / 5 / 5 / 5 / 5**（Search / University / Policy / PDF / Planning / Hallucination）。
该配比**无法满足**验收标准第 2 条「至少 8 个任务类别」，故 v1.0 从其让位，新增 Exam 与 Research：

| 类别 | MVP 配比 | v1.0 定稿 | 变化 |
|---|---:|---:|---:|
| Search（搜索与信息检索） | 20 | 12 | −8 |
| University（院校与招生） | 10 | 9 | −1 |
| Policy（政策与时效） | 5 | 5 | 0 |
| PDF（资料处理） | 5 | 5 | 0 |
| Planning（学习规划） | 5 | 5 | 0 |
| Hallucination（抗幻觉） | 5 | 5 | 0 |
| **Exam（真题与数据分析）** | — | **5** | +5（新增） |
| **Research（多步骤研究）** | — | **4** | +4（新增） |
| 合计 | 50 | 50 | — |

> **★ 原 MVP 的 `20 / 10 / 5 / 5 / 5 / 5` 配比，作为 Phase 2 扩到 100 题时的基线比例写在此处留档。**

---

## 2. Phase 2 —— 规模扩展（100 Public + 100 Private）

**目标**：在保留 8 类覆盖的前提下，把测试集扩到 100 Public + 100 Private，建立反过拟合的长效机制（方案 R6）。

### 2.1 100 题 Public 集的类别配比（**以 MVP 6 类 20/10/5/5/5/5 为基线比例**）

以 MVP 的 `20 / 10 / 5 / 5 / 5 / 5`（= 50 题基线）按同一比例放大一倍到 100 题，再叠加 v1.0 新增的两类（Exam / Research），得到 Phase 2 的 100 题 Public 配比：

| 类别 | MVP 基线（×2 → 100） | Exam/Research 调入 | **Phase 2 Public 定稿** | 占比 |
|---|---:|---:|---:|---:|
| Search | 40 | −10 | **30** | 30% |
| University | 20 | −5 | **15** | 15% |
| Policy | 10 | 0 | **10** | 10% |
| Exam | — | +10 | **10** | 10% |
| PDF | 10 | 0 | **10** | 10% |
| Planning | 10 | 0 | **10** | 10% |
| Research | — | +8 | **8** | 8% |
| Hallucination | 10 | −3 | **7** | 7% |
| **合计** | **100** | 0 | **100** | 100% |

> 配比原则：① 保留 MVP 6 类的**相对基线比例**（Search 仍为第一大类）；② Exam/Research 从 Search/University 让位，与 v1.0 的取舍方向一致；③ 8 类每类 ≥7 条，保证分组统计有意义。

### 2.2 100 题 Private 集

- **构造方式**：与 Public 同分布（同类别配比、同难度分布），但**题目内容、fixture、ground truth 全部不同**，且**不入公开仓库**。
- **用途**：Public 用于公开排名与回归；Private 用于**防过拟合抽查**与**发版前终审**。
- **发布流程**：Private 仅由维护者持有；CI 用 `private_demo` 验证 `--split private` 隔离正确（方案 R6、DoD#15）。
- **难度分布**：沿用 v1.0 的 Easy/Medium/Hard/Expert = 20%/35%/30%/15%（Medium 上取、Expert 下取）。

### 2.3 工程配套

- [ ] `benchmark/tasks/{public,private}` 各 100 题；`task.json` 的 `version` 字段规范（gt 变更必须升版）。
- [ ] **快照库补全**：v1.0 遗留的 19 道联网题快照建立（T-07），并纳入 `snapshot verify` 回归。
- [ ] **抗过拟合回归**：CI 增加 anti-echo / 抄题面 作弊探针的通过率上界断言（v1.0 已有探针思路，见 QA 报告 R4）。
- [ ] **成本口径统一**（方案 R4）：跨 Agent 的成本对比升为 Gate 可选项。
- [ ] Private 集发布流程与防污染规范文档化。

---

## 3. Phase 3 —— 接入 GeneralBench

**目标**：从「考研垂直场景」扩展到「通用 Agent 能力」评测，与业界基准互通。

| 子项 | 内容 | 接入方式 |
|---|---|---|
| **SWE-bench** | 真实 GitHub issue 修复 → 代码级评测 | 复用 `command` Runner + 容器化 workspace；引入 patch 应用与测试执行 grader |
| **Terminal-Bench** | 终端任务（shell 操作、文件处理） | 复用进程级沙箱 + 命令序列判定 |
| **Browser / WebArena 类** | 浏览器交互任务 | 新增 browser Runner（Playwright 类）；快照机制复用于离线回放 |

**共同工程**：
- [ ] 抽象「外部基准适配层」（`tools/` 下的 Phase3 导入占位已在 v1.0 预留）。
- [ ] 统一 `task.json` schema，允许外部基准映射到 KaoyanBench 的 `Task` 模型。
- [ ] 报告层支持多基准并列展示（不产出单一综合排名，延续方案第 35 节原则）。
- [ ] 沙箱升级为**容器级隔离**（v1.0 仅为进程级，Phase 3 因执行不受信代码必须升级）。

---

## 4. Phase 4 —— 公开 Leaderboard

**目标**：对外提供持续更新的公开排名与可比对的历史结果。

- **服务化**：
  - [ ] 结果收集服务：接收/校验提交的 `suite.json`（校验 schema + grader_mode + baseline 完整性）。
  - [ ] Leaderboard 前端：按能力维度分栏（Accuracy / Reliability / Efficiency），**不做单一总冠军**（方案第 35 节）。
  - [ ] 历史趋势与回归可视化。
- **治理**：
  - [ ] 提交需声明 `agent` 版本、`model`、`grader_mode`；degraded 结果单独标注，不参与排名。
  - [ ] 反作弊：Private 集抽查 + anti-echo 探针常态化。
  - [ ] 数据时效：联网题快照版本化，排名标注快照日期。
- **合规**：
  - [ ] 保持「不编造真实院校数据」原则（方案 R3）在对外结果的可见性。
  - [ ] 报告模板中英文双语（延续 README 文案）。

---

## 5. 里程碑（粗排，非承诺）

| 里程碑 | 内容 | 依赖 |
|---|---|---|
| v1.1 | 补 23 题快照、CI 加 anti-echo 断言 | v1.0 |
| v2.0 | Phase 2：100 Public + 100 Private + 完整 baseline | v1.1 |
| v2.x | Phase 3：SWE-bench / Terminal-Bench / Browser 接入 | v2.0 + 容器隔离 |
| v3.0 | Phase 4：公开 Leaderboard | v2.x |
