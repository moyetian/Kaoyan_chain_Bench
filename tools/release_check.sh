#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# tools/release_check.sh —— KaoyanBench v1.0 上线前一键检查
#
# 顺序：pytest → validate → fixtures → smoke → report → 产物存在性 → 回归门禁
# 输出：清晰的 PASS/FAIL 汇总，任一失败 → 脚本退出码非 0。
#
# 用法：
#   bash tools/release_check.sh
#   bash tools/release_check.sh --concurrency 4      # 透传给 run（本脚本默认只跑 smoke）
#
# 设计：不依赖网络、不需要 API key（smoke 用 mock Runner）。
# ★ 不修改任何事实源；只在 results/ 与 reports/ 写派生物。
# ---------------------------------------------------------------------------
set -uo pipefail

# 切到脚本所在仓库根，保证相对路径正确
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || { echo "无法进入仓库根：$ROOT"; exit 2; }

# 解释器与 CLI 入口：默认面向已 `pip install -e .` 的 Unix 环境；
# 非 Unix（如 Windows Git Bash）或未安装时可用环境变量覆盖，例如：
#   PY=py KYB="py -m kaoyanbench" PYTHONPATH=src bash tools/release_check.sh
PY="${PY:-python3}"
KYB="${KYB:-kaoyanbench}"

TAG="release-$(date +%Y%m%d-%H%M%S)"
REPORT_DIR="reports/smoke__mock__${TAG}"

# 结果收集
declare -a NAMES=()
declare -a RESULTS=()
FAILED=0

pass() { NAMES+=("$1"); RESULTS+=("PASS"); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { NAMES+=("$1"); RESULTS+=("FAIL"); FAILED=1; printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }

hr() { printf '%s\n' "------------------------------------------------------------"; }

echo "============================================================"
echo " KaoyanBench 上线前检查（release_check.sh）"
echo " 仓库根：$ROOT"
echo " 时间：$(date -u +%Y-%m-%dT%H:%M:%SZ)  TAG=$TAG"
echo "============================================================"

# ---------------------------------------------------------------------------
hr; echo "[1/8] 安装与版本"
hr
if $PY -c "import kaoyanbench" 2>/dev/null; then
  MOD="$($PY -c 'import kaoyanbench;print(kaoyanbench.__file__)')"
  VER="$($KYB --version 2>/dev/null)"
  echo "  模块路径：$MOD"
  echo "  版本：$VER"
  # 路径归属用 Python 归一化比较，避免 Windows/Git Bash 的正反斜杠差异
  if $PY -c '
import pathlib, sys
mod = pathlib.Path(sys.argv[1]).resolve()
root = pathlib.Path(sys.argv[2]).resolve()
sys.exit(0 if (root == mod or root in mod.parents) else 1)
' "$MOD" "$ROOT"; then
    pass "安装指向本仓库（未错位）"
  else
    fail "安装错位：模块路径不在本仓库（$MOD）"
  fi
  [ "$VER" = "1.1.0" ] && pass "版本号 = 1.1.0" || fail "版本号非 1.1.0（实际：$VER）"
else
  fail "kaoyanbench 未安装（请先 pip install -e .；或用 PY/KYB 覆盖解释器与入口）"
fi

# ---------------------------------------------------------------------------
hr; echo "[2/8] 单元测试 pytest"
hr
# 单次运行：捕获输出后按退出码判定（原实现跑了两次且管道会吞掉退出码）
if PYTEST_OUT="$($PY -m pytest tests/ -q 2>&1)"; then
  echo "$PYTEST_OUT" | tail -3
  pass "pytest 全绿"
else
  echo "$PYTEST_OUT" | tail -20
  fail "pytest 存在失败用例"
fi

# ---------------------------------------------------------------------------
hr; echo "[3/8] 任务集校验 validate --suite core50"
hr
VALIDATE_OUT="$($KYB validate --suite core50 2>&1)"
VALIDATE_CODE=$?
echo "$VALIDATE_OUT" | grep -E "^suite=|error=" | head -1 || true
if [ "$VALIDATE_CODE" -eq 0 ]; then
  pass "validate core50 通过（error=0）"
else
  fail "validate core50 未通过（退出码=$VALIDATE_CODE）"
fi

# ---------------------------------------------------------------------------
hr; echo "[4/8] fixtures 校验"
hr
FIX_OUT="$($KYB fixtures check 2>&1)"
FIX_CODE=$?
echo "$FIX_OUT" | tail -1 || true
if [ "$FIX_CODE" -eq 0 ]; then
  pass "fixtures check 通过"
else
  fail "fixtures check 未通过（退出码=$FIX_CODE）"
fi

# ---------------------------------------------------------------------------
hr; echo "[5/8] 离线冒烟 run --suite smoke --agent mock"
hr
RUN_OUT="$($KYB run --suite smoke --agent mock --tag "$TAG" --seed 42 2>&1)"
RUN_CODE=$?
echo "$RUN_OUT" | grep -E "suite=smoke" || echo "(未捕获 suite 摘要行)"
if [ "$RUN_CODE" -eq 0 ] && echo "$RUN_OUT" | grep -q "success_rate=100.0%"; then
  pass "smoke 6/6 全通过"
elif [ "$RUN_CODE" -eq 0 ]; then
  fail "smoke 运行成功但未达 100%（检查输出）"
else
  fail "smoke 运行失败（退出码=$RUN_CODE）"
fi

# ---------------------------------------------------------------------------
hr; echo "[6/8] 报告生成与产物存在性"
hr
REP_OUT="$($KYB report --suite smoke --agent mock --tag "$TAG" -f json,markdown,html,csv 2>&1)"
REP_CODE=$?
echo "$REP_OUT" | head -1 || true
[ "$REP_CODE" -eq 0 ] && pass "report 生成成功" || fail "report 生成失败（退出码=$REP_CODE）"

for f in report.json report.md report.html report.csv; do
  if [ -f "$REPORT_DIR/$f" ]; then
    pass "产物存在：$REPORT_DIR/$f"
  else
    fail "产物缺失：$REPORT_DIR/$f"
  fi
done

# 报告 schema_version 自检（JSON 可解析）
if [ -f "$REPORT_DIR/report.json" ]; then
  SV="$($PY -c "import json;print(json.load(open('$REPORT_DIR/report.json')).get('schema_version'))" 2>/dev/null)"
  echo "  report.json schema_version = $SV"
  [ "$SV" = "1.0" ] && pass "report.json schema_version=1.0" || fail "report.json schema_version 异常（$SV）"
fi

# ---------------------------------------------------------------------------
hr; echo "[7/8] HTML 报告验收（零外部请求 + 12 类元素）"
hr
if [ -f "$REPORT_DIR/report.html" ]; then
  HTML_OUT="$($PY tools/check_report_html.py "$REPORT_DIR/report.html" 2>&1)"
  HTML_CODE=$?
  echo "$HTML_OUT" | tail -1 || true
  [ "$HTML_CODE" -eq 0 ] && pass "check_report_html 全通过" || fail "check_report_html 未通过（退出码=$HTML_CODE）"
else
  fail "缺少 $REPORT_DIR/report.html，无法做 HTML 验收"
fi

# ---------------------------------------------------------------------------
hr; echo "[8/8] 回归门禁 regression"
hr
# 以本次 release 结果为 current；baseline 必须使用入库基线
# （benchmark/baselines/）。缺失即 FAIL，严禁自比对（P0 教训：自比对恒绿）。
BASE_FILE="benchmark/baselines/smoke__mock__baseline.suite.json"
if [ -f "$BASE_FILE" ]; then
  cp "$BASE_FILE" "results/suites/smoke__mock__baseline.suite.json"
  BASE_TAG="baseline"
  echo "  使用入库基线：$BASE_FILE"
else
  fail "回归基线缺失（$BASE_FILE），拒绝自比对（见 benchmark/baselines/README.md）"
  BASE_TAG=""
fi
if [ -n "$BASE_TAG" ]; then
REG_OUT="$($KYB regression --baseline "$BASE_TAG" --current "$TAG" --suite smoke --agent mock 2>&1)"
REG_CODE=$?
echo "$REG_OUT" | tail -1 || true
if [ "$REG_CODE" -eq 0 ]; then
  pass "regression 门禁通过（退出码 0）"
elif [ "$REG_CODE" -eq 1 ]; then
  fail "regression 门禁失败（退出码 1，存在指标回退）"
else
  fail "regression 执行错误（退出码=$REG_CODE）"
fi
fi

# ---------------------------------------------------------------------------
hr
echo " 汇总"
hr
for i in "${!NAMES[@]}"; do
  if [ "${RESULTS[$i]}" = "PASS" ]; then
    printf '  \033[32m[PASS]\033[0m %s\n' "${NAMES[$i]}"
  else
    printf '  \033[31m[FAIL]\033[0m %s\n' "${NAMES[$i]}"
  fi
done
hr

TOTAL=${#NAMES[@]}
PASSED=0
for r in "${RESULTS[@]}"; do [ "$r" = "PASS" ] && PASSED=$((PASSED+1)); done
echo "  结果：$PASSED / $TOTAL 项通过"
if [ "$FAILED" -eq 0 ]; then
  printf '  \033[32m✅ RELEASE CHECK ALL PASS —— 可以发布\033[0m\n'
  exit 0
else
  printf '  \033[31m❌ RELEASE CHECK FAILED —— 禁止发布，请修复后重试\033[0m\n'
  exit 1
fi
