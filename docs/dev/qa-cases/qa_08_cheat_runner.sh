#!/usr/bin/env bash
# qa_08_cheat_runner.sh —— "抄题面"作弊 Runner（QA 独立构造，用于探基准可判别性）
#
# 目的：模拟一个**完全不做任何推理/检索**、只会把题面泄漏内容原样复述的 Agent。
# 策略：读取 task.json，把下列字段原文拼接后作为 final_answer 输出：
#   - instruction（题面）
#   - expected.must_find[].any_of / value（判定关键字）
#   - expected.required_fields / required_sources 的字面量
#   - grader.checks 中的 value / patterns / any_of（判定词）
# 若这样的"抄题面"策略仍能拿高分，说明题面到判定词之间存在泄漏（P1-2 的更强判据）。
#
# 用法（由 QA 的 cheat agent yaml 以 command runner 调用）：
#   bash qa_08_cheat_runner.sh <task_dir>
# 该脚本读取 <task_dir>/task.json，向 stdout 输出一行 JSON：
#   {"answer": "<拼接后的题面文本>", "tool_calls": [], "sources": [], "citations": []}
set -u
TASK_DIR="${1:-}"
if [ -z "$TASK_DIR" ] || [ ! -f "$TASK_DIR/task.json" ]; then
  echo '{"answer": ""}'
  exit 0
fi

python3 - "$TASK_DIR/task.json" <<'PY'
import json, sys

path = sys.argv[1]
try:
    d = json.load(open(path, encoding="utf-8"))
except Exception:
    print(json.dumps({"answer": ""}, ensure_ascii=False))
    raise SystemExit(0)

parts = []

def add(x):
    if x is None:
        return
    if isinstance(x, str):
        parts.append(x)
    elif isinstance(x, (int, float)):
        parts.append(str(x))
    elif isinstance(x, list):
        for i in x:
            add(i)
    elif isinstance(x, dict):
        for k, v in x.items():
            # 判定关键字类字段重点抄；其余字段也一并抄（本题面全量泄漏策略）
            add(v)

# 1) 题面原文
add(d.get("instruction", ""))

# 2) expected 段全部可读文本（must_find 的 any_of/value、must_not_claim、required_* 等）
add(d.get("expected", {}))

# 3) grader.checks 的判定词（value / patterns / any_of / expected...）
grader = d.get("grader", {}) or {}
add(grader.get("checks", []))
add(grader.get("weights", {}))

# 4) answer_format 里可能写明的答案模板
add(d.get("answer_format", {}))

answer = "\n".join(str(p) for p in parts if str(p).strip())
print(json.dumps({"answer": answer, "tool_calls": [], "sources": [], "citations": []},
                 ensure_ascii=False))
PY
