# 05 · Roadmap (KaoyanBench trajectory)

> **English** | [中文](05-roadmap.md)

> Task: D-03 | Scope: Phase 2 / Phase 3 / Phase 4
> This doc continues the tradeoff notes in `docs/01-需求拆解与方案定稿.md` §6.1 (Chinese-only).

---

## 0. Overview

| Phase | Theme | Scale | Status |
|---|---|---|---|
| **v1.0 (MVP)** | 8-category 50-task Public set + full grading engine + CI gates | 50 tasks / Public | ✅ Shipped |
| **Phase 2** | Grow to 100 Public + 100 Private, restore full baseline ratios | 100 + 100 | Planned |
| **Phase 3** | Plug in GeneralBench (SWE-bench / Terminal-Bench / Browser) | External benchmarks | Planned |
| **Phase 4** | Public Leaderboard | Service | Planned |

---

## 1. v1.0 baseline (shipped)

- Main suite `core50`: **50 tasks / 8 categories / Public**.
- Categories: search 12, university 9, policy 5, exam 5, pdf 5, planning 5, research 4, hallucination 5.
- Difficulty: Easy 10 / Medium 18 / Hard 15 / Expert 7.
- Auto-grading: 100% auto-gradeable (DoD #4 is measured as auto-grading coverage, semantic
  included; 24/50 = 48% are purely deterministic and need no grader model).
- Regression baseline: `smoke`'s mock baseline is versioned in `benchmark/baselines/` (CI reads it
  read-only; a missing baseline fails the gate — self-comparison is forbidden).
- Snapshot wording: the 23 v1.1 snapshots are **synthetic mirrors** (fictional replay corpora for
  `--offline-replay` link verification); real web snapshots are yet to be captured and versioned
  via `snapshot fetch` (see Phase 4 data freshness).
- CI: PR gates run only `smoke` (6 fully-offline tasks) + regression diff; full `core50` runs as
  nightly / release gates.

### 1.1 Difference vs the original §36 MVP ratios (kept on record)

The original MVP used **6 categories** at **20 / 10 / 5 / 5 / 5 / 5**
(Search / University / Policy / PDF / Planning / Hallucination).
That mix **fails** acceptance criterion #2 ("at least 8 task categories"), so v1.0 deviated from it
and added Exam and Research:

| Category | MVP ratio | v1.0 final | Change |
|---|---:|---:|---:|
| Search | 20 | 12 | −8 |
| University | 10 | 9 | −1 |
| Policy | 5 | 5 | 0 |
| PDF | 5 | 5 | 0 |
| Planning | 5 | 5 | 0 |
| Hallucination | 5 | 5 | 0 |
| **Exam** | — | **5** | +5 (new) |
| **Research** | — | **4** | +4 (new) |
| Total | 50 | 50 | — |

> **★ The original MVP `20 / 10 / 5 / 5 / 5 / 5` mix is recorded here as the baseline ratio for
> growing to 100 tasks in Phase 2.**

---

## 2. Phase 2 — scale-up (100 Public + 100 Private)

**Goal**: grow to 100 Public + 100 Private while keeping 8-category coverage — the long-term
anti-overfitting mechanism (design R6).

### 2.1 100-task Public mix (**anchored on the MVP 6-category 20/10/5/5/5/5 baseline**)

Scale the MVP `20 / 10 / 5 / 5 / 5 / 5` (= 50-task baseline) up 2× to 100 tasks, then fold in the two
v1.0-added categories (Exam / Research) to get the Phase 2 100-task Public mix:

| Category | MVP baseline (×2 → 100) | Exam/Research shift | **Phase 2 Public final** | Share |
|---|---:|---:|---:|---:|
| Search | 40 | −10 | **30** | 30% |
| University | 20 | −5 | **15** | 15% |
| Policy | 10 | 0 | **10** | 10% |
| Exam | — | +10 | **10** | 10% |
| PDF | 10 | 0 | **10** | 10% |
| Planning | 10 | 0 | **10** | 10% |
| Research | — | +8 | **8** | 8% |
| Hallucination | 10 | −3 | **7** | 7% |
| **Total** | **100** | 0 | **100** | 100% |

> Ratio principles: ① keep the MVP 6 categories' **relative baseline ratios** (Search stays the
> largest); ② Exam/Research take share from Search/University, same direction as the v1.0 tradeoff;
> ③ every category ≥7 tasks so per-group statistics stay meaningful.

### 2.2 100-task Private set

- **Construction**: same distribution as Public (same category mix, same difficulty mix), but
  **different task content, fixtures, and ground truth throughout**, and **never in the public repo**.
- **Purpose**: Public for open ranking and regression; Private for **overfitting spot-checks** and
  **pre-release final review**.
- **Release flow**: Private held by maintainers only; CI uses `private_demo` to verify `--split
  private` isolation (design R6, DoD#15).
- **Difficulty mix**: same as v1.0 — Easy/Medium/Hard/Expert = 20%/35%/30%/15% (Medium rounds up,
  Expert rounds down).

### 2.3 Engineering companions

- [ ] 100 tasks each in `benchmark/tasks/{public,private}`; `task.json` `version` discipline
  (ground-truth changes must bump the version).
- [ ] **Snapshot library**: the 23 online tasks carried over from v1.0 get **synthetic mirror**
  snapshots (T-07 replay-link part), covered by `snapshot verify` regression; real web capture and
  versioning see Phase 4 data freshness.
- [ ] **Anti-overfitting regression**: CI asserts an upper bound on anti-echo / prompt-copying cheat
  probe pass rates (probe idea exists since v1.0, see QA report R4).
- [ ] **Unified cost wording** (design R4): cross-agent cost comparison promoted to an optional gate.
- [ ] Document the Private-set release flow and anti-contamination rules.

---

## 3. Phase 3 — plug in GeneralBench

**Goal**: grow from "grad-exam vertical" to "general agent capability" evaluation, interoperable with
industry benchmarks.

| Item | Content | How |
|---|---|---|
| **SWE-bench** | Real GitHub issue fixes → code-level grading | Reuse `command` Runner + containerized workspace; add patch-apply and test-execution graders |
| **Terminal-Bench** | Terminal tasks (shell ops, file handling) | Reuse process sandbox + command-sequence verdicts |
| **Browser / WebArena-like** | Browser-interaction tasks | New browser Runner (Playwright-like); snapshot mechanism reused for offline replay |

**Shared engineering**:
- [ ] Abstract an "external benchmark adapter" layer (Phase-3 import stubs already reserved under
  `tools/` in v1.0).
- [ ] Unify the `task.json` schema so external benchmarks map onto KaoyanBench's `Task` model.
- [ ] Report layer shows multiple benchmarks side by side (no single overall ranking, continuing
  design §35).
- [ ] Sandbox upgraded to **container-level isolation** (v1.0 is process-level only; Phase 3 must
  upgrade since it executes untrusted code).

---

## 4. Phase 4 — public Leaderboard

**Goal**: serve continuously updated public rankings with comparable historical results.

- **Service**:
  - [ ] Result intake: receive/validate submitted `suite.json` (schema + grader_mode + baseline
    integrity checks).
  - [ ] Leaderboard frontend: columns per capability dimension (Accuracy / Reliability / Efficiency),
    **no single overall champion** (design §35).
  - [ ] Historical trends and regression visualization.
- **Governance**:
  - [ ] Submissions declare `agent` version, `model`, `grader_mode`; degraded results labeled
    separately, excluded from ranking.
  - [ ] Anti-cheat: Private-set spot checks + standing anti-echo probes.
  - [ ] Data freshness: versioned online-task snapshots, rankings stamped with snapshot dates.
- **Compliance**:
  - [ ] Keep the "never fabricate real institution data" principle (design R3) visible in public
    results.
  - [ ] Bilingual (Chinese/English) report templates (continuing the README wording).

---

## 5. Milestones (rough order, non-committal)

| Milestone | Content | Depends on |
|---|---|---|
| v1.1 | 23 synthetic-mirror snapshots, CI anti-echo assertion (`validate.yml` echo probe: core50 pass rate ≤8%) | v1.0 |
| v2.0 | Phase 2: 100 Public + 100 Private + full baselines | v1.1 |
| v2.x | Phase 3: SWE-bench / Terminal-Bench / Browser ingestion | v2.0 + container isolation |
| v3.0 | Phase 4: public Leaderboard | v2.x |
