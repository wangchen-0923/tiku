"""绑定校验验证：题号 / 题型 / 选项数 / 空数 / 分值各造一个不一致用例。

运行：
    python scripts/verify_sheet_binding.py

覆盖验收 4：
  · 题号集合一致且连续
  · 每题题型一致
  · 选择题选项数一致、判断题必须 2 选项
  · 填空题空数一致、每空分之和 = 试卷该题分值
  · 解答题/作文题分值一致
  · 总分一致
  · 任一不满足 → 阻断，并给出「第几题、期望值、实际值」
"""

from __future__ import annotations

import copy
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, r"D:\tiku\server")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.sheet.sections import build_question_types  # noqa: E402
from apps.sheet.services import validate_sheet_against_paper  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def _paper() -> list[dict]:
    return [
        {"seq": 1, "type": "SINGLE_CHOICE", "kind": "CHOICE",
         "score": 2.0, "options": 4, "blanks": None},
        {"seq": 2, "type": "SINGLE_CHOICE", "kind": "CHOICE",
         "score": 2.0, "options": 4, "blanks": None},
        {"seq": 3, "type": "TRUE_FALSE", "kind": "TRUE_FALSE",
         "score": 2.0, "options": 2, "blanks": None},
        {"seq": 4, "type": "FILL_BLANK", "kind": "FILL_BLANK",
         "score": 4.0, "options": 0, "blanks": 2},
        {"seq": 5, "type": "CALCULATION", "kind": "ANSWER",
         "score": 10.0, "options": 0, "blanks": None},
    ]


def _sections() -> list[dict]:
    return [
        {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择题",
         "start_seq": 1, "count": 2, "options": 4, "per_row": 5,
         "score": 2.0, "items": []},
        {"order": 2, "type": "TRUE_FALSE", "title": "二、判断题",
         "start_seq": 3, "count": 1, "options": 2, "per_row": 5,
         "score": 2.0, "items": []},
        {"order": 3, "type": "FILL_BLANK", "title": "三、填空题",
         "start_seq": 4, "count": 1, "options": 0, "per_row": 2,
         "score": 4.0,
         "items": [{"seq": 4, "blanks": 2, "chars": 6, "per_line": 2,
                    "separator": "　　", "blank_scores": [2.0, 2.0],
                    "score": 4.0}]},
        {"order": 4, "type": "ANSWER", "title": "四、解答题",
         "start_seq": 5, "count": 1, "options": 0, "per_row": 0,
         "score": 10.0,
         "items": [{"seq": 5, "lines": 8, "style": "SOLID",
                    "sub_count": 1, "score": 10.0}]},
    ]


def _tpl(sections: list[dict]):
    return SimpleNamespace(question_types=build_question_types(sections))


def _run(sections: list[dict]) -> dict:
    return validate_sheet_against_paper(_tpl(sections), _paper())


def _has(result: dict, code: str, seq: int | None = None) -> bool:
    return any(
        b["code"] == code and (seq is None or b["seq"] == seq)
        for b in result["blocking"]
    )


def main() -> int:
    print("\n【B1】完全匹配")
    r = _run(_sections())
    check("题号/题型/选项/空数/分值全一致 → 通过",
          r["ok"] and not r["blocking"], str(r["blocking"][:2]))
    check("总分为 20", abs(r["paper_total_score"] - 20.0) < 0.01,
          str(r["paper_total_score"]))

    print("\n【B2】题号不连续")
    s = copy.deepcopy(_sections())
    s[1]["start_seq"] = 4
    s[2]["start_seq"] = 5
    s[2]["items"][0]["seq"] = 5
    s[3]["start_seq"] = 6
    s[3]["items"][0]["seq"] = 6
    r = _run(s)
    check("题号断层 → 阻断", not r["ok"])
    check("报 SHEET_SEQ_DISCONTINUOUS",
          _has(r, "SHEET_SEQ_DISCONTINUOUS"),
          str([b["code"] for b in r["blocking"]]))

    print("\n【B3】题型不一致")
    s = copy.deepcopy(_sections())
    s[1]["type"] = "SINGLE_CHOICE"
    s[1]["options"] = 4
    s[1]["start_seq"] = 3
    s[1]["items"] = []
    s[2]["type"] = "ANSWER"
    s[2]["start_seq"] = 4
    s[2]["items"] = [{"seq": 4, "lines": 8, "style": "SOLID",
                      "sub_count": 1, "score": 4.0}]
    s[2]["score"] = 4.0
    s[3]["type"] = "ANSWER"
    s[3]["start_seq"] = 5
    s[3]["items"] = [{"seq": 5, "lines": 8, "style": "SOLID",
                      "sub_count": 1, "score": 10.0}]
    s[3]["score"] = 10.0
    r = _run(s)
    check("第 3 题判断题被做成解答题 → 阻断",
          _has(r, "TYPE_MISMATCH", 3),
          str([b for b in r["blocking"] if b["seq"] == 3][:1]))
    b = next((x for x in r["blocking"] if x["seq"] == 3), {})
    check("提示含「第 3 题」与期望/实际值",
          "第 3 题" in b.get("message", "")
          and b.get("expected") and b.get("actual"),
          b.get("message", ""))

    print("\n【B4】选项数不一致")
    s = copy.deepcopy(_sections())
    s[0]["options"] = 6
    r = _run(s)
    check("第 1 题选项 6 vs 试卷 4 → 阻断",
          _has(r, "OPTION_COUNT_MISMATCH", 1),
          str([b for b in r["blocking"] if b["seq"] == 1][:1]))
    b = next((x for x in r["blocking"] if x["seq"] == 1), {})
    check("定位到第 1 题且给期望/实际",
          "第 1 题" in b.get("message", "")
          and b.get("expected") == 4 and b.get("actual") == 6,
          f'{b.get("expected")}/{b.get("actual")}')

    print("\n【B5】判断题必须 2 选项")
    s = copy.deepcopy(_sections())
    s[1]["options"] = 4
    r = _run(s)
    check("判断题做成 4 选项 → 阻断",
          _has(r, "TF_OPTION_MISMATCH", 3)
          or _has(r, "OPTION_COUNT_MISMATCH", 3),
          str([b for b in r["blocking"] if b["seq"] == 3][:1]))

    print("\n【B6】填空空数不一致")
    s = copy.deepcopy(_sections())
    s[2]["items"][0]["blanks"] = 3
    s[2]["items"][0]["blank_scores"] = [2.0, 1.0, 1.0]
    r = _run(s)
    check("第 4 题空数 3 vs 试卷 2 → 阻断",
          _has(r, "BLANK_COUNT_MISMATCH", 4),
          str([b for b in r["blocking"] if b["seq"] == 4][:1]))
    b = next((x for x in r["blocking"]
              if x["seq"] == 4 and x["code"] == "BLANK_COUNT_MISMATCH"), {})
    check("定位到第 4 题且给期望/实际",
          "第 4 题" in b.get("message", "")
          and b.get("expected") == 2 and b.get("actual") == 3,
          f'{b.get("expected")}/{b.get("actual")}')

    print("\n【B7】分值不一致")
    s = copy.deepcopy(_sections())
    s[3]["items"][0]["score"] = 8.0
    s[3]["score"] = 8.0
    r = _run(s)
    check("第 5 题分值 8 vs 试卷 10 → 阻断",
          _has(r, "SCORE_MISMATCH", 5),
          str([b for b in r["blocking"] if b["seq"] == 5][:1]))
    check("总分也不一致 → TOTAL_SCORE_MISMATCH",
          _has(r, "TOTAL_SCORE_MISMATCH"),
          str([b["code"] for b in r["blocking"]]))
    b = next((x for x in r["blocking"] if x["seq"] == 5), {})
    check("定位到第 5 题且给期望/实际",
          "第 5 题" in b.get("message", "")
          and b.get("expected") == 10.0 and b.get("actual") == 8.0,
          f'{b.get("expected")}/{b.get("actual")}')

    print("\n【B8】缺题 / 多题")
    s = copy.deepcopy(_sections())
    s[3]["count"] = 0
    s[3]["items"] = []
    r = _run(s)
    check("答题卡缺第 5 题 → 阻断",
          _has(r, "MISSING_QUESTION", 5),
          str([b["code"] for b in r["blocking"]]))
    check("题量不一致也报出", _has(r, "COUNT_MISMATCH"))

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
