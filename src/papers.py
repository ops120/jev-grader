"""试卷注册表：让工作台同时支持多份试卷。

一份试卷 = 一份题库（exam.json）+ 一组判定 spec（src/judge/specs/*.json）。
spec 里是判据表、措辞、判断清单与 state 组装规则；把概率折成答案的算术在
src/judge/strategies.py 的三个具名策略里。specs 给多个时由题目的 kind 选一个
（数学卷单选与多选是两套判断清单）。
"""
from __future__ import annotations

from pathlib import Path

from judge import spec as _spec

ROOT = Path(__file__).resolve().parent.parent

PAPERS: dict[str, dict] = {
    "english": {
        "id": "english",
        "subject": "英语",
        "title": "2025 年高考英语（全国卷）",
        "exam": ROOT / "data" / "exam.json",
        "traces": ROOT / "data" / "traces",
        "specs": ("english",),
        "has_latex": False,
    },
    "math": {
        "id": "math",
        "subject": "数学",
        "title": "2025 年高考数学（新Ⅰ卷）",
        "exam": ROOT / "data" / "exam_math.json",
        "traces": ROOT / "data" / "traces_math",
        "specs": ("math", "math_multi"),
        "has_latex": True,
    },


    "xc_fs": {
        "id": "xc_fs", "subject": "行测副省级", "title": "2026 国考行测（副省级）",
        "exam": ROOT / "data" / "exam_xc_副省级.json",
        "traces": ROOT / "data" / "traces_xc_fs",
        "specs": ("xingce",), "has_latex": False,
    },
    "xc_ds": {
        "id": "xc_ds", "subject": "行测地市类", "title": "2026 国考行测（地市类）",
        "exam": ROOT / "data" / "exam_xc_地市类.json",
        "traces": ROOT / "data" / "traces_xc_ds",
        "specs": ("xingce",), "has_latex": False,
    },
    "xc_zf": {
        "id": "xc_zf", "subject": "行测行政执法类", "title": "2026 国考行测（行政执法类）",
        "exam": ROOT / "data" / "exam_xc_行政执法类.json",
        "traces": ROOT / "data" / "traces_xc_zf",
        "specs": ("xingce",), "has_latex": False,
    },
}

DEFAULT = "english"


def get(paper_id: str | None) -> dict:
    """按 id 取试卷。未知 id 直接报错，**不做静默回退**——
    否则已删除或拼错的 id 会悄悄拿到默认卷的数据，界面显示的是另一份试卷。"""
    if paper_id in (None, ""):
        return PAPERS[DEFAULT]
    if paper_id not in PAPERS:
        raise KeyError(paper_id)
    return PAPERS[paper_id]


def listing() -> dict:
    """给前端的 ``/api/papers`` 载荷：试卷清单 + 轨迹版本。

    ``trace_version`` 必须这里给而不是前端写死：界面用它把旧代码产出的轨迹标成
    "过旧"，版本对不上就会把可信的判定读成不可信的。前端仍保留一个兜底常量，
    只在接口整体不可用时生效，并在界面上明说是兜底值。

    轨迹文件名前缀随科目不同（英语 Q、数学 M），故按 *.json 统计而非写死前缀。
    """
    papers_out = []
    for p in PAPERS.values():
        solved = len([f for f in p["traces"].glob("*.json")
                      if not f.name.startswith("_")]) if p["traces"].exists() else 0
        papers_out.append({"id": p["id"], "subject": p["subject"], "title": p["title"],
                           "solved": solved, "has_latex": p["has_latex"],
                           "exam_ready": p["exam"].exists()})
    return {"papers": papers_out, "trace_version": trace_version()}


def trace_version() -> int:
    """所有已注册 spec 里最高的轨迹版本。注册了新卷却没提升版本，读到的还是旧语义。"""
    return max(_spec.get(name).trace_version()
               for cfg in PAPERS.values() for name in cfg["specs"])
