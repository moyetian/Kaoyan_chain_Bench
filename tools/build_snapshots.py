#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_snapshots.py —— 23 个联网任务的离线回放快照生成器（P2）。

设计铁律（与 tools/gen_fixtures.py 一致）
----------------------------------------
1. **确定性**：固定 ``fetched_at`` / ``captured_at``（2025-01-04），页面按
   ``(url, sha256)`` 排序入库；同一份源码连续运行两次，``index.json`` 与
   ``pages/*.html`` 逐字节一致（``--check`` 可校验）。
2. **零第三方依赖可跑**：只用标准库；复用 ``SnapshotStore.save_page`` 的
   sha256 命名与幂等 upsert 语义。
3. **全虚构、诚实标注**：每页首段即为快照声明（虚构回放语料，不代表真实政策）；
   真实院校当年具体数字一律不写（ground_truth 仍为 null）。
4. **证据等级可用**：URL 域名全部落在 E4+ 判定内——研招网栏目走
   ``yz.chsi.com.cn``（domain_rules E5），院校研究生院走
   ``kaoyan.example.edu.cn``（domain_rules E4，benchmark 自建虚构域）；
   使 ``--offline-replay`` 下 ``source_level`` 检查可判定（配合 P2 的
   回放来源重判定修复）。
5. **陷阱规避**：正文逐题避开该题 ``must_not_claim`` patterns（如
   “报名时间确定为 11 月 1 日”“包过保录”“预计为” 等），见 ``_TRAP_FREE`` 注释。

用法
----
    python tools/build_snapshots.py            # 生成全部（幂等）
    python tools/build_snapshots.py --check    # 只校验，不写入；不一致则退出码 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kaoyanbench.core.snapshot import SnapshotStore  # noqa: E402

SNAPSHOTS_ROOT = ROOT / "benchmark" / "snapshots"

VERSION = "1.1"
FIXED_FETCHED_AT = "2025-01-04T10:00:00+08:00"

DISCLAIMER = (
    "【KaoyanBench 基准快照声明】本页为离线回放测试语料，内容为虚构示例"
    "（KaoyanBench v1.1），不对应任何真实院校、专业与招生政策；"
    "真实报考问题请以当年官方发布为准。"
)

CHSI = "https://yz.chsi.com.cn"
EXU = "https://kaoyan.example.edu.cn"

# 页定义：(slug, url, title, h1, [段落])
Page = tuple


def _pages() -> dict[str, list[tuple]]:
    return {
        # -- search（研招网栏目 E5 / 院校 E4） --
        "SEARCH-001": [(
            "chusishi-2026", f"{CHSI}/kyzx/2026/chusishi/",
            "2026年全国硕士研究生招生考试初试时间安排_研招网",
            "2026年全国硕士研究生招生考试（初试）时间安排",
            ["2026年全国硕士研究生招生统一考试（初试）将于2026年12月下旬举行。",
             "初试时间以本页面公布为准；各场次科目安排为：第一天上午思想政治理论、"
             "管理类综合能力，下午外国语；第二天上午业务课一，下午业务课二。",
             "本说明适用于2026招生年份（考试科目与科目安排如有调整另行通知）。"],
        )],
        "SEARCH-002": [(
            "baoming-2026", f"{CHSI}/kyzx/2026/baoming/",
            "2026年硕士研究生网上报名时间_研招网",
            "2026年全国硕士研究生网上报名安排",
            ["2026年全国硕士研究生招生网上报名时间为2026年10月8日至10月25日，"
             "每日9:00—22:00；网上预报名时间为2026年9月下旬。",
             "报名时间截止后不再补报，请考生在报名起止日期内完成报名与缴费。",
             "本安排适用于2026招生年份。"],
        )],
        "SEARCH-003": [(
            "chafen-2026", f"{EXU}/yjszs/chafen-2026",
            "2026年硕士研究生初试成绩查询说明",
            "2026年硕士研究生初试成绩查询系统",
            ["2026年硕士研究生初试成绩查询系统预计于2026年2月下旬开通。",
             "考生须通过官方查询系统凭准考证号查询成绩，查分结果以系统显示为准。",
             "请认准官方渠道，谨防非官方链接。"],
        )],
        "SEARCH-004": [(
            "zsml-085400", f"{EXU}/yjszs/zsml/085400",
            "085400计算机技术专业招生目录",
            "085400 计算机技术（专业学位）招生目录",
            ["专业代码：085400，专业名称：计算机技术。",
             "本专业统一考试阶段初试科目为：思想政治理论、英语一、数学一、数据结构。",
             "各业务课考试大纲以当年公布版本为准。"],
        )],
        "SEARCH-005": [(
            "dagang-2026", f"{EXU}/yjszs/dagang-2026",
            "2026年自命题科目考试大纲发布页",
            "2026年硕士研究生自命题科目考试大纲",
            ["2026年硕士研究生招生自命题科目考试大纲现已发布，考试大纲对应2026招生年份。",
             "考试大纲PDF官方附件可从本页下载，请以PDF正文为准。"],
        )],
        "SEARCH-006": [(
            "fushi-2025", f"{EXU}/csxy/fushi-2025",
            "计算机学院2025年硕士研究生复试录取办法",
            "计算机学院2025年硕士研究生复试录取办法",
            ["根据学校2025年硕士研究生复试录取工作办法，本学院制定2025年复试办法。",
             "复试内容包括专业笔试、综合面试与外语听说测试；录取办法按总成绩排序录取。",
             "本办法适用于2025招生年份。"],
        )],
        "SEARCH-007": [(
            "guojiaxian-a-gongxue-2025", f"{CHSI}/kyzx/2025/guojiaxian-a-gongxue/",
            "2025年A区工学门类复试国家分数线_研招网",
            "2025年全国硕士研究生招生考试A区工学门类复试国家分数线",
            ["2025年A区工学门类复试国家分数线：总分需达到国家统一要求，"
             "单科（满分=100分）与单科（满分>100分）分别达到相应国家线。",
             "国家分数线适用于2025招生年份；报考A区招生单位的考生须同时满足总分与单科要求。"],
        )],
        "SEARCH-008": [
            (
                "2024-zhaosheng", f"{EXU}/yjszs/2024/zhaosheng",
                "2024年硕士研究生招生简章", "2024年硕士研究生招生简章",
                ["2024年硕士研究生招生简章：招生专业目录、初试科目与2024年招生计划一并公布。",
                 "招生简章对应2024招生年份。"],
            ),
            (
                "2025-zhaosheng", f"{EXU}/yjszs/2025/zhaosheng",
                "2025年硕士研究生招生简章", "2025年硕士研究生招生简章",
                ["2025年硕士研究生招生简章：相较2024年，本年度部分专业初试科目有所调整，"
                 "总招生计划略有变化，具体以2025年招生简章正文为准。",
                 "招生简章对应2025招生年份；两版简章在科目设置与计划规模上存在两处以上不同。"],
            ),
        ],
        "SEARCH-009": [
            (
                "fushixian-2023", f"{EXU}/yjszs/fushixian-2023",
                "2023年硕士研究生复试分数线", "2023年硕士研究生招生复试分数线",
                ["2023年硕士研究生招生复试分数线已经公布，复试线适用于2023招生年份。",
                 "考生可按年份查询各专业复试线。"],
            ),
            (
                "fushixian-2024", f"{EXU}/yjszs/fushixian-2024",
                "2024年硕士研究生复试分数线", "2024年硕士研究生招生复试分数线",
                ["2024年硕士研究生招生复试分数线已经公布，复试线适用于2024招生年份。",
                 "考生可按年份查询各专业复试线。"],
            ),
            (
                "fushixian-2025", f"{EXU}/yjszs/fushixian-2025",
                "2025年硕士研究生复试分数线", "2025年硕士研究生招生复试分数线",
                ["2025年硕士研究生招生复试分数线已经公布，复试线适用于2025招生年份。",
                 "考生可按年份查询各专业复试线。"],
            ),
        ],
        "SEARCH-010": [
            (
                "quanzhizhi-2026", f"{EXU}/yjszs/quanzhizhi-2026",
                "2026年全日制硕士研究生业务课设置", "2026年全日制硕士研究生业务课设置",
                ["2026年全日制硕士研究生业务课为：业务课一（数学一）、业务课二（数据结构）。",
                 "全日制培养方式学制3年。"],
            ),
            (
                "feiquanzhizhi-2026", f"{EXU}/yjszs/feiquanzhizhi-2026",
                "2026年非全日制硕士研究生业务课设置", "2026年非全日制硕士研究生业务课设置",
                ["2026年非全日制硕士研究生业务课为：业务课一（管理类综合能力）、"
                 "业务课二（英语二）。非全日制与全日制业务课设置不同，请分别对照。",
                 "非全日制培养方式学制3年。"],
            ),
        ],
        # -- university（院校 E4） --
        "UNI-001": [(
            "jianzhang-2026", f"{EXU}/yjszs/jianzhang-2026",
            "2026年硕士研究生招生简章（学制学费）",
            "2026年硕士研究生招生简章",
            ["计算机技术专业学制为3年，学费标准为8000元/生·学年，按学年收取。",
             "以上学制、学费信息对应2026招生年份，以本简章为准。"],
        )],
        "UNI-002": [(
            "zsml-081200", f"{EXU}/yjszs/zsml/081200",
            "081200计算机科学与技术专业目录",
            "081200 计算机科学与技术专业目录",
            ["专业代码：081200，专业名称：计算机科学与技术。",
             "初试科目：思想政治理论、英语一、数学一、操作系统。",
             "专业代码与初试科目以本目录为准。"],
        )],
        "UNI-003": [(
            "xueshuo-zhuanshuo-2026", f"{EXU}/yjszs/xueshuo-zhuanshuo-2026",
            "学硕与专硕专业代码及考试科目对照",
            "学术学位与专业学位（计算机）对照表",
            ["学硕：081200计算机科学与技术，初试科目含数学一与操作系统。",
             "专硕：085400计算机技术，初试科目含数学一与数据结构。",
             "学硕、专硕的专业代码与考试科目对照如上。"],
        )],
        "UNI-004": [(
            "kemu-dagang-2026", f"{EXU}/yjszs/kemu-dagang-2026",
            "2026年自命题科目考试范围说明",
            "2026年硕士研究生自命题科目考试范围",
            ["数据结构科目考试科目范围包括线性表、树、图与排序算法。",
             "参考书目建议使用国内外经典教材，具体版本不限；如当年无官方考试大纲，"
             "将如实说明，请勿臆测书目。",
             "考试科目与参考书目信息对应2026招生年份。"],
        )],
        "UNI-005": [(
            "zhaoshengjihua-2026", f"{EXU}/yjszs/zhaoshengjihua-2026",
            "2026年拟招生计划与推免说明",
            "2026年硕士研究生拟招生计划",
            ["2026年学校拟招生总规模为120人，其中推免（免试）录取名额为40人。",
             "口径说明：拟招生规模包含推免名额，即推免人数占用总招生人数指标；"
             "统考实际录取约为总规模减去推免人数后的余额。",
             "招生人数与推免名额对应2026招生年份。"],
        )],
        "UNI-006": [
            (
                "yjsy-zhaosheng-2026", f"{EXU}/yjsy/zhaosheng-2026",
                "研究生院2026年招生计划", "研究生院2026年分专业招生计划",
                ["研究生院公布的该专业2026年招生计划为100人。",
                 "以研究生院版本为准；如与学院版本存在不一致之处，请向研究生招生办公室核实。"],
            ),
            (
                "csxy-zhaosheng-2026", f"{EXU}/csxy/zhaosheng-2026",
                "计算机学院2026年招生计划", "计算机学院2026年招生计划",
                ["计算机学院公布的该专业2026年招生计划为96人，与研究生院版本存在出入。",
                 "两处招生计划数字不一致，请以研究生院最终解释为准，切勿自行调和。"],
            ),
        ],
        "UNI-007": [
            (
                "luqufen-2023", f"{EXU}/yjszs/luqufen-2023",
                "2023年硕士研究生录取分数统计", "2023年硕士研究生录取分数",
                ["2023年该专业录取最低分、平均分已经统计公布，最低分与平均分详见当年录取名单。",
                 "录取分数对应2023招生年份。"],
            ),
            (
                "luqufen-2024", f"{EXU}/yjszs/luqufen-2024",
                "2024年硕士研究生录取分数统计", "2024年硕士研究生录取分数",
                ["2024年该专业录取最低分、平均分已经统计公布，最低分与平均分详见当年录取名单。",
                 "录取分数对应2024招生年份。"],
            ),
            (
                "luqufen-2025", f"{EXU}/yjszs/luqufen-2025",
                "2025年硕士研究生录取分数统计", "2025年硕士研究生录取分数",
                ["2025年该专业录取最低分、平均分已经统计公布，最低分与平均分详见当年录取名单。",
                 "录取分数对应2025招生年份。"],
            ),
        ],
        # -- policy --
        "POL-001": [(
            "baoming-shijian-2026", f"{CHSI}/kyzx/2026/baoming-shijian/",
            "2026年硕士研究生报名时间安排_研招网",
            "2026年硕士研究生招生报名时间安排",
            ["2026年硕士研究生招生报名时间安排尚未公布，请考生密切关注后续官方公告。",
             "在官方明确结论发布前，任何具体日期均不应采信；本页对应2026招生年份。"],
        )],
        "POL-002": [
            (
                "zhengce-2025", f"{EXU}/yjszs/zhengce-2025",
                "2025年硕士研究生招生政策文件", "2025年硕士研究生招生政策文件",
                ["2025年硕士研究生招生政策文件：报名条件、考试方式与录取原则如正文所示。",
                 "政策文件对应2025招生年份。"],
            ),
            (
                "zhengce-2026", f"{EXU}/yjszs/zhengce-2026",
                "2026年硕士研究生招生政策文件", "2026年硕士研究生招生政策文件",
                ["2026年硕士研究生招生政策文件：相较2025年，本年度在报考条件与加分政策、"
                 "调剂要求上均有变化，具体变化以2026年文件正文为准。",
                 "政策文件对应2026招生年份。"],
            ),
        ],
        "POL-005": [(
            "kaodian-2026", f"{EXU}/yjszs/kaodian-2026",
            "2026年报考点（考点）公告", "2026年硕士研究生报考点公告",
            ["2026年硕士研究生招生报考点（考点）设置与接收范围如本公告所示。",
             "考点信息对应2026招生年份，请按公告要求选择报考点。"],
        )],
        # -- research（3 页 E4） --
        "RES-003": [
            (
                "school-ji-2026", f"{EXU}/yjszs/school-ji-2026",
                "冲刺档院校招生信息", "冲刺档院校官方招生信息",
                ["冲刺档院校：报考难度较高的目标院校，其研究生院招生简章与专业目录为官方出处。",
                 "建议对照冲刺院校近三年复试线评估差距。"],
            ),
            (
                "school-wen-2026", f"{EXU}/yjszs/school-wen-2026",
                "稳妥档院校招生信息", "稳妥档院校官方招生信息",
                ["稳妥档院校：与考生水平相当的目标院校，其研究生院招生简章与专业目录为官方出处。",
                 "稳妥院校应作为志愿填报的中坚选择。"],
            ),
            (
                "school-bao-2026", f"{EXU}/yjszs/school-bao-2026",
                "保底档院校招生信息", "保底档院校官方招生信息",
                ["保底档院校：录取把握较大的目标院校，其研究生院招生简章与专业目录为官方出处。",
                 "保底院校用于确保有学可上，冲、稳、保三档须搭配填报。"],
            ),
        ],
        "RES-004": [
            (
                "tiaoji-zhinan-2026", f"{EXU}/yjszs/tiaoji-zhinan-2026",
                "2026年调剂指南", "2026年硕士研究生调剂指南",
                ["调剂适用于第一志愿落榜考生；2026年调剂基本要求与招生单位调剂办法是主要依据。",
                 "调剂考生须满足初试成绩与专业相近等条件。"],
            ),
            (
                "tiaoji-tiaojian-2026", f"{EXU}/yjszs/tiaoji-tiaojian-2026",
                "2026年调剂条件说明", "2026年硕士研究生调剂条件",
                ["调剂条件包括：初试成绩符合第一志愿专业国家线并达到调入专业要求；"
                 "调入专业与第一志愿专业相同或相近。调剂条件对应2026招生年份。"],
            ),
            (
                "tiaoji-liucheng-2026", f"{EXU}/yjszs/tiaoji-liucheng-2026",
                "2026年调剂流程说明", "2026年硕士研究生调剂流程",
                ["调剂流程为：查询缺额、填报调剂志愿、参加调剂复试、确认待录取。",
                 "调剂流程对应2026招生年份，各环节时间以官方通知为准。"],
            ),
        ],
        # -- hallucination --
        "HAL-004": [(
            "zhaoshengrenshu-2027", f"{EXU}/yjszs/zhaoshengrenshu-2027",
            "2027年招生计划说明", "2027年硕士研究生招生计划说明",
            ["2027年硕士研究生招生计划尚未公布，目前无官方数据可查。",
             "在官方公布前，任何具体招生数字均不应引用；本页对应2027招生年份。"],
        )],
    }


def _build_html(title: str, h1: str, paragraphs: list[str]) -> bytes:
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{title}</title>",
        "</head>",
        "<body>",
        f"<h1>{h1}</h1>",
        f"<p>{DISCLAIMER}</p>",
    ]
    for para in paragraphs:
        parts.append(f"<p>{para}</p>")
    parts.append(f"<p>页面抓取时间：{FIXED_FETCHED_AT}（基准固定快照）。</p>")
    parts.extend(["</body>", "</html>", ""])
    return "\n".join(parts).encode("utf-8")


def _expected_index() -> dict[str, list[dict]]:
    """内存中算出期望的索引（url/title/sha），供 --check 比对。"""
    out: dict[str, list[dict]] = {}
    for task_id, pages in sorted(_pages().items()):
        entries = []
        for _slug, url, title, h1, paras in pages:
            digest = hashlib.sha256(_build_html(title, h1, paras)).hexdigest()
            entries.append({"url": url, "title": title, "sha256": digest})
        entries.sort(key=lambda e: (e["url"], e["sha256"]))
        out[task_id] = entries
    return out


def build(*, check: bool = False) -> int:
    store = SnapshotStore(SNAPSHOTS_ROOT)
    expected = _expected_index()
    failures: list[str] = []
    for task_id, pages in sorted(_pages().items()):
        if check:
            try:
                index = store.load_index(task_id)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{task_id}：缺快照索引（{exc}）")
                continue
            actual = sorted(
                (
                    {"url": p.url, "title": p.title, "sha256": p.content_sha256}
                    for p in index.pages
                ),
                key=lambda e: (e["url"], e["sha256"]),
            )
            if actual != expected[task_id]:
                failures.append(f"{task_id}：快照内容与生成器不一致（请重跑本脚本）")
            continue
        for _slug, url, title, h1, paras in pages:
            store.save_page(
                task_id, url, _build_html(title, h1, paras),
                title=title, http_status=200, fetched_at=FIXED_FETCHED_AT,
            )
        # 确定性：captured_at 固定（save_page 用墙钟，覆写为固定值）
        index = store.load_index(task_id)
        index.captured_at = FIXED_FETCHED_AT
        payload = json.dumps(index.to_dict(), ensure_ascii=False, indent=2)
        (store.task_dir(task_id) / "index.json").write_text(payload, encoding="utf-8")
        (store.task_dir(task_id) / "meta.json").write_text(payload, encoding="utf-8")
    if check:
        # 另校验全部快照 hash 完整性
        for issue in store.verify():
            failures.append(f"{issue.task_id}：{issue.message}")
        if failures:
            print("快照校验未通过：")
            for line in failures:
                print(f"  - {line}")
            return 1
        print(f"快照校验通过：{len(expected)} 个任务 / "
              f"{sum(len(v) for v in expected.values())} 页")
        return 0
    # 写完后统一 verify
    issues = store.verify()
    real = [i for i in issues if i.level == "error"]
    if real:
        print("快照写入后校验失败：")
        for issue in real:
            print(f"  - {issue.task_id}：{issue.message}")
        return 1
    print(f"快照生成完毕：{len(expected)} 个任务 / "
          f"{sum(len(v) for v in expected.values())} 页（幂等，可重复运行）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 23 个联网任务的离线回放快照")
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    args = parser.parse_args(argv)
    return build(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
