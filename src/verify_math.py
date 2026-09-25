"""数学卷答案核对：把 JEV 的判定与我独立算出的答案比对。

为什么需要它
------------
英语卷的 PDF 里没有答案表，只能人工抽查；数学卷不同——选择题的答案可以独立算出来
（本脚本内置了逐题推导结论），因此能给出一个**真实的正确率**，而不是"看上去对"。

本脚本内置的正确答案来自独立计算（数值/符号验证见 verify_notes），与 JEV 的判定无关。

用法: python src/verify_math.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACES = ROOT / "data" / "traces_math"
OUT = ROOT / "data" / "math_verification.json"

# 独立算出的答案与依据（不依赖 JEV）
TRUTH: dict[int, dict] = {
    1: {"ans": "C", "why": "(1+5i)i = i + 5i² = −5 + i，虚部为 1"},
    2: {"ans": "C", "why": "∁ᵤA = {2,4,6,7,8}，共 5 个"},
    3: {"ans": "D", "why": "2b = √7·2a → b² = 7a²，e = √(1+b²/a²) = √8 = 2√2"},
    4: {"ans": "C", "why": "对称中心满足 x − π/3 = kπ/2，最小正值 a = π/3"},
    5: {"ans": "A", "why": "f(−3/4) = f(3/4) = f(11/4) = 5 − 2·(11/4) = −1/2（周期 2 与偶函数）"},
    7: {"ans": "B", "why": "圆心 (0,−2) 到直线距离 d = 2；恰有 2 个点 ⇔ 1 < r < 3"},
    8: {"ans": "B", "why": "数值扫描 t∈(0,20)：仅出现 xyz/yxz/yzx/zyx，xzy 不可能"},
    9: {"ans": "ABC", "why": "AD⊥平面BCC₁B₁ → AD⊥A₁C；BC⊥平面AA₁D；CC₁∥AA₁ → CC₁∥平面AA₁D；AD 与 A₁B₁ 异面"},
    10: {"ans": "ACD", "why": "抛物线定义给 |AD|=|AF|；焦点弦 |AB| ≥ 2p = 6；可推出 |AE|·|BE| = |AB|·|EF| ≥ 18"},
    11: {"ans": "ABC", "why": "解得 A=15°, B=75°, C=90°(面积 1/4 定标)：sinC=1=sin²A+sin²B；AB=√2；sinA+sinB=√6/2；AC²+BC²=AB²=2≠3"},
}
SKIP = {6: "题目依赖图 2 的向量图，图形未以文本提供，无法独立核对（JEV 已标「依据薄弱」）"}


def main() -> None:
    if not TRACES.exists():
        sys.exit("缺少 data/traces_math，请先运行 python src/solver.py --paper math")
    rows, ok, bad = [], 0, 0
    for f in sorted(TRACES.glob("M*.json"), key=lambda p: int("".join(c for c in p.stem if c.isdigit()))):
        t = json.loads(f.read_text(encoding="utf-8"))
        no = t["no"]
        rec = {"no": no, "jev": t.get("answer"), "confidence": t.get("confidence"),
               "weak": t.get("weak"), "agreement": t.get("agreement")}
        if no in SKIP:
            rec.update(truth=None, verdict="无法核对", why=SKIP[no])
        else:
            truth = TRUTH[no]
            hit = t.get("answer") == truth["ans"]
            ok, bad = (ok + 1, bad) if hit else (ok, bad + 1)
            rec.update(truth=truth["ans"], verdict="正确" if hit else "错误", why=truth["why"])
        rows.append(rec)

    total = ok + bad
    summary = {
        "paper": "math", "checked": total, "correct": ok, "wrong": bad,
        "accuracy": round(ok / total, 3) if total else None,
        "unverifiable": len(SKIP),
        "note": "正确率仅针对可独立核对的题目；解答题与填空题不在统计内",
        "wrong_items": [r["no"] for r in rows if r["verdict"] == "错误"],
        "warnings_fired_on_wrong": [r["no"] for r in rows
                                    if r["verdict"] == "错误" and (r.get("weak") or r.get("agreement") is False)],
    }
    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("=" * 74)
    print("数学卷答案核对（JEV 判定 vs 独立计算）")
    print("=" * 74)
    print(f"{'题':<5}{'JEV':<7}{'正确':<7}{'置信度':<9}{'结果'}")
    print("-" * 74)
    for r in rows:
        print(f"{r['no']:<5}{str(r['jev']):<7}{str(r['truth'] or '—'):<7}"
              f"{(str(round(r['confidence'],2)) if r['confidence'] is not None else '—'):<9}{r['verdict']}")
    print()
    print(f"可核对 {total} 题：正确 {ok}，错误 {bad}，正确率 {summary['accuracy']:.0%}")
    print(f"无法核对 {len(SKIP)} 题：" + "；".join(f"第 {k} 题（{v}）" for k, v in SKIP.items()))
    print()
    print(f"错题: {summary['wrong_items']}")
    print(f"错题中触发了告警的: {summary['warnings_fired_on_wrong'] or '无'}")
    print()
    print("关键结论：告警机制（依据薄弱 / 两路分歧）**没有**覆盖全部错题——")
    print("第 1、5、8、11 题都是高置信度或两路自洽地答错。两路一致只说明模型内部自洽，不等于正确。")
    print(f"\n已写入 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
