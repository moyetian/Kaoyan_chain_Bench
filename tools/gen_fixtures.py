#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_fixtures.py —— KaoyanBench 确定性 fixture 生成器（方案 2.10 / 3.3 / 6.3，T-01/T-02）。

设计铁律（方案 6.5 第 5 条）
----------------------------
1. **确定性**：固定 seed、无墙钟时间戳、无 ``random``、无字典遍历顺序依赖。
   同一份源码连续运行两次，``benchmark/fixtures/`` 与各任务 ``references/`` 下
   每个文件的 sha256 必须**逐字节一致**。
2. **零第三方依赖可跑**：
   - PDF 走**内置最小 PDF 生成器**（纯标准库 ``zlib``，多页 + 可选表格，符合 PDF 1.4，
     可被通用阅读器打开；文本流用 FlateDecode 压缩，可用标准库 ``zlib`` 解压后按
     ``Tj`` / ``TJ`` 操作符提取文本）。
   - Excel 无 ``openpyxl`` 时**降级为 CSV** 并打印告警。
   - ``fpdf2`` / ``openpyxl`` 为**可选增强依赖**，缺失不影响主流程。
3. **全虚构**：所有院校、专业、人名、链接均为虚构（北原大学 / 东岭工业大学 /
   ``northplain.edu.example`` 等），每个文本文件头写明「虚构数据，仅用于基准测试」。
4. **快照库（SNAP）** 用本地固定假快照内容构造，**绝不联网**，保证可复现。

产物
----
- ``benchmark/fixtures/{seeds,templates}/...``：生成器输入源（供人 review）。
- ``benchmark/snapshots/<TASK_ID>/...``：**运行时快照库**（与 ``config/default.yaml`` 的
  ``evaluation.snapshot_dir`` 一致，供 ``snapshot verify`` 与 ``--offline-replay`` 消费）。
- ``benchmark/tasks/public/<category>/<TASK_ID>/references/...``：按任务分发的 fixture。
- 每个 ``references/`` 目录下写 ``manifest.json``（``{"files": {name: {sha256, size}}}``），
  供 ``kaoyanbench validate`` 校验 hash。

用法
----
    python tools/gen_fixtures.py            # 生成全部（幂等）
    python tools/gen_fixtures.py --check    # 只校验，不写入；不一致则退出码 1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import zlib
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
VERSION = "1.0"
SEED = 20250923  # 固定 seed 常量；本生成器实际不使用随机数，保留作语义标记与追溯

DISCLAIMER_ZH = "虚构数据，仅用于基准测试（KaoyanBench v1.0）；不对应任何真实院校、专业或人员。"

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = ROOT / "benchmark" / "fixtures"
SNAPSHOTS_ROOT = ROOT / "benchmark" / "snapshots"
TASKS_ROOT = ROOT / "benchmark" / "tasks" / "public"

# 虚构院校与专业字典（全部虚构，排名仅用于内部一致性）
FAKE_UNIVERSITIES = {
    "northplain": {
        "name": "北原大学",
        "en": "Northplain University",
        "domain": "northplain.edu.example",
        "code": "10B01",
        "city": "北原市",
    },
    "eastridge": {
        "name": "东岭工业大学",
        "en": "Eastridge Institute of Technology",
        "domain": "eastridge.edu.example",
        "code": "10B02",
        "city": "东岭市",
    },
    "southbay": {
        "name": "南湾理工大学",
        "en": "Southbay University of Technology",
        "domain": "southbay.edu.example",
        "code": "10B03",
        "city": "南湾市",
    },
}

# 虚构专业（代码为 6 位，前缀 0/1 仅为虚构）
FAKE_MAJORS = [
    {"code": "081200", "name": "计算机科学与技术", "type": "学硕"},
    {"code": "085404", "name": "计算机技术", "type": "专硕"},
    {"code": "081000", "name": "信息与通信工程", "type": "学硕"},
    {"code": "085400", "name": "电子信息", "type": "专硕"},
    {"code": "080200", "name": "机械工程", "type": "学硕"},
    {"code": "085500", "name": "机械", "type": "专硕"},
]

EXAM_SUBJECTS = ["数学一", "数学二", "英语一", "英语二", "政治", "数据结构", "计算机组成原理", "操作系统"]

FIXED_FETCHED_AT = "2025-01-04T10:00:00+08:00"  # 固定抓取时刻，保证快照可复现

_CREATED: list[Path] = []  # 本次运行的写入记录（相对 ROOT）


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _write_bytes(path: Path, data: bytes, *, check: bool) -> None:
    """确定性写入：内容相同则不动（保持 mtime 也无所谓，hash 才是判定口径）。"""
    if check:
        if not path.is_file() or path.read_bytes() != data:
            raise SystemExit(f"[check] 不一致或缺失：{_rel(path)}")
        _CREATED.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        _CREATED.append(path)
        return
    path.write_bytes(data)
    _CREATED.append(path)


def _write_text(path: Path, text: str, *, check: bool) -> None:
    _write_bytes(path, text.encode("utf-8"), check=check)


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# T-02：内置最小 PDF 生成器（纯标准库，多页 + 可选表格）
# --------------------------------------------------------------------------- #
# 说明：PDF 文本对象里中文无法直接用标准字体编码，故本生成器**正文统一用
# ASCII 拼音/编号**（虚构院校以拉丁转写出现），保证任何阅读器都能打开并提取。
# 这是「只保证我们自己生成的 PDF 能读」的取舍（方案 R7）。

def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _pdf_text_ops(lines: list[str], x: float = 72.0, top: float = 720.0, leading: float = 18.0) -> str:
    """把若干行文本转成 PDF 内容流操作符（BT ... ET）。"""
    ops = ["BT", "/F1 11 Tf", f"{leading:g} TL", f"1 0 0 1 {x:g} {top:g} Tm"]
    for line in lines:
        ops.append(f"({_pdf_escape(line)}) Tj")
        ops.append("T*")
    ops.append("ET")
    return "\n".join(ops)


def _pdf_table_ops(header: list[str], rows: list[list[str]], *, x: float = 72.0, top: float = 690.0
                   ) -> str:
    """用等宽 F1 字体画一张简单表（列宽按字符数近似）。"""
    widths = [max(len(str(r[i])) for r in [header] + rows) + 3 for i in range(len(header))]

    def fmt(cells: list[str]) -> str:
        return "".join(str(c).ljust(w) for c, w in zip(cells, widths))

    lines = [fmt(header), "-" * sum(widths)]
    for r in rows:
        lines.append(fmt([str(c) for c in r]))
    return _pdf_text_ops(lines, x=x, top=top, leading=14.0)


def _pdf_pages_content(pages: list[str]) -> bytes:
    """把每页内容流（字符串）组装为完整 PDF 文件字节。纯标准库、确定性。"""
    objects: list[bytes] = []  # 1-based 对象体（不含 "N 0 obj"）

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    # 预留：Catalog(1) / Pages(2) / Font(3)，正文从 4 开始
    objects.append(b"")  # 1 catalog（占位）
    objects.append(b"")  # 2 pages（占位）
    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

    page_ids: list[int] = []
    for content in pages:
        raw = content.encode("latin-1", "replace")
        comp = zlib.compress(raw, 9)
        stream_id = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp) + comp + b"\nendstream")
        page_id = add(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {stream_id} 0 R >>"
            ).encode("latin-1")
        )
        page_ids.append(page_id)

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = ("<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_ids), kids)).encode("latin-1")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode("latin-1"))
        out.write(body)
        out.write(b"\nendobj\n")
    xref_pos = out.tell()
    n = len(objects) + 1
    out.write(f"xref\n0 {n}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.write(
        (
            f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
        ).encode("latin-1")
    )
    return out.getvalue()


def build_pdf(title: str, blocks: list, *, use_fpdf2: bool | None = None) -> bytes:
    """生成 PDF 字节。

    ``blocks`` 为有序块列表：``{"kind":"text","lines":[...]}`` 或
    ``{"kind":"table","header":[...],"rows":[[...]]}`` 或 ``{"kind":"pagebreak"}``。

    **默认走内置最小生成器**（纯标准库，字节与运行环境无关，跨环境可复现）；
    仅当 ``use_fpdf2=True``（或全局 ``--enhanced`` 且检测到 fpdf2）时才走增强路径。
    两条路径都**只输出 ASCII 正文**。
    """
    if use_fpdf2:
        data = _build_pdf_fpdf2(title, blocks)
        if data is not None:
            return data
    return _build_pdf_builtin(title, blocks)


# 全局开关：``--enhanced`` 时置 True，允许可选依赖增强路径
_USE_ENHANCED = False


def _has_fpdf2() -> bool:
    try:
        import fpdf  # noqa: F401

        return True
    except Exception:
        return False


def _build_pdf_fpdf2(title: str, blocks: list) -> bytes | None:
    try:
        from fpdf import FPDF  # type: ignore
    except Exception:
        return None
    try:
        pdf = FPDF()
        pdf.set_auto_page_break(True, margin=18)
        pdf.add_page()
        pdf.set_font("Courier", size=11)
        pdf.cell(0, 8, _ascii(title), ln=True)
        for block in blocks:
            if block.get("kind") == "pagebreak":
                pdf.add_page()
            elif block.get("kind") == "table":
                header = [_ascii(str(c)) for c in block["header"]]
                rows = [[_ascii(str(c)) for c in r] for r in block["rows"]]
                widths = [max(len(r[i]) for r in [header] + rows) + 3 for i in range(len(header))]
                for row in [header] + rows:
                    for cell, w in zip(row, widths):
                        pdf.cell((w * 1.6), 7, cell, border=0)
                    pdf.ln(7)
            else:
                for line in block.get("lines", []):
                    pdf.cell(0, 6, _ascii(line), ln=True)
        raw = pdf.output(dest="S")
        return bytes(raw)
    except Exception:
        return None


def _ascii(text: str) -> str:
    """把中文转写为 ASCII 备注，保证任何字体都能渲染。"""
    return text.encode("ascii", "replace").decode("ascii")


def _build_pdf_builtin(title: str, blocks: list) -> bytes:
    pages: list[str] = []
    current: list[str] = [_ascii(title), ""]
    for block in blocks:
        if block.get("kind") == "pagebreak":
            pages.append("\n".join(current))
            current = []
        elif block.get("kind") == "table":
            current.append(_pdf_table_ops(block["header"], block["rows"]))
        else:
            current.append(_pdf_text_ops(block.get("lines", [])))
    if current:
        pages.append("\n".join(current))
    if not pages:
        pages = [""]
    return _pdf_pages_content(pages)


_TJ_RE = re.compile(r"\(((?:[^()\\]|\\.)*)\)\s*Tj|\[([^\]]*)\]\s*TJ", re.DOTALL)
_TJ_ARRAY_STR_RE = re.compile(r"\(((?:[^()\\]|\\.)*)\)")


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """自研最简 PDF 文本提取器（方案 R7）：解 FlateDecode 流 + 解析 ``Tj``/``TJ``。

    只保证能读回本生成器产出的 PDF，不做通用解析。
    """
    text_parts: list[str] = []
    for stream in _iter_pdf_streams(pdf_bytes):
        try:
            decoded = zlib.decompress(stream)
        except zlib.error:
            decoded = stream
        body = decoded.decode("latin-1", "replace")
        for match in _TJ_RE.finditer(body):
            if match.group(1) is not None:
                text_parts.append(_unescape_pdf(match.group(1)))
            elif match.group(2) is not None:
                text_parts.append(
                    "".join(_unescape_pdf(m.group(1)) for m in _TJ_ARRAY_STR_RE.finditer(match.group(2)))
                )
    return "\n".join(text_parts)


def _iter_pdf_streams(pdf_bytes: bytes):
    pos = 0
    while True:
        s = pdf_bytes.find(b"stream", pos)
        if s == -1:
            return
        start = s + len(b"stream")
        if pdf_bytes[start:start + 2] == b"\r\n":
            start += 2
        elif pdf_bytes[start:start + 1] in (b"\n", b"\r"):
            start += 1
        end = pdf_bytes.find(b"endstream", start)
        if end == -1:
            return
        yield pdf_bytes[start:end]
        pos = end + len(b"endstream")


def _unescape_pdf(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            mapping = {"n": "\n", "r": "\r", "t": "\t", "(": "(", ")": ")", "\\": "\\"}
            out.append(mapping.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def extract_pdf_text_inner(tj_group: str) -> str:
    """提取 ``[...]TJ`` 数组中的字符串片段（未压缩场景的兼容辅助）。"""
    return "".join(_unescape_pdf(m.group(1)) for m in _TJ_ARRAY_STR_RE.finditer(tj_group))


# --------------------------------------------------------------------------- #
# CSV / Excel 写入
# --------------------------------------------------------------------------- #
def csv_bytes(header: list[str], rows: list[list], *, bomb: bool = True) -> bytes:
    """确定性 CSV：固定 ``\\n`` 换行、LF、UTF-8。``bomb`` 控制 BOM（默认带）。"""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for r in rows:
        writer.writerow(r)
    data = buf.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bomb else data


def write_xlsx_or_csv(base: Path, *, sheet_rows: list[list], header: list[str], check: bool) -> tuple[str, str]:
    """写 Excel：有 openpyxl → ``.xlsx``；否则降级 ``.csv`` 并告警。

    返回 ``(文件名, 实际格式)``，格式为 ``xlsx`` 或 ``csv``，供任务写明 ``fixture_format``。
    """
    if not _USE_ENHANCED:
        fname = base.name + ".csv"
        sys.stderr.write(
            f"[warn] 未启用 --enhanced（或未安装 openpyxl），Excel fixture 降级为 CSV：{_rel(base.parent / fname)}\n"
        )
        _write_bytes(base.parent / fname, csv_bytes(header, sheet_rows), check=check)
        return fname, "csv"

    try:
        import openpyxl  # type: ignore
    except Exception:
        fname = base.name + ".csv"
        sys.stderr.write(
            f"[warn] 未安装 openpyxl，Excel fixture 降级为 CSV：{_rel(base.parent / fname)}\n"
        )
        _write_bytes(base.parent / fname, csv_bytes(header, sheet_rows), check=check)
        return fname, "csv"

    from openpyxl import Workbook  # type: ignore

    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append([str(h) for h in header])
    for row in sheet_rows:
        ws.append(list(row))
    out = io.BytesIO()
    # openpyxl 默认写入创建/修改时间等元数据 → 引入非确定性；此处固定为常量以保证可复现。
    from datetime import datetime
    fixed_dt = datetime(2025, 1, 4, 10, 0, 0)
    wb.properties.created = fixed_dt
    wb.properties.modified = fixed_dt
    wb.properties.creator = "KaoyanBench-gen_fixtures"
    wb.properties.lastModifiedBy = "KaoyanBench-gen_fixtures"
    wb.properties.title = "FICTIONAL admission data for benchmark testing only"
    wb.save(out)
    fname = base.name + ".xlsx"
    _write_bytes(base.parent / fname, out.getvalue(), check=check)
    return fname, "xlsx"


# --------------------------------------------------------------------------- #
# 内容构造（全部虚构）
# --------------------------------------------------------------------------- #
def _header_lines(subject: str) -> list[str]:
    return [f"# {subject}", f"# {DISCLAIMER_ZH}", f"# version={VERSION} seed={SEED}", ""]


def build_exam_csv() -> tuple[list[str], list[list]]:
    """真题考点 CSV：固定 60 行（年份 2015~2024 × 章节），无随机。"""
    chapters = [
        ("数据结构", 12), ("计算机组成原理", 10), ("操作系统", 9), ("计算机网络", 8),
        ("数据库", 7), ("软件工程", 6), ("编译原理", 5), ("离散数学", 3),
    ]
    years = list(range(2015, 2025))
    header = ["year", "chapter", "question_no", "score", "type"]
    rows: list[list] = []
    # 确定性遍历：按 (year, chapter) 排序，不依赖字典序
    seq = []
    for y in sorted(years):
        for idx, (chap, n) in enumerate(sorted(chapters)):
            seq.append((y, chap, n, idx))
    for i, (y, chap, n, idx) in enumerate(seq):
        # 只用整数算术，无 random
        qno = (i * 7) % 30 + 1
        if chap in ("数据结构", "计算机组成原理", "操作系统"):
            rows.append([y, chap, qno, 2 if i % 3 else 4, "选择题"])
        else:
            rows.append([y, chap, qno, 3 if i % 2 else 5, "简答题"])
    return header, rows


def build_admission_csv() -> tuple[list[str], list[list]]:
    """录取数据 CSV：北原大学 081200 专业 2022–2025 录取最低分/平均分（虚构）。"""
    header = ["university", "major_code", "year", "min_score", "avg_score", "plan_count", "admitted"]
    base_min = {2022: 348, 2023: 351, 2024: 349, 2025: 355}
    rows = []
    for y in sorted(base_min):
        rows.append([
            "北原大学", "081200", y,
            base_min[y],
            base_min[y] + 12 + (y - 2022) * 2,
            30 + (y - 2022),
            28 + (y - 2022),
        ])
    return header, rows


def build_lead_typo_csv() -> tuple[list[str], list[list]]:
    """HAL-003 用：自相矛盾数据表（同一年同一专业两条不同最低分）。"""
    header = ["university", "major_code", "year", "min_score", "source_page"]
    rows = [
        ["南湾理工大学", "085404", 2025, 362, "page-a"],
        ["南湾理工大学", "085404", 2025, 355, "page-b"],  # 故意冲突
    ]
    return header, rows


def build_weak_point_csv() -> tuple[list[str], list[list]]:
    """PLAN-004 用：薄弱点诊断表。"""
    header = ["knowledge_point", "mastery", "question_count", "priority"]
    rows = [
        ["数据结构-红黑树", 0.25, 12, "high"],
        ["操作系统-信号量", 0.35, 9, "high"],
        ["计算机网络-TCP拥塞控制", 0.40, 8, "high"],
        ["计算机组成原理-浮点运算", 0.55, 10, "medium"],
        ["数学一-概率论", 0.60, 15, "medium"],
        ["英语一-长难句", 0.70, 20, "low"],
    ]
    return header, rows


def build_policy_texts() -> dict[str, str]:
    """三份本地政策文本（2024/2025/2026，虚构）。用于 POL-003/POL-004。"""
    common = "\n".join(_header_lines("东岭工业大学 推免工作实施办法（虚构）"))
    texts: dict[str, str] = {}
    texts["policy_2024.txt"] = common + "\n".join([
        "第一条 本办法适用于东岭工业大学 2024 年推荐优秀应届本科毕业生免试攻读研究生工作。",
        "第二条 推免名额按教育部下达计划执行，2024 年全校推免比例为 14.5%。",
        "第三条 申请者须为应届本科毕业生，前三年学业成绩排名专业前 30%。",
        "第四条 需提交两名副教授及以上职称专家的推荐信。",
        "第五条 综合成绩 = 学业成绩 70% + 科研创新 20% + 综合素质 10%。",
        "第六条 获得国家级竞赛一等奖者，综合成绩加 3 分。",
        "第七条 公示期为 5 个工作日。",
        "",
    ])
    texts["policy_2025.txt"] = common + "\n".join([
        "第一条 本办法适用于东岭工业大学 2025 年推荐优秀应届本科毕业生免试攻读研究生工作。",
        "第二条 推免名额按教育部下达计划执行，2025 年全校推免比例为 15.2%。",
        "第三条 申请者须为应届本科毕业生，前三年学业成绩排名专业前 25%。",
        "第四条 需提交两名副教授及以上职称专家的推荐信。",
        "第五条 综合成绩 = 学业成绩 65% + 科研创新 25% + 综合素质 10%。",
        "第六条 获得国家级竞赛一等奖者，综合成绩加 5 分。",
        "第七条 公示期为 7 个工作日。",
        "",
    ])
    texts["policy_2026.txt"] = common + "\n".join([
        "第一条 本办法适用于东岭工业大学 2026 年推荐优秀应届本科毕业生免试攻读研究生工作。",
        "第二条 推免名额按教育部下达计划执行，2026 年全校推免比例为 16.0%。",
        "第三条 申请者须为应届本科毕业生，前三年学业成绩排名专业前 25%。",
        "第五条 综合成绩 = 学业成绩 60% + 科研创新 30% + 综合素质 10%。",
        "第七条 公示期为 7 个工作日。",
        "附则 第六条（竞赛加分）自本办法施行之日起不再适用。",
        "",
    ])
    return texts


def build_major_catalog_html(uni_key: str = "northplain", *, conflict: bool = False) -> str:
    """虚构院校镜像站：研究生院招生专业目录页（HTML）。"""
    uni = FAKE_UNIVERSITIES[uni_key]
    disclaim = f"<!-- {DISCLAIMER_ZH} -->"
    rows = []
    for i, mj in enumerate(FAKE_MAJORS):
        plan = 20 + i * 3
        if conflict and mj["code"] == "081200":
            plan = 33  # 与 page_b 冲突
        rows.append(
            f"      <tr><td>{mj['code']}</td><td>{mj['name']}</td>"
            f"<td>{mj['type']}</td><td>{plan}</td><td>{EXAM_SUBJECTS[i % len(EXAM_SUBJECTS)]}</td></tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{uni['name']} 研究生院 · 2026 招生专业目录</title>
{disclaim}
</head>
<body>
<h1>{uni['name']} 研究生院</h1>
<p class="disclaimer">{DISCLAIMER_ZH}</p>
<p>官方域名：{uni['domain']} （虚构示例域名，不可访问）</p>
<h2>2026 年硕士研究生招生专业目录（节选）</h2>
<table id="major-catalog">
  <thead><tr><th>专业代码</th><th>专业名称</th><th>学位类型</th><th>拟招生人数</th><th>初试科目</th></tr></thead>
  <tbody>
{chr(10).join(rows)}
  </tbody>
</table>
<p>说明：表中人数为拟招生人数（含推免）。详情以研究生院正式公告为准。</p>
</body>
</html>
"""


def build_college_page_html(uni_key: str = "northplain") -> str:
    """学院二级页面（含复试/招生人数，可能与研究生院页冲突）。用于 UNI-006/009。"""
    uni = FAKE_UNIVERSITIES[uni_key]
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{uni['name']} 计算机学院 · 2026 招生说明</title>
<!-- {DISCLAIMER_ZH} -->
</head>
<body>
<h1>{uni['name']} 计算机学院</h1>
<p class="disclaimer">{DISCLAIMER_ZH}</p>
<h2>2026 年硕士招生说明（学院口径）</h2>
<p>专业代码 081200（计算机科学与技术）2026 年学院拟招生人数为 <strong>33</strong> 人。</p>
<p>复试方式：采用现场面试，满分 100 分，低于 60 分不予录取。</p>
<p>参考书目：《数据结构（C 语言版）》《计算机操作系统》。（虚构书目）</p>
</body>
</html>
"""


def build_hallucination_html(uni_key: str = "northplain") -> str:
    """HAL-005 用：若干带/不带 E 级标注的结论页。"""
    uni = FAKE_UNIVERSITIES[uni_key]
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{uni['name']} 招生信息汇总页</title>
<!-- {DISCLAIMER_ZH} -->
</head>
<body>
<h1>{uni['name']} 招生信息汇总</h1>
<p class="disclaimer">{DISCLAIMER_ZH}</p>
<ul>
  <li data-evidence="E5">2025 年计算机技术专业复试线为 355 分（来源：研究生院公告）。</li>
  <li data-evidence="E4">学制为 3 年，学费 8000 元/年（来源：研究生院官网）。</li>
  <li data-evidence="E0">该校 2026 年将扩招至 500 人（无任何来源）。</li>
</ul>
</body>
</html>
"""


def build_pdf_docs() -> dict[str, bytes]:
    """生成多份虚构招生简章 PDF（内置最小生成器 / 可选 fpdf2）。"""
    docs: dict[str, bytes] = {}

    # brief_2026.pdf：5 页，含学制与学费（PDF-001）
    blocks = [{"kind": "text", "lines": _header_lines("Northplain University 2026 Admission Guide (FICTIONAL)")
              + [DISCLAIMER_ZH, ""]}]
    sections = [
        ["Section 1. Basic Information",
         "School: Northplain University (fictional).",
         "City: Northplain City.",
         "Duration (xuezhi): 3 years for academic master.",
         "Tuition (xuefei): 8000 CNY per year.",
         "Contact: admissions@northplain.edu.example (fictional).",
         ""],
        ["Section 2. Programs",
         "081200 Computer Science and Technology (academic).",
         "085404 Computer Technology (professional).",
         ""],
        ["Section 3. Exam Subjects",
         "Mathematics I, English I, Politics, Data Structures.",
         ""],
        ["Section 4. Retest (fushi)",
         "Retest method: on-site interview, full score 100, pass >= 60.",
         "Reference books: Data Structures; Operating Systems. (fictional)",
         ""],
        ["Section 5. Notes",
         "All data fictional, for benchmark testing only.",
         ""],
    ]
    for i, sec in enumerate(sections):
        if i:
            blocks.append({"kind": "pagebreak"})
        blocks.append({"kind": "text", "lines": sec})
    docs["brief_2026_5p.pdf"] = build_pdf("Northplain University 2026 Admission Guide", blocks)

    # catalog_table.pdf：含专业目录表格（PDF-002）
    header = ["Code", "Name", "Type", "Plan", "Subject"]
    rows = [[m["code"], _ascii(m["name"]), _ascii(m["type"]), 20 + i * 3, EXAM_SUBJECTS[i % len(EXAM_SUBJECTS)]]
            for i, m in enumerate(FAKE_MAJORS)]
    table_blocks = [
        {"kind": "text", "lines": ["Northplain University 2026 Major Catalog (FICTIONAL)", DISCLAIMER_ZH, ""]},
        {"kind": "table", "header": header, "rows": rows},
    ]
    docs["catalog_table.pdf"] = build_pdf("Northplain University 2026 Major Catalog", table_blocks)

    # brief_long_20p.pdf：20 页（PDF-003 定位复试方式章节）
    long_blocks = []
    for page in range(1, 21):
        if page > 1:
            long_blocks.append({"kind": "pagebreak"})
        if page == 7:
            long_blocks.append({"kind": "text", "lines": [
                "Section: Retest Method (fushi fangshi)",
                "Retest method: on-site interview plus written test.",
                "Full score 100; pass threshold 60.",
                "Weight: retest 40%, preliminary 60%.",
                "All data fictional.",
                "",
            ]})
        else:
            long_blocks.append({"kind": "text", "lines": [
                f"Chapter {page}",
                f"This is page {page} of the fictional long admission document.",
                "Content placeholder for benchmark testing.",
                "",
            ]})
    docs["brief_long_20p.pdf"] = build_pdf("Northplain University Long Guide", long_blocks)

    # 两版简章（PDF-004 对比）：2025 vs 2026，差异固定
    docs["brief_2025.pdf"] = build_pdf("Northplain University 2025 Guide", [
        {"kind": "text", "lines": ["Northplain University 2025 Admission Guide (FICTIONAL)", DISCLAIMER_ZH, ""]},
        {"kind": "text", "lines": ["Duration: 3 years.", "Tuition: 8000 CNY/year.",
                                   "Plan (081200): 30.", "Retest weight: 30%.", ""]},
    ])
    docs["brief_2026.pdf"] = build_pdf("Northplain University 2026 Guide", [
        {"kind": "text", "lines": ["Northplain University 2026 Admission Guide (FICTIONAL)", DISCLAIMER_ZH, ""]},
        {"kind": "text", "lines": ["Duration: 3 years.", "Tuition: 9000 CNY/year.",
                                   "Plan (081200): 33.", "Retest weight: 40%.", ""]},
    ])

    return docs


def build_snapshot_library() -> dict[str, str]:
    """本地固定假快照库（SNAP），绝不联网。返回 {相对路径: 内容}。

    结构**必须**与 ``core/snapshot.py`` 的 ``SnapshotStore`` 逐字段一致，否则
    ``snapshot verify`` 与 ``--offline-replay`` 读不出内容：

    - ``<task_id>/index.json``：``{task_id, captured_at, pages: [SnapshotPage, ...]}``，
      其中 ``snapshot_path`` 是**相对任务目录**的路径（``pages/<sha256>.html``）。
    - ``<task_id>/meta.json``：``index.json`` 的副本（兼容别名）。
    - ``<task_id>/pages/<sha256>.meta.json``：页面级旁注，仅供人 review，消费方不读。
    """
    files: dict[str, str] = {}
    pages_by_task: dict[str, list[dict]] = {}

    def page(task_id: str, url: str, title: str, year: int, body: str) -> None:
        html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<!-- {DISCLAIMER_ZH} --></head>
<body><h1>{title}</h1><p>{DISCLAIMER_ZH}</p>{body}</body></html>
"""
        raw = html.encode("utf-8")
        sha = sha256_bytes(raw)
        relative = f"pages/{sha}.html"  # 相对任务目录（SnapshotStore.save_page 的约定）
        files[f"{task_id}/{relative}"] = html
        files[f"{task_id}/pages/{sha}.meta.json"] = _j(
            {
                "url": url,
                "title": title,
                "domain": "northplain.edu.example",
                "fetched_at": FIXED_FETCHED_AT,
                "content_sha256": sha,
                "snapshot_path": relative,
                "year": year,
                "made_up": True,
            }
        )
        pages_by_task.setdefault(task_id, []).append(
            {
                "url": url,
                "title": title,
                "fetched_at": FIXED_FETCHED_AT,
                "content_sha256": sha,
                "http_status": 200,
                "snapshot_path": relative,
                "bytes": len(raw),
            }
        )

    # SEARCH-011：北原大学 2025 推免接收办法
    page(
        "SEARCH-011",
        "https://northplain.edu.example/graduate/tuimian-2025",
        "北原大学 2025 年推荐免试研究生接收办法",
        2025,
        "<p>2025 年推免接收比例 15.2%，申请截止 2024-09-25。（虚构）</p>",
    )
    # SEARCH-012：三条冲突分数线快照
    page("SEARCH-012", "https://northplain.edu.example/a", "北原大学 085404 分数线（公告 A）", 2025,
         "<p>2025 年 085404 复试线：348 分。（虚构，公告 A）</p>")
    page("SEARCH-012", "https://northplain.edu.example/b", "北原大学 085404 分数线（公告 B，较新）", 2025,
         "<p>2025 年 085404 复试线修正为：355 分。（虚构，公告 B，最新有效）</p>")
    page("SEARCH-012", "https://northplain.edu.example/c", "北原大学 085404 分数线（转载，过期）", 2024,
         "<p>2024 年 085404 复试线：340 分。（虚构，第三方转载，已过期）</p>")

    # RES-002：院校资料快照
    page("RES-002", "https://eastridge.edu.example/graduate/2026", "东岭工业大学 2026 招生总览", 2026,
         "<p>2026 年拟招生 1200 人。（虚构）</p>")

    for task_id, pages in pages_by_task.items():
        # 与 SnapshotStore._upsert_page 的排序一致（url, content_sha256）
        pages.sort(key=lambda p: (p["url"], p["content_sha256"]))
        payload = _j(
            {
                "task_id": task_id,
                "captured_at": FIXED_FETCHED_AT,
                "pages": pages,
            }
        )
        files[f"{task_id}/index.json"] = payload
        files[f"{task_id}/meta.json"] = payload  # 兼容别名（= index.json 内容）
    return files


# --------------------------------------------------------------------------- #
# 任务 → fixture 分发映射（对齐方案 6.3 清单）
# --------------------------------------------------------------------------- #
def _task_dir(category: str, task_id: str) -> Path:
    return TASKS_ROOT / category / task_id


def _write_references(category: str, task_id: str, files: dict[str, bytes | str], *, check: bool) -> None:
    """把 fixture 写入任务 references/ 并生成 manifest.json。"""
    refs = _task_dir(category, task_id) / "references"
    manifest_files: dict[str, dict] = {}
    for name in sorted(files):
        data = files[name]
        payload = data.encode("utf-8") if isinstance(data, str) else data
        target = refs / name
        _write_bytes(target, payload, check=check)
        manifest_files[name] = {"sha256": sha256_bytes(payload), "size": len(payload)}
    manifest = {"task_id": task_id, "version": VERSION, "files": manifest_files}
    _write_bytes(refs / "manifest.json", _j(manifest).encode("utf-8"), check=check)


def distribute(check: bool) -> None:
    exam_header, exam_rows = build_exam_csv()
    adm_header, adm_rows = build_admission_csv()
    lead_header, lead_rows = build_lead_typo_csv()
    weak_header, weak_rows = build_weak_point_csv()
    policies = build_policy_texts()
    pdfs = build_pdf_docs()

    # ---- seed 源文件（供人 review，与任务分发内容一致）----
    seeds = FIXTURES_ROOT / "seeds"
    _write_bytes(seeds / "exam_questions.csv", csv_bytes(exam_header, exam_rows), check=check)
    _write_bytes(seeds / "admission_scores.csv", csv_bytes(adm_header, adm_rows), check=check)
    _write_bytes(seeds / "lead_conflict.csv", csv_bytes(lead_header, lead_rows), check=check)
    _write_bytes(seeds / "weak_points.csv", csv_bytes(weak_header, weak_rows), check=check)
    for name, text in sorted(policies.items()):
        _write_text(seeds / name, text, check=check)
    tmpl = FIXTURES_ROOT / "templates"
    _write_text(tmpl / "northplain_catalog.html", build_major_catalog_html(), check=check)
    _write_text(tmpl / "northplain_catalog_conflict.html", build_major_catalog_html(conflict=True), check=check)
    _write_text(tmpl / "northplain_college.html", build_college_page_html(), check=check)
    _write_text(tmpl / "northplain_hallucination.html", build_hallucination_html(), check=check)

    # ---- 快照库（SNAP）----
    # ★ 必须写到运行时快照库 benchmark/snapshots/（config: evaluation.snapshot_dir），
    #   否则 snapshot verify / --offline-replay 读不到。
    snap_root = SNAPSHOTS_ROOT
    for rel, content in sorted(build_snapshot_library().items()):
        _write_text(snap_root / rel, content, check=check)

    # ---- 按任务分发 ----
    # EXAM-001：真题 CSV
    _write_references("exam", "EXAM-001", {"questions.csv": csv_bytes(exam_header, exam_rows)}, check=check)
    # EXAM-002：同 CSV（按年份/章节统计）
    _write_references("exam", "EXAM-002", {"questions.csv": csv_bytes(exam_header, exam_rows)}, check=check)
    # EXAM-003：录取数据 Excel（降级 CSV）
    adm_name, adm_fmt = write_xlsx_or_csv(
        _task_dir("exam", "EXAM-003") / "references" / "admission_scores",
        header=adm_header, sheet_rows=adm_rows, check=False,
    )
    # 重新确定性写 manifest（write_xlsx_or_csv 已落文件）
    _write_references("exam", "EXAM-003", {adm_name: (Path(_task_dir("exam","EXAM-003")/"references"/adm_name)).read_bytes()}, check=check)
    _write_text(_task_dir("exam", "EXAM-003") / "references" / "format.txt", "fixture_format=%s\n" % adm_fmt, check=check)
    # EXAM-004：真题 CSV + 大纲 TXT
    syllabus = "\n".join(_header_lines("北原大学 2026 计算机专业初试大纲（虚构）") + [
        "第一章 数据结构：线性表、树、图、查找、排序。",
        "第二章 计算机组成原理：CPU、存储、总线。",
        "第三章 操作系统：进程、内存、文件系统。",
        "第四章 计算机网络：TCP/IP、路由。",
        "2024 版新增：第 2.5 节 流水线。",
        "2025 版新增：第 4.3 节 拥塞控制；第 3.4 节 死锁检测；第 1.6 节 B+ 树。",
        "",
    ])
    _write_references("exam", "EXAM-004", {
        "questions.csv": csv_bytes(exam_header, exam_rows),
        "syllabus.txt": syllabus,
    }, check=check)
    # EXAM-005：3 个 CSV 合并
    def split_csv(idx: int) -> bytes:
        part = [r for i, r in enumerate(sorted(exam_rows, key=lambda x: (x[0], x[1], x[2]))) if i % 3 == idx]
        return csv_bytes(exam_header, part)
    _write_references("exam", "EXAM-005", {
        "questions_a.csv": split_csv(0),
        "questions_b.csv": split_csv(1),
        "questions_c.csv": split_csv(2),
    }, check=check)

    # PDF 类
    _write_references("pdf", "PDF-001", {"brief_2026_5p.pdf": pdfs["brief_2026_5p.pdf"]}, check=check)
    _write_references("pdf", "PDF-002", {"catalog_table.pdf": pdfs["catalog_table.pdf"]}, check=check)
    _write_references("pdf", "PDF-003", {"brief_long_20p.pdf": pdfs["brief_long_20p.pdf"]}, check=check)
    _write_references("pdf", "PDF-004", {
        "brief_2025.pdf": pdfs["brief_2025.pdf"],
        "brief_2026.pdf": pdfs["brief_2026.pdf"],
    }, check=check)
    _write_references("pdf", "PDF-005", {"brief_long_20p.pdf": pdfs["brief_long_20p.pdf"]}, check=check)

    # UNI-008/009：镜像站 HTML
    _write_references("university", "UNI-008", {
        "source_01.html": build_major_catalog_html(),
    }, check=check)
    _write_references("university", "UNI-009", {
        "source_01.html": build_major_catalog_html(conflict=True),
        "source_02.html": build_college_page_html(),
    }, check=check)

    # POL-003/004：政策文本
    _write_references("policy", "POL-003", {
        "policy_2024.txt": policies["policy_2024.txt"],
        "policy_2025.txt": policies["policy_2025.txt"],
        "policy_2026.txt": policies["policy_2026.txt"],
    }, check=check)
    _write_references("policy", "POL-004", {
        "policy_2025.txt": policies["policy_2025.txt"],
        "policy_2026.txt": policies["policy_2026.txt"],
    }, check=check)

    # PLAN-004：薄弱点诊断表
    _write_references("planning", "PLAN-004", {"weak_points.csv": csv_bytes(weak_header, weak_rows)}, check=check)

    # RES-001/002：资料集
    cat_rows = [[m["code"], m["name"], m["type"], 20 + i * 3, EXAM_SUBJECTS[i % len(EXAM_SUBJECTS)]]
                for i, m in enumerate(FAKE_MAJORS)]
    catalog_csv = csv_bytes(["code", "name", "type", "plan", "subject"], cat_rows)
    _write_references("research", "RES-001", {
        "majors.html": build_major_catalog_html(),
        "majors.csv": catalog_csv,
    }, check=check)
    _write_references("research", "RES-002", {
        "majors.html": build_major_catalog_html(),
        "majors.csv": catalog_csv,
        "scores.csv": csv_bytes(adm_header, adm_rows),
    }, check=check)

    # HAL-002/003/005
    _write_references("hallucination", "HAL-002", {
        "college.txt": "\n".join(_header_lines("北原大学 计算机学院 招生说明（虚构）") + [
            "专业代码 081200 学制 3 年。",
            "复试方式：现场面试，满分 100。",
            "（本资料未提供参考书目信息。）",
            "",
        ]),
    }, check=check)
    _write_references("hallucination", "HAL-003", {
        "scores_conflict.csv": csv_bytes(lead_header, lead_rows),
    }, check=check)
    _write_references("hallucination", "HAL-005", {
        "claims.html": build_hallucination_html(),
    }, check=check)

    # SEARCH-011/012：快照副本进 references（供离线题直接读）
    snap = build_snapshot_library()
    for tid in ("SEARCH-011", "SEARCH-012"):
        sub = {k.split("/", 1)[1]: v for k, v in snap.items() if k.startswith(tid + "/")}
        _write_references("search", tid, sub, check=check)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    global _USE_ENHANCED
    parser = argparse.ArgumentParser(description="KaoyanBench 确定性 fixture 生成器")
    parser.add_argument("--check", action="store_true", help="只校验不写入；不一致退出码 1")
    parser.add_argument(
        "--enhanced",
        action="store_true",
        help="可选依赖增强路径（fpdf2/openpyxl）；默认走内置零依赖路径以保证跨环境字节一致",
    )
    args = parser.parse_args(argv)
    _USE_ENHANCED = bool(args.enhanced)

    if not _USE_ENHANCED:
        sys.stderr.write(
            "[info] 默认零依赖路径：PDF 用内置最小生成器，Excel 降级 CSV。"
            "（如需 fpdf2/openpyxl 增强，加 --enhanced）\n"
        )
    else:
        if not _has_fpdf2():
            sys.stderr.write("[warn] 未检测到 fpdf2，PDF 回退内置最小生成器。\n")
        try:
            import openpyxl  # noqa: F401

            sys.stderr.write("[info] --enhanced：Excel 使用 .xlsx（openpyxl）。\n")
        except Exception:
            sys.stderr.write("[warn] 未检测到 openpyxl，Excel 降级为 .csv。\n")

    distribute(check=args.check)
    if args.check:
        sys.stderr.write(f"[check] 全部 {len(_CREATED)} 个 fixture 与预期一致 ✅\n")
    else:
        sys.stderr.write(f"[ok] 生成 {len(_CREATED)} 个 fixture 文件（确定性，seed={SEED}）。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
