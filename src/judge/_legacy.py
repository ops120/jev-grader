"""S0（重构前）三份 compose 的冻结副本——只给对拍门禁 ``tests/golden_compose.py`` 当参照系。

为什么要有这个"重复代码"
------------------------
新组合策略必须被证明与旧实现**逐字节等价**，而唯一可信的参照系就是改动前的实现本身。
一旦旧的 ``compose`` 被就地改写或删除，等价性就再也无法重跑验证——所以这里保留一份
重构前逐字抄出的副本，并且由 ``tests/test_judge_contract.py`` 用 AST 比对守住它：
它与 ``orchestrators.py`` 里那份同名 compose 必须继续逐字相等，
只要有人试图"顺手修一下"参照系，就得同时改两处，而那是门禁要拦的事。

本文件的函数**不接受任何行为修正**。发现 bug 请改 ``strategies.py`` 并同步更新门禁基线，
不要动这里。生产代码不得 import 本模块。
"""
from __future__ import annotations

from typing import Any

# 轨迹格式版本：界面据此识别用旧代码生成的轨迹，避免陈旧字段被静默误读
TRACE_VERSION = 2


# ==========================================================================
# questions.compose —— 英语卷（行测卷亦复用它）
# ==========================================================================
def compose(item: dict, data: dict, legends: dict) -> dict:
    """把模型返回的原子判断组合成轨迹；最终答案与分歧标记由代码决定。"""
    ans = data.get("answers", {})
    letters = sorted(item.get("options") or {})

    def choice(key: str) -> dict | None:
        a = ans.get(key)
        if not a or a.get("type") != "choice":
            return None
        return {
            "choice": a.get("choice"),
            "confidence": a.get("confidence"),
            "probabilities": a.get("probabilities") or {},
        }

    support = {}
    for L in letters:
        a = ans.get(f"support_{L}")
        if a and a.get("type") == "noul":
            support[L] = a.get("noul")

    best = choice("best")
    strategy = choice("strategy")
    evidence = choice("evidence")
    distractor = choice("distractor")

    answer = best["choice"] if best else None
    ranked = sorted((v, k) for k, v in support.items() if v is not None)
    top_support = ranked[-1][1] if ranked else None
    support_max = ranked[-1][0] if ranked else None
    agreement = bool(answer and top_support and answer == top_support)
    # 逐选项支持度整体偏低时，说明没有哪个选项真正得到材料支持（常见于材料缺失的题），
    # 此时"两路一致"只是两个都很弱的判断恰好指向同一项，不能当作可靠依据。
    weak = support_max is not None and support_max < 0.5

    steps: list[dict] = []
    if strategy:
        steps.append({
            "key": "strategy", "label": "题型识别", "type": "choice",
            "choice": strategy["choice"], "confidence": strategy["confidence"],
            "probabilities": strategy["probabilities"],
            "legend": legends["strategy"], "desc": legends["strategy"].get(strategy["choice"], ""),
        })
    if evidence and legends["evidence"]:
        steps.append({
            "key": "evidence", "label": "证据定位", "type": "choice",
            "choice": evidence["choice"], "confidence": evidence["confidence"],
            "probabilities": evidence["probabilities"],
            "legend": legends["evidence"],
            "desc": legends["evidence"].get(evidence["choice"], ""),
            "sentence": evidence["choice"],
        })
    if ranked:
        steps.append({
            "key": "support", "label": "逐选项支持度（每项一个独立判断）", "type": "group",
            "items": [{"key": L, "value": support.get(L), "text": legends["option"].get(L, "")}
                      for L in letters],
            "legend": legends["option"],
        })
    if best:
        steps.append({
            "key": "best", "label": "最终判定", "type": "choice",
            "choice": best["choice"], "confidence": best["confidence"],
            "probabilities": best["probabilities"], "legend": legends["option"],
            "desc": legends["option"].get(best["choice"], ""),
        })
    if distractor:
        # 易错选项不应等于正确答案；若模型仍选了它，由代码在其余选项中取最高概率者
        d_choice = distractor["choice"]
        if d_choice == answer:
            rest = {k: v for k, v in distractor["probabilities"].items() if k != answer}
            if rest:
                d_choice = max(rest, key=rest.get)
        steps.append({
            "key": "distractor", "label": "干扰项识别", "type": "choice",
            "choice": d_choice, "confidence": distractor["confidence"],
            "probabilities": distractor["probabilities"], "legend": legends["option"],
            "desc": legends["option"].get(d_choice, ""),
        })

    rule = ("答案取「最终判定」中概率最高的选项。"
            "若该选项在「逐选项支持度」中不是最高，则两者结论不一致，标记为分歧需人工复核。")
    if weak:
        top_pct = f"{support_max:.0%}" if support_max is not None else "—"
        rule += (f"本题逐选项支持度普遍很低（最高仅 {top_pct}），说明没有哪个选项真正得到材料支持，"
                 f"通常意味着题目依赖的材料不在文本内；此时不宜采信答案。")
    elif agreement:
        rule += "本题两路判断结论一致。"
    elif answer and top_support:
        rule += f"本题「最终判定」选 {answer}，而「逐选项支持度」最高的是 {top_support}，存在分歧。"

    return {
        "trace_version": TRACE_VERSION,
        "item_id": item["id"], "no": item["no"], "kind": item.get("kind", ""),
        "model": data.get("model"), "latency_ms": data.get("_latency_ms"),
        "usage": data.get("usage"), "steps": steps,
        "answer": answer,
        "confidence": best["confidence"] if best else None,
        "support": support,
        "top_support": top_support,
        "support_max": support_max,
        "weak": weak,
        "agreement": agreement,
        "composition_rule": rule,
        "legends": legends,
    }


# ==========================================================================
# questions_math.compose —— 数学卷（单选／多选两套形状）
# ==========================================================================
THRESHOLD = 0.5          # 多选题：Noul 概率达到该值即认为该选项入选
WEAK_BAND = (0.35, 0.65)  # 多选：全部选项都落在该区间说明判断无区分度


def _kinds(it: dict) -> tuple[bool, bool]:
    k = it.get("kind", "")
    return k.endswith("single"), k.endswith("multi")


def math_compose(item: dict, data: dict, legends: dict) -> dict:
    ans = data.get("answers", {})
    letters = sorted(item.get("options") or {})
    single, multi = _kinds(item)

    def choice(key: str) -> dict | None:
        a = ans.get(key)
        if not a or a.get("type") != "choice":
            return None
        return {"choice": a.get("choice"), "confidence": a.get("confidence"),
                "probabilities": a.get("probabilities") or {}}

    support = {L: (ans.get(f"opt_{L}") or {}).get("noul")
               for L in letters if (ans.get(f"opt_{L}") or {}).get("type") == "noul"}
    point, strategy, best, trap = choice("point"), choice("strategy"), choice("best"), choice("trap")
    hint = choice("count_hint")

    steps: list[dict] = []
    if point:
        steps.append({"key": "point", "label": "考点识别", "type": "choice",
                      "choice": point["choice"], "confidence": point["confidence"],
                      "probabilities": point["probabilities"], "legend": legends["point"],
                      "desc": legends["point"].get(point["choice"], "")})
    if strategy:
        steps.append({"key": "strategy", "label": "解法策略", "type": "choice",
                      "choice": strategy["choice"], "confidence": strategy["confidence"],
                      "probabilities": strategy["probabilities"], "legend": legends["strategy"],
                      "desc": legends["strategy"].get(strategy["choice"], "")})

    ranked = sorted((v, k) for k, v in support.items() if v is not None)
    support_max = ranked[-1][0] if ranked else None
    top_support = ranked[-1][1] if ranked else None

    if support:
        steps.append({
            "key": "support", "label": "逐选项判定（每项一个独立判断）", "type": "group",
            "items": [{"key": L, "value": support.get(L), "text": legends["option"].get(L, "")}
                      for L in letters],
            "legend": legends["option"],
        })

    if single:
        answer = best["choice"] if best else None
        agreement = bool(answer and top_support and answer == top_support)
        weak = support_max is not None and support_max < 0.5
        if best:
            steps.append({"key": "best", "label": "最终判定", "type": "choice",
                          "choice": best["choice"], "confidence": best["confidence"],
                          "probabilities": best["probabilities"], "legend": legends["option"],
                          "desc": legends["option"].get(best["choice"], "")})
        if trap:
            # 易错选项不应等于正确答案；若模型仍选了它，则由代码在其余选项中重新取最高概率者
            trap_choice = trap["choice"]
            if trap_choice == answer:
                rest = {k: v for k, v in trap["probabilities"].items() if k != answer}
                if rest:
                    trap_choice = max(rest, key=rest.get)
                    trap = {**trap, "choice": trap_choice,
                            "desc": legends["option"].get(trap_choice, "")}
            steps.append({"key": "trap", "label": "易错选项", "type": "choice",
                          "choice": trap_choice, "confidence": trap["confidence"],
                          "probabilities": trap["probabilities"], "legend": legends["option"],
                          "desc": legends["option"].get(trap_choice, "")})
        confidence = best["confidence"] if best else None
    else:
        # 多选题：代码按阈值把 Noul 概率折成选项集合
        chosen = [L for L in letters if (support.get(L) or 0) >= THRESHOLD]
        answer = "".join(chosen) if chosen else None
        decided = [(1 - abs((support[L] or 0.5) - 0.5) * 2) for L in letters if L in support]
        confidence = round(1 - sum(decided) / len(decided), 3) if decided else None
        weak = bool(support) and all(WEAK_BAND[0] <= (support.get(L) or 0) <= WEAK_BAND[1]
                                     for L in letters)
        agreement = None
        rows = [{"key": L, "value": support.get(L), "text": legends["option"].get(L, ""),
                 "pass": (support.get(L) or 0) >= THRESHOLD} for L in letters]
        steps.append({
            "key": "decide", "label": f"阈值判定（代码决定：概率 ≥ {THRESHOLD} 入选）",
            "type": "group", "items": rows, "legend": legends["option"],
            "summary": f"入选 {answer or '—'}", "threshold": THRESHOLD,
        })
        if hint:
            steps.append({"key": "count_hint", "label": "正确选项个数（仅作参考）", "type": "choice",
                          "choice": hint["choice"], "confidence": hint["confidence"],
                          "probabilities": hint["probabilities"], "legend": {},
                          "desc": f"模型认为最可能有 {hint['choice']} 个正确选项"})

    rule = ("单选题：答案取「最终判定」概率最高的选项；"
            "若它与「逐选项判定」中概率最高的选项不一致，则标记为分歧。" if single else
            f"多选题：答案不是单个选项而是一个集合，故不使用选择判定，"
            f"改为「逐选项判定」中概率 ≥ {THRESHOLD} 的选项全部入选（阈值由代码决定）。")
    if weak:
        top = f"{support_max:.0%}" if support_max is not None else "—"
        rule += (f" 本题最高概率仅 {top}，" if single else f" 本题各选项概率集中在 0.5 附近，")
        rule += "说明模型对哪个选项都没把握——常见于题目依赖图形或条件超出其能力范围的情况，不宜采信。"
    elif single and agreement:
        rule += " 本题两路判断结论一致。"
    elif single and answer and top_support:
        rule += f" 本题「最终判定」选 {answer}，而「逐选项判定」概率最高的是 {top_support}，存在分歧。"

    return {
        "trace_version": 2, "item_id": item["id"], "no": item["no"], "kind": item["kind"],
        "subject": "数学",
        "model": data.get("model"), "latency_ms": data.get("_latency_ms"),
        "usage": data.get("usage"), "steps": steps,
        "answer": answer, "confidence": confidence,
        "support": support, "top_support": top_support, "support_max": support_max,
        "weak": weak, "agreement": agreement,
        "composition_rule": rule, "legends": legends,
    }
