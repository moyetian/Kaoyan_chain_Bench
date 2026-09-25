# KaoyanBench

> **English** | [中文](README.md)

> **KaoyanBench is an open benchmark for evaluating AI agents on graduate-entrance-examination research, information retrieval, document analysis, evidence verification, planning, and long-horizon workflows.**

Companion project: [`kaoyan_chain`](https://github.com/moyetian/kaoyan_chain) (a study-planning chain for the graduate entrance exam). KaoyanBench is a **standalone project** — it does not depend on that codebase and connects to it only through a pluggable Runner adapter.

> Current version **v1.1.0** (see [CHANGELOG](CHANGELOG.en.md)): all 23 online tasks ship with
> **synthetic mirror snapshots** (fictional replay corpora for `--offline-replay` link verification;
> real web snapshots are yet to be captured and versioned via `snapshot fetch`,
> enforced by the `validate --require-snapshots` gate), Agent/task-directory isolation against
> cheating, and budget-caliber plus re-grade consistency fixes. Private hidden tasks are
> **not in the public repo** (only the mechanism and generator are public,
> see `benchmark/tasks/private/README.md`).

## Design principles

1. **Reproducible**: fixed tasks, fixed fixtures, fixed seeds, versioned web snapshots.
2. **Auto-gradeable**: all 50 v1.0 tasks are auto-gradeable (DoD #4 is measured as **auto-grading
   coverage**, semantic included; 24 of them (48%) are purely deterministic and need no grader model).
3. **Regressable**: `compare` + `regression` gates — a gate failure is a CI FAIL.
4. **Zero third-party dependencies**: the core pipeline (task loading / Runner / Logger / Grader /
   Reporter / CLI / regression) is pure standard library, runs on Python ≥3.10.
5. **No fabricated data**: for tasks involving real institutions' current-year figures, ground truth
   is always empty — grading checks only point hits + source levels + year tags; every institution
   inside fixtures is fictional.

## v1.0 scope

| Item | Content |
|---|---|
| Main suite | `core50`: 50 tasks / 8 categories / Public |
| Categories | search 12, university 9, policy 5, exam 5, PDF 5, planning 5, multi-step research 4, anti-hallucination 5 |
| Difficulty | Easy 10 / Medium 18 / Hard 15 / Expert 7 |
| Grading | 100-point scale, 7 dimensions + 11 key metrics |
| Reports | JSON (source of truth) + Markdown + single-file HTML |
| Out of scope | Leaderboard site, full SWE-bench ingestion, scanned-PDF OCR, Human Grader UI |

Full design (Chinese) in [`docs/01-需求拆解与方案定稿.md`](docs/01-需求拆解与方案定稿.md).

## Quick start

```bash
pip install -e .

# 1. Validate the task suites (required in CI)
kaoyanbench validate

# 2. Build fixtures (bundled data for offline tasks)
kaoyanbench fixtures build

# 3. Offline smoke test (no agent / API key needed)
kaoyanbench run --suite smoke --agent mock --tag smoke-1
kaoyanbench report --suite smoke --tag smoke-1

# 4. Plug in a real agent (example: the kaoyan_chain CLI)
kaoyanbench run --suite core50 --agent kaoyan_chain --tag v2.8.0 --runs 3

# 5. Cross-version regression
kaoyanbench regression --baseline v2.7.0 --current v2.8.0 --suite core50
```

## Layout

```
config/      configuration (default.yaml + agents/*.yaml + graders/*.yaml)
benchmark/   task suites (tasks/{public,private}, suites/, fixtures/, snapshots/)
src/kaoyanbench/  source (core/, reporters/, utils/)
tools/       fixture generators, snapshot fetching, Phase-3 external-benchmark import stubs
docs/        docs (01 ratified design / 02 task-suite spec / 03 new-agent onboarding /
             04 report fields / 05 roadmap; 01/02/07/08 currently Chinese-only,
             English available for 03/04/05/06)
results/     run traces (JSONL + SQLite, gitignored)
reports/     report artifacts (gitignored)
```

## Grader model and model under test must differ

KaoyanBench forces `model` (under test) and `grader_model` (grader) in `config/default.yaml` to come
from different sources, avoiding self-grading bias. If the grader model is unavailable
(no key / no network), results are explicitly labeled `grader_mode: "degraded"` with a red warning
banner in reports — **never silently pretending to have graded**.

## License

MIT, see [LICENSE](LICENSE).
