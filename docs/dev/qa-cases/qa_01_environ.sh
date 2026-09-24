#!/usr/bin/env bash
# qa_01_environ.sh —— 建立隔离测试环境（克隆到 /tmp，避免污染仓库）
# 用法：bash docs/dev/qa-cases/qa_01_environ.sh
set -u
SRC=/workspace/KaoyanBench
LAB=${1:-/tmp/kb_lab}
rm -rf "$LAB"
cp -r "$SRC" "$LAB"
cd "$LAB"
rm -rf reports results .pytest_cache
echo "LAB=$LAB"
echo "--- 版本 ---"
kyb --version
echo "--- 任务总数 ---"
find benchmark/tasks -name task.json | wc -l
echo "--- 现有自测（仅供参考，不作为验收依据）---"
python -m pytest tests/ -q 2>&1 | tail -3
