"""试卷注册表：从 data/banks/ 动态发现题库，一份题库文件就是一份试卷。

题库不再由本仓库的解析脚本生成（提取功能已抽离）。用户按 ``templates/`` 的模板
制作题库 JSON，通过工作台上传，或直接放进 ``data/banks/``——刷新即出现在顶栏，
不需要改任何代码。

一份试卷 = 题库文件 + 题库 meta 里声明的判定 spec（src/judge/specs/*.json）。
spec 里是判据表、措辞、判断清单与 state 组装规则；把概率折成答案的算术在
src/judge/strategies.py 的三个具名策略里。specs 给多个时由题目的 kind 选一个
（数学卷单选与多选是两套判断清单）。

诚实性约束
----------
- 未知 id 直接报错，**不做静默回退**——否则已删除或拼错的 id 会悄悄拿到
  默认卷的数据，界面显示的是另一份试卷。
- meta 读不出的题库照样列出，但带 load_error，绝不悄悄消失，也不会顶替别的卷。
- ``trace_version`` 属于代码（全部 spec 文件的最大版本），不由某份题库决定：
  版本对不上时界面会把旧轨迹标成「过旧」，宁可重算也不误读。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BANKS_DIR = ROOT / "data" / "banks"

# 旧版硬编码注册表（五份实测卷）的迁移通道：本机若还留着这些题库文件，
# 按老 id 继续可用，**旧的轨迹目录也一并沿用**——求解轨迹是烧了配额换来的，
# 换一套目录命名等于把它们孤儿化。文件不存在就不出现——注册表不再假装它们存在。
LEGACY_PAPERS: dict[str, dict] = {
    "english": {"exam": "data/exam.json", "traces": "data/traces",
                "specs": ("english",), "has_latex": False},
    "math": {"exam": "data/exam_math.json", "traces": "data/traces_math",
             "specs": ("math", "math_multi"), "has_latex": True},
    "xc_fs": {"exam": "data/exam_xc_副省级.json", "traces": "data/traces_xc_fs",
              "specs": ("xingce",), "has_latex": False},
    "xc_ds": {"exam": "data/exam_xc_地市类.json", "traces": "data/traces_xc_ds",
              "specs": ("xingce",), "has_latex": False},
    "xc_zf": {"exam": "data/exam_xc_行政执法类.json", "traces": "data/traces_xc_zf",
              "specs": ("xingce",), "has_latex": False},
}


def _read_meta(path: Path) -> tuple[dict | None, str | None]:
    """只取题库 meta。文件损坏或版本不对时返回 (None, 原因)——
    清单里照样露面并写明坏在哪，不静默消失、也不假装能打开。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"不是合法 JSON：{type(e).__name__}: {e}"
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return None, "缺少 meta 对象，不是 v2 题库信封"
    version = meta.get("bank_version")
    if version is None:
        version = 1
    if version != 2:
        return None, (f"bank_version={version}，当前代码只支持 v2——"
                      f"请按 templates/ 的模板重新制作并上传")
    return meta, None


def _entry(pid: str, exam: Path, specs: tuple[str, ...], has_latex: bool,
           meta: dict | None, load_error: str | None,
           traces: Path | None = None) -> dict:
    return {
        "id": pid,
        "exam": exam,
        "traces": traces or (ROOT / "data" / f"traces_{pid}"),
        "specs": specs,
        "has_latex": has_latex,
        "meta": meta,
        "load_error": load_error,
    }


def discover() -> dict[str, dict]:
    """扫描 data/banks/*.json + 旧版迁移路径，返回 {id: 试卷配置}。

    每次都重扫磁盘：题库是外部文件，上传/删除要立刻生效，缓存反而会造成
    「界面显示的题库清单和磁盘不一致」这种更糟的静默。
    """
    out: dict[str, dict] = {}
    if BANKS_DIR.exists():
        for f in sorted(BANKS_DIR.glob("*.json")):
            pid = f.stem
            meta, err = _read_meta(f)
            specs: tuple[str, ...] = ()
            has_latex = False
            if err is None:
                raw_specs = meta.get("specs")
                if (isinstance(raw_specs, list) and raw_specs
                        and all(isinstance(s, str) and s.strip() for s in raw_specs)):
                    specs = tuple(str(s).strip() for s in raw_specs)
                else:
                    err = "meta.specs 缺失或不合法（需为非空 spec 名数组）"
                has_latex = bool(meta.get("has_latex"))
            out[pid] = _entry(pid, f, specs, has_latex, meta, err)
    for pid, cfg in LEGACY_PAPERS.items():
        f = ROOT / cfg["exam"]
        if pid in out or not f.exists():
            continue
        meta, err = _read_meta(f)
        specs = tuple(cfg["specs"])
        if err is None and isinstance(meta.get("specs"), list) and meta.get("specs"):
            specs = tuple(str(s).strip() for s in meta["specs"])
        out[pid] = _entry(pid, f, specs, bool(cfg["has_latex"]), meta, err,
                          traces=ROOT / cfg["traces"])
    return out


def get(paper_id: str | None) -> dict:
    """按 id 取试卷。未知 id 直接报错，**不做静默回退**——
    否则已删除或拼错的 id 会悄悄拿到默认卷的数据，界面显示的是另一份试卷。"""
    registry = discover()
    if paper_id in (None, ""):
        if not registry:
            raise KeyError(paper_id)
        paper_id = sorted(registry)[0]
    if paper_id not in registry:
        raise KeyError(paper_id)
    return registry[paper_id]


def default_id() -> str | None:
    """默认卷 = 字序第一份；没有任何题库时为 None（界面给出上传指引而不是报错）。"""
    registry = discover()
    return sorted(registry)[0] if registry else None


def listing() -> dict:
    """给前端的 ``/api/papers`` 载荷：试卷清单 + 轨迹版本。

    ``trace_version`` 必须这里给而不是前端写死：界面用它把旧代码产出的轨迹标成
    "过旧"，版本对不上就会把可信的判定读成不可信的。前端仍保留一个兜底常量，
    只在接口整体不可用时生效，并在界面上明说是兜底值。

    题库读取失败的条目 exam_ready=False 且带 error，界面上明说坏在哪，
    而不是从清单里消失。
    """
    papers_out = []
    for p in discover().values():
        meta = p["meta"] or {}
        solved = len([f for f in p["traces"].glob("*.json")
                      if not f.name.startswith("_")]) if p["traces"].exists() else 0
        papers_out.append({
            "id": p["id"],
            "subject": str(meta.get("subject") or p["id"]),
            "title": str(meta.get("title") or meta.get("subject") or p["id"]),
            "solved": solved,
            "has_latex": p["has_latex"],
            "exam_ready": p["load_error"] is None and bool(p["specs"]),
            "error": p["load_error"],
        })
    return {"papers": papers_out, "trace_version": trace_version()}


def trace_version() -> int:
    """全部 spec 文件里最高的轨迹版本。版本属于代码：注册了新 spec 却不提升，
    读到的还是旧语义。没有任何 spec 文件时退回 2（与本仓库随附的 spec 一致）。"""
    from judge import spec as spec_mod

    versions = [s.trace_version() for s in spec_mod.load_all().values()]
    return max(versions) if versions else 2
