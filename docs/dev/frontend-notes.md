# 前端（报告渲染层）交付说明 — KaoyanBench v1.0

> 负责人：前端工程师「方砚」
> 依据：`docs/01-需求拆解与方案定稿.md` 第 5.6 / 5.7 节、第 7 节 F-02/F-03/F-04
> 约束：**零第三方依赖**（渲染层只用 Python 标准库）；只消费 `ReportModel`，不二次读文件（方案 5.6）
> 状态：Markdown / HTML / CSV 三格式 + SVG 图表资产 + 校验脚本全部落地，`pytest tests/` 全绿（240 passed），已用 `/tmp/kybdemo` 副本端到端实测。

---

## 1. 已实现清单

| 文件 | 职责 | 状态 |
| --- | --- | --- |
| `src/kaoyanbench/reporters/markdown_reporter.py` | 产出 `report.md`（F-02）：总体指标 / 总分构成 / 按类别 / 按难度 / 错误分类 / TOP 失败任务 / 版本对比 / 运行环境 | ✅ |
| `src/kaoyanbench/reporters/html_reporter.py` | 产出**单文件** `report.html`（F-04）：内联 CSS/JS/SVG，渲染 5.7 的 **12 类元素** | ✅ |
| `src/kaoyanbench/reporters/csv_reporter.py` | 产出 `report.csv`（P1）：`tasks[]` 拉平宽表，UTF-8 BOM | ✅ |
| `src/kaoyanbench/reporters/assets/svg.py` | 手写 SVG 图表生成器（F-03）：堆叠条 / 分组柱 / 直方图 / 横向条 / 双条 / 迷你趋势 | ✅ |
| `tools/check_report_html.py` | 12 类元素 DOM 断言 + 零外部请求 + HTML 结构校验，输出逐项 PASS/FAIL | ✅ |
| `tools/gen_demo_benchmark.py` | 生成离线演示项目副本（14 题 / 8 类），供渲染端到端验收（**零侵入**，不入库） | ✅ |

三个 Reporter 均在模块内调用 `register_reporter(...)` **自我注册**，后端 `core/reporter.py` 的懒加载表已预留槽位，**未改动 `core/**`**。

### 1.1 自我注册

```python
def _register() -> None:
    try:
        from ..core.reporter import register_reporter
        register_reporter("html", HtmlReporter())
    except Exception:  # 注册失败不应拖垮模块导入
        pass
_register()
```

实测（仓库根）：

```text
$ python -c "from kaoyanbench.core.reporter import available_reporters, resolve_reporter; print(available_reporters()); ..."
available: ['json', 'markdown', 'html', 'csv']
  json      -> JsonReporter
  markdown  -> MarkdownReporter
  md        -> MarkdownReporter
  html      -> HtmlReporter
  csv       -> CsvReporter
```

---

## 2. 12 类元素 ↔ DOM 选择器对照表

`html_reporter.py` 的 `ELEMENT_SELECTORS` 是**唯一事实源**，`tools/check_report_html.py` 直接复用同一常量，杜绝实现与验收两处漂移。

| # | 元素 | 数据源 | DOM 选择器 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | 顶部告警横幅 | `run.grader_mode != "full"` | `#degraded-banner` | 非 full 时 `data-show="1"` + `banner-bad`（红色），写明 degraded 任务数；full 时 `data-show="0"` |
| 2 | 12 个指标卡 | `summary` | `#metric-cards .metric-card` | 数值 + 单位（`mc-unit`）；全轮估算时 `tokens/cost` 打「估算」角标；`None` → `—` |
| 3 | 总分构成堆叠条 | `score_breakdown_mean`（7 维） | `#chart-breakdown svg.chart-stacked` | 条长 = `total`；含 `svg-c1..c7` 调色板 |
| 4 | 按类别能力柱状图 | `by_category` | `#chart-category svg.chart-grouped` | 3 系列：成功率 / 事实性 / 引用准确率 |
| 5 | 按难度成功率柱 | `by_difficulty` | `#chart-difficulty svg.chart-grouped` | X 轴标签带 `n=样本数` |
| 6 | Latency 分布直方图 | `tasks[].metrics.latency_seconds` | `#chart-latency svg.chart-histogram` | **前端分桶**（7 桶）+ P50/P90 标线 |
| 7 | 成本 TOP10 横向条 | `tasks[].metrics.cost_usd` | `#chart-cost svg.chart-hbars` | 仅统计非空成本；为空给空状态 |
| 8 | 错误分类条形 | `errors`（11 类） | `#chart-errors svg.chart-hbars` | `count=0` 也列出；附样例任务 |
| 9 | Pass@1 vs Pass@3 | `summary.pass_at_1/3` | `#chart-pass svg.chart-double` | 附差值 `pp` |
| 10 | 任务明细表 | `tasks[]` | `#task-table tbody#task-table-body` | 纯前端 JS 搜索/筛选/排序（见 §4） |
| 11 | 任务详情抽屉 | `tasks[].checks` + `trace_ref` | `#task-drawer .drawer-check-row` | 服务端首渲一条（禁 JS 也可读），JS 接管全部任务 |
| 12 | 版本对比表 | `comparison.deltas` + `gates` | `#comparison-section .gate-badge` | Δpp + 方向箭头（升红降绿）+ pass/fail/warn/skipped 徽章 |

---

## 3. 与 5.7 契约的差异清单

以下差异均**已按后端真实产出适配渲染**，不改后端。

### 3.1 `score_breakdown_mean` 维度分值口径（原「211.1%」问题）

- **后端真实口径**（`core/scorer.py:232-264`）：每个维度序列化的是 `max_scores[dim] × hit_ratio`，其中 `max_scores[dim] = DIMENSION_MAX[dim] × (total/100)`（无中性再分配时），**并且 `Σ max_scores == total`**（有 `neutral` 维度时按 `effective_weights` 重分配，`_reallocate_max_scores`）。因此：
  - **各维得分之和 ≡ `total`**（守恒）；
  - 单个维度得分**可以超过**方案 4.9 的 `DIMENSION_MAX`（例：只有事实性检查项时事实性得分可达 95 分）。
- **前端无法从 `report.json` 精确还原逐维等效满分**：`score_breakdown_mean` 只透出 `{dim: value, total}`，**没有**逐维 `max` 字段（逐维 `effective_weights` 在后端 `SuiteResult.grades[].score.effective_weights` 里，5.7 的 report.json 未透出）。
- **适配方案（Markdown §2 / HTML 元素 3）**：改用**守恒口径**——
  - Markdown 表列：`维度 | 得分 | 默认满分(方案 4.9) | 占 100 分比(value/total)`，合计行占比恒 `100.0%`；
  - HTML 堆叠条：段长 = 该维得分 / `total`，图例显示 `得分 / 等效满分(占比×total)`。
  - 这样既不再算出 `211.1%`，又**不编造**逐维满分；跨版本可比。
- **建议（给后端/主理人）**：若希望报告直接展示「该维等效满分」，请在 `score_breakdown_mean` 内增加逐维 `*_max`（或透出 `effective_weights`），前端即可精确渲染。当前已在两处渲染器的口径说明里注明该限制。

### 3.2 `by_category[]` 多出 `pass_at_1` / `pass_at_3`

5.7 的 `by_category` 示例未列出这两个键，但后端真实产出**包含** `pass_at_1` / `pass_at_3`（见 `reports/report.json` 与 `/tmp/kybdemo` 实测）。前端按「**契约是下界、多给的键忽略即可**」处理：当前渲染不消费它们，无副作用。建议 5.7 文档补齐。

### 3.3 `tasks[].category` / `difficulty` 为 `"unknown"`（后端派发缺口）

实测：`by_category[].label` 正确（=「搜索与信息检索」），但 `tasks[].category` / `tasks[].difficulty` / `network` 全为 `"unknown"`。原因：`cli.py:cmd_report` 调用 `build_report(...)` 时**未传 `task_meta`**，而 `build_report_model` 的 `_task_rows` 正依赖 `task_meta` 填这几列（`reporter.py:328-336`）。这属于**后端接线缺口**，前端不修（遵守「不改 `core/**`」）。

- **前端表现**：明细表类别/难度列显示「unknown」，不报错、不崩；筛选下拉不含真实类别（因为数据里没有）。
- **建议**：`cmd_report` 在组装 `SuiteResult` 时把 `registry.load_task` 的 `category/difficulty/network` 作为 `task_meta` 传入。

### 3.4 `errors[].sample_task_ids` / `trace_ref` 为相对路径

`trace_ref` 是相对仓库根路径，HTML 仅**文本展示**、不发起任何请求/跳转（单文件离线约束）。

---

## 4. HTML 关键实现要点

### 4.1 零外部请求

- CSS / JS / SVG **全部内联**；无 CDN、无外链字体（`font-family` 走系统字体栈 `system-ui, "PingFang SC", "Microsoft YaHei"` 等）；
- **不用 emoji 图标**：告警/关闭图标为内联 SVG（`_ICON_ALERT` / `_ICON_CLOSE`），状态用纯 CSS 圆点；
- 数据以 `<script type="application/json" id="report-data">` **内联**，运行时从 DOM 读取，**不发 fetch/XHR**；
- 唯一的 `http(s)://` 是 SVG 命名空间 `http://www.w3.org/2000/svg`（XML 标识符，**非网络请求**）。

### 4.2 深/浅色

- CSS 变量两套：`:root{…}`（浅色）+ `@media (prefers-color-scheme:dark){:root{…}}`（深色）；
- `<meta name="color-scheme" content="light dark">`；
- `svg.py` 的 `svg-c1..c8` 调色板在**两套变量里都给了实际颜色**（`svg.chart rect.svg-cN{fill:var(--svg-cN)}`），实测深色下 `--svg-c1` 自动切到 `#5b8dff`。

### 4.3 任务明细表（纯前端，零依赖）

- 搜索：`#task-search`（按 task_id 子串，大小写不敏感）；
- 筛选：`#task-filter-category` / `#task-filter-difficulty` / `#task-filter-result`（全部/通过/未通过）；
- 排序：`th.sortable[data-sort=task_id|score|latency|cost]`，点击切换升降，`aria-sort` 同步；
- 重置：`#task-reset`；计数：`#task-count-note`；
- 空状态：无匹配 → 「没有符合当前筛选条件的任务。」；无数据 → 「本轮没有任务明细数据。」；
- 50 行实测流畅（只操作内存数组 + 重建 tbody）。

### 4.4 可访问性 / 安全

- 抽屉 `role="dialog" aria-modal="true"`，打开聚焦关闭按钮、Esc 关闭、点击遮罩关闭、关闭后焦点归还触发元素；
- 表格 `caption.sr-only`、表头 `scope="col"`、排序 `aria-sort`；交互元素为 `<button>`；
- `:focus-visible` 可见焦点，**未**全局 `outline:none`；
- JS 全部用 `textContent`/`createElement`，**不用 innerHTML 拼字符串**；JSON 内联时把 `</` 转义为 `<\/`，防 `</script>` 注入。

---

## 5. 本地验证

### 5.1 仓库自测

```bash
cd /workspace/KaoyanBench
pip install -e .
python -m pytest tests/ -q          # → 240 passed
```

### 5.2 端到端（离线演示副本）

```bash
cd /workspace/KaoyanBench
python tools/gen_demo_benchmark.py --out /tmp/kybdemo --force
cd /tmp/kybdemo && pip install -e .
# ⚠ 后端 --suite 路径有 bug（见 §6），本副本需打 1 行兼容补丁后再跑：
kaoyanbench --root . run --suite demo --agent mock --tag v1 --seed 42
kaoyanbench --root . report --suite demo --agent mock --tag v1 --format json,markdown,html,csv
# 版本对比：再跑一个 tag，带 --baseline
kaoyanbench --root . run --suite demo --agent mock --tag v3 --seed 9
kaoyanbench --root . report --suite demo --agent mock --tag v3 --baseline v1 --format json,markdown,html,csv
```

产物：`/tmp/kybdemo/reports/demo__mock__v{1,2,3}/report.{json,md,html,csv}`。

### 5.3 校验脚本

```bash
cd /workspace/KaoyanBench
python tools/check_report_html.py /tmp/kybdemo/reports/demo__mock__v1/report.html
python tools/check_report_html.py --from-json /tmp/kybdemo/reports/demo__mock__v3/report.json
# 退出码：0=全 PASS，1=有 FAIL
```

### 5.4 实机浏览器验证（agent-browser）

```bash
agent-browser open "file:///tmp/kybdemo/reports/demo__mock__v1/report.html"
agent-browser eval "document.querySelectorAll('#task-table-body tr').length"   # 14
agent-browser fill "#task-search" "SEARCH"                                      # → 显示 4 / 14
agent-browser select "#task-filter-result" "fail"                               # → 显示 0 / 14（v1 全通过）
agent-browser click "th[data-sort='score']"                                     # aria-sort=descending
agent-browser click "button[data-task-id='RESEARCH-001']"                       # 抽屉 data-open=1，4 条 check
agent-browser --color-scheme dark open "file:///…/report.html"                  # body bg=rgb(15,17,22)
```

---

## 6. 已知后端问题（前端不修，仅记录）

### 6.1 `--suite` / `--task` 路径崩溃（P0）

`core/registry.py:380` 的 `load_suite_tasks`：

```python
return load_suite(suite.path or suite.id, tasks_root)   # ← 返回 Suite 对象，不是 list[Task]
```

调用方 `cli.py:_load_suite` 期望 `list[Task]`，随后 `len(tasks)` 抛
`TypeError: object of type 'Suite' has no len()`，导致 `kaoyanbench run --suite …` 必崩。
**1 行修复建议**：改为 `return list(getattr(load_suite(suite.path or suite.id, tasks_root), "resolved_tasks", []) or [])`。

> 本次验证**只在 `/tmp/kybdemo` 副本**里打了该兼容补丁，**未改动生产 `core/**`**。

### 6.2 任务集未交付

仓库 `benchmark/tasks/public/` 为空，故用 `tools/gen_demo_benchmark.py` 生成副本验收（全离线确定性，真实数值，非伪造）。

---

## 7. 未实现 / 后续项

| 项 | 说明 |
| --- | --- |
| 工具调用时间线 | 5.7 元素 11 提到「工具调用时间线（若加载 JSONL）」——单文件离线约束下**不加载外部 JSONL**，当前抽屉只展示 `checks` + `trace_ref` 文本路径。若需时间线，建议后端把 `tool_calls` 摘要内联进 `tasks[]`。 |
| 单测 | 未新增 `tests/` 用例（渲染层无既有测试文件）。当前状态 **240 passed（数量未变）**，渲染正确性由 `tools/check_report_html.py` + agent-browser 实测覆盖。若需纳入 CI，建议新增 `tests/test_reporters.py`。 |
| 暗色截图自动化 | agent-browser 的 `media` 子命令在当前版本不可用，改用 `--color-scheme dark` 实测通过。 |

---

## 8. 给后续人的注意事项

1. **12 类元素的 DOM 选择器有唯一来源**：改渲染时同步维护 `html_reporter.ELEMENT_SELECTORS`，`tools/check_report_html.py` 会自动跟。
2. **不要改 `ReportModel` 语义**：渲染层只读；数值 `None` 一律显示 `—`，**绝不用 0 冒充**（与后端约定一致）。
3. **SVG 动态文本必须过 `svg.escape()`**：否则答案里的 `<` 会撑坏 XML。
4. **保持零外部请求**：任何 `@import` / `url()` / `<link>` / 外链字体 / emoji 图标都会让 `check_report_html.py` 的 §2 失败。
5. **口径差异（§3.1）若不修**，新图表请沿用「占比 / total」守恒口径，勿用 `DIMENSION_MAX` 当分母。
6. **`register_reporter` 失败静默**：不影响 `_LAZY_REPORTERS` 兜底，但若报 `ReporterNotFoundError`，先查本模块是否 import 报错。
