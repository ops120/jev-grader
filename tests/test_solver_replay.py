"""零配额门禁 4：不联网重放整条后端路径 build -> ask -> compose -> 落盘。

前三道门禁各自证明一段：build 载荷未变、与历史请求未变、compose 与 S0 未变。
这一道把它们串起来跑**生产代码** ``solver.solve_one``：拿当年落盘的真实请求喂一个假
JEV 客户端，看它组合出的轨迹是否就是历史轨迹本身。它测的是接线——spec 选路、并发签名、
缓存判断、轨迹字段、落盘——S3 改的正是这些接线。

假响应从历史轨迹反推：choice 步取回 choice/confidence/probabilities，
group 步按选项字母取回 noul 值。因此比对排除的只有 solver 自己追加的运行期字段
（wall_ms / n_questions / request）与两个已知的新增可选字段。

跑法：python tests/test_solver_replay.py     退出码 0 = 全卷一致（或差异全部在台账内）

题库与轨迹都在本机（不入库）；缺失时跳过并如实说明跳过了多少。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import papers  # noqa: E402
import solver  # noqa: E402

# solver 在 compose 之后追加的运行期字段：不是判定内容，重放无从复现
RUNTIME_FIELDS = {"wall_ms", "n_questions", "request"}
# 本轮新增的可选字段（旧轨迹里没有），界面缺失时会标注"按推断"
ADDITIONAL_FIELDS = {"answer_shape", "reveal"}

# 已知缺陷台账：这 10 条英语轨迹的「干扰项」一步与今天的实现不可能对得上。
# 它们的 distractor.choice 恰好等于该题自己的 answer，而现行实现（以及重构前的 S0 实现）
# 在这种情形下必然改判为其余选项中概率最高者——说明这批数据写于"改判"逻辑上线之前，
# 而原始判定没有被持久化，已经找不回来了。
# 台账只赦免这一处差异：题号不在名单里、或缺陷签名不符、或差异落在别的步骤/字段，硬失败。
LEGACY_REPICK_GAP: dict[str, tuple[str, ...]] = {
    "english": ("Q22", "Q28", "Q30", "Q31", "Q35", "Q41", "Q45", "Q46", "Q50", "Q51"),
}
REPICK_STEP_KEYS = {"distractor", "trap"}
REPICK_FIELDS = {"choice", "desc"}


def answers_from_trace(trace: dict, questions: dict) -> dict:
    """把历史轨迹反推成一份"模型回答"，形状与 JEV 返回体一致。"""
    letters: dict[str, Any] = {}
    answers: dict[str, Any] = {}
    for step in trace.get("steps", []):
        if step.get("type") == "choice":
            answers[step["key"]] = {"type": "choice", "choice": step.get("choice"),
                                    "confidence": step.get("confidence"),
                                    "probabilities": step.get("probabilities") or {}}
        elif step.get("type") == "group":
            for row in step.get("items") or []:
                letters[row["key"]] = row.get("value")
    for key, q in questions.items():
        if q.get("type") == "noul":
            answers[key] = {"type": "noul", "noul": letters.get(key.rsplit("_", 1)[-1]),
                            "confidence": 0.9, "probabilities": {}}
    return answers


def payload_key(state: dict, questions: dict) -> tuple[str, str]:
    return (json.dumps(state, sort_keys=True, ensure_ascii=False),
            json.dumps(questions, sort_keys=True, ensure_ascii=False))


class FakeJev:
    """只在 solve_one 构造的载荷与历史请求逐字节一致时才应答——不一致就是接线错了。"""

    def __init__(self, pending: dict[tuple[str, str], dict]):
        self.pending = pending

    async def ask(self, state: dict, questions: dict) -> dict:
        hit = self.pending.get(payload_key(state, questions))
        if hit is None:
            raise AssertionError(f"solve_one 构造的载荷与历史请求不一致：{sorted(questions)}")
        return hit


def strip(trace: dict) -> dict:
    out = {k: v for k, v in trace.items()
           if k not in RUNTIME_FIELDS and k not in ADDITIONAL_FIELDS}
    out["steps"] = [{k: v for k, v in s.items() if k not in ADDITIONAL_FIELDS}
                    for s in trace.get("steps", [])]
    return out


def diff_fields(old: dict, new: dict) -> list[tuple[str, str]]:
    """差异位置清单，用 (步骤键或 '<top>' , 字段名) 表示；步骤按 key 对齐而不是按下标。"""
    diffs: list[tuple[str, str]] = []
    old_steps = {s.get("key"): s for s in old.get("steps") or []}
    new_steps = {s.get("key"): s for s in new.get("steps") or []}
    for top in set(old) | set(new):
        if top == "steps":
            continue
        if json.dumps(old.get(top), sort_keys=True, ensure_ascii=False) != \
           json.dumps(new.get(top), sort_keys=True, ensure_ascii=False):
            diffs.append(("<top>", top))
    if list(old_steps) != list(new_steps):
        diffs.append(("steps", "序列"))
    for key in old_steps:
        a, b = old_steps.get(key) or {}, new_steps.get(key) or {}
        for f in set(a) | set(b):
            if json.dumps(a.get(f), sort_keys=True, ensure_ascii=False) != \
               json.dumps(b.get(f), sort_keys=True, ensure_ascii=False):
                diffs.append((str(key), f))
    return sorted(diffs)


def explain(pid: str, rid: str, stored: dict, diffs: list[tuple[str, str]]) -> str | None:
    """差异是否恰好就是台账描述的那一个？是则返回人话说明，否则 None（= 硬失败）。"""
    if rid not in LEGACY_REPICK_GAP.get(pid, ()):
        return None
    if not diffs or any(step not in REPICK_STEP_KEYS or field not in REPICK_FIELDS
                        for step, field in diffs):
        return None
    hit = next((s for s in stored.get("steps") or []
                if s.get("key") in REPICK_STEP_KEYS), None)
    if not hit or hit.get("choice") != stored.get("answer"):
        return None                      # 缺陷签名已不成立：这不是那个已知问题
    return (f"存的是改判上线前的原值 {hit['choice']}（恰等于本题答案），"
            f"现行实现会改判为其余选项中概率最高者")


async def run_paper(cfg: dict) -> tuple[int, int, int, list[str], list[str]]:
    """返回 (重放题数, 一致题数, 台账内差异题数, 硬失败明细, 台账明细)。"""
    exam = json.loads(cfg["exam"].read_text(encoding="utf-8"))
    passages = {p["id"]: p for p in exam.get("passages", [])}
    pending: dict[tuple[str, str], dict] = {}
    stored: dict[str, dict] = {}
    for item in exam["items"]:
        trace_file = cfg["traces"] / f"{item['id']}.json"
        if not item.get("answerable") or not trace_file.exists():
            continue
        trace = json.loads(trace_file.read_text(encoding="utf-8"))
        req = trace.get("request")
        if not req or not trace.get("steps"):
            continue
        questions = req["questions"]
        pending[payload_key(req["state"], questions)] = {
            "model": trace.get("model"), "_latency_ms": trace.get("latency_ms"),
            "usage": trace.get("usage"), "answers": answers_from_trace(trace, questions)}
        stored[item["id"]] = trace
    if not stored:
        return 0, 0, 0, [], []

    client = FakeJev(pending)
    sem = asyncio.Semaphore(4)
    bad: list[str] = []
    known: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:      # 绝不往真实轨迹目录写
        work = dict(cfg)
        work["traces"] = Path(tmp)
        results = await asyncio.gather(*(
            solver.solve_one(client, it, passages.get(it.get("passage_id")), sem, False, work)
            for it in exam["items"] if it["id"] in stored))
        for r in results:
            rid = r["id"]
            if r["status"] != "ok":
                bad.append(f"{rid}: status={r['status']} {r.get('detail', '')}")
                continue
            new = json.loads((work["traces"] / f"{rid}.json").read_text(encoding="utf-8"))
            diffs = diff_fields(strip(stored[rid]), strip(new))
            if not diffs:
                continue
            why = explain(cfg["id"], rid, strip(stored[rid]), diffs)
            line = f"{rid}: " + ", ".join(f"{s}.{f}" for s, f in diffs)
            (known if why else bad).append(f"{line} —— {why}" if why else line)
    n = len(stored)
    return n, n - len(bad) - len(known), len(known), bad, known


def main() -> int:
    hard: list[str] = []
    total = same = known_total = 0
    ledger_ids: list[str] = []
    for pid, cfg in papers.PAPERS.items():
        if not cfg["exam"].exists() or not cfg["traces"].exists():
            print(f"  SKIP  {pid:<8} 题库或轨迹缺失，这道门禁未生效")
            continue
        n, ok, known, bad, detail = asyncio.run(run_paper(cfg))
        if not n:
            print(f"  ----  {pid:<8} 轨迹里没有 request 字段，无可重放载荷")
            continue
        total += n
        same += ok
        known_total += known
        ledger_ids += detail
        hard += [f"{pid}/{m}" for m in bad]
        note = f"（含台账 {known}）" if known else ""
        print(f"  {'OK  ' if not bad else 'FAIL'}  {pid:<8} 重放 {n} 题："
              f"{ok} 题逐字段等于历史轨迹{note}")

    if ledger_ids:
        print("\n台账内差异（数据比代码旧，不是接线错）：")
        for line in ledger_ids:
            print("  -", line)
    if hard:
        print("\nsolve_one 重放不一致（不在台账内，硬失败）：")
        for f in hard:
            print("  -", f)
        return 1
    if not total:
        print("\n跳过：本机没有可重放的题库与轨迹，这道门禁未生效")
        return 0
    print(f"\n通过：{total} 题走完 build -> ask -> compose -> 落盘 全路径；"
          f"{same} 题逐字段等于历史轨迹，{known_total} 题差异全部在台账内。"
          f"要让这 {known_total} 题也能逐字节复现，需要把模型原值 raw_answers 一并持久化")
    return 0


if __name__ == "__main__":
    sys.exit(main())
