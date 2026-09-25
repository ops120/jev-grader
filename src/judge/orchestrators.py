"""三份编排主体的逐字副本（重构前 S0 的实现）。

为什么三个实现放在同一个模块
----------------------------
它们的差异全部是**表格与措辞**，而同名辅助函数在不同卷里语义并不相同：
``_clip``／``_blank_context`` 只有英语与行测用，``_kinds`` 只有数学用。
分成三个文件就要给它们改名或做跨文件 re-export，两种都只是在制造噪音；
放在一处反而由段落标题直接说明"哪段属于哪份卷子"。

本模块的不变量：**字符级保持 S0**。S3 起生产路径已切到 ``engine`` + ``spec``，
本文件只作为重构前实现的一份副本留在树里，供 ``tests/golden_compose.py`` 当参照系。
不得增删改任何细节——
``_legacy.py`` 里有同一份 compose 的第二份副本，两者由 ``tests/test_judge_contract.py``
逐字对拍：想改参照系必须同时改两处，而那正是这些门禁要拦住的事。
"""
from __future__ import annotations

import re
from typing import Any

# ==========================================================================
# 英语卷（全国卷）——同时也是行测卷的地基
# ==========================================================================

# 轨迹格式版本：界面据此识别用旧代码生成的轨迹，避免陈旧字段被静默误读
TRACE_VERSION = 2

STRATEGY_MCQ = {
    "detail": "细节理解：答案直接对应文中某一处事实或数据",
    "inference": "推理判断：需依据文中信息作一步推断，文中无现成表述",
    "main_idea": "主旨大意：概括段落或全文的中心、标题、写作目的",
    "vocab": "词义猜测：依据上下文判断某个词或短语的含义",
    "attitude": "观点态度：判断作者或文中人物的态度、评价",
    "structure": "写作手法/篇章结构：如举例、引用、对比、数据说明",
}
STRATEGY_CLOZE = {
    "collocation": "固定搭配与惯用法",
    "context_logic": "上下文逻辑：转折、因果、并列、递进",
    "semantics": "词义辨析：近义词在语境中的取舍",
    "grammar": "语法结构：时态、语态、非谓语、词性",
}
STRATEGY_SEVEN = {
    "topic_sentence": "主题句：概括该段或全文要点",
    "transition": "承上启下：衔接前后段的过渡句",
    "detail_support": "细节支撑：补充说明前文的具体信息",
    "pronoun_reference": "指代衔接：与前后文人称/指代呼应",
    "conclusion": "总结收束：对前文的归纳或展望",
}


def _en_strategy_table(kind: str) -> dict[str, str]:
    return {"cloze": STRATEGY_CLOZE, "seven": STRATEGY_SEVEN}.get(kind, STRATEGY_MCQ)


def _en_clip(text: str, n: int = 240) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _en_blank_context(passage: dict, no: int) -> str:
    """取含该题空的句子及前后各一句，作为填空类题目的上下文。"""
    sents = passage["sentences"]
    idx = next((i for i, s in enumerate(sents) if f"[{no}]" in s["text"]), None)
    if idx is None:
        return ""
    lo, hi = max(0, idx - 1), min(len(sents), idx + 2)
    return " ".join(s["text"] for s in sents[lo:hi])


def en_build(item: dict, passage: dict | None) -> tuple[Any, dict, dict]:
    """返回 (state, questions, legends)。legends 供界面把判定键翻成中文。"""
    options: dict[str, str] = item.get("options") or {}
    letters = sorted(options)
    kind = item.get("kind", "")
    strategy_table = _en_strategy_table(kind)
    sentences = (passage or {}).get("sentences") or []

    if kind in ("cloze", "seven"):
        question = item["stem"]
        context = _en_blank_context(passage, item["no"]) if passage else ""
        passage_text = " ".join(s["text"] for s in sentences)
    else:
        question = item["stem"]
        context = ""
        passage_text = " ".join(s["text"] for s in sentences)

    state: dict[str, Any] = {
        "passage": passage_text,
        "question": question,
    }
    if context:
        state["blank_context"] = context
    state["options"] = options

    if kind == "seven":
        best_instr = ("依据 `state.blank_context` 与 `state.passage`，"
                      "选出填入该空后使全文最连贯的选项。注意有两项为多余选项，均无法与该空衔接。")
        support_instr = ("选项 {L}（`state.options.{L}`）填入该空后，是否与上下文衔接自然、语义连贯？")
        support_true = "衔接自然：指代、逻辑与话题均与上下文一致"
        support_false = "衔接生硬：话题跳脱、指代不明，或本身是多余选项"
        distractor_instr = ("哪个选项最具干扰性——看起来与上下文相关，"
                            "但填入该空会造成逻辑或指代问题？")
    elif kind == "cloze":
        best_instr = "依据 `state.blank_context` 与 `state.passage`，选出最适合该空的选项。"
        support_instr = ("选项 {L}（`state.options.{L}`）填入该空后，语义、搭配与上下文逻辑是否恰当？")
        support_true = "搭配自然、逻辑通顺，符合上下文语气"
        support_false = "搭配不当、语义矛盾，或与上下文逻辑不符"
        distractor_instr = "哪个选项最具干扰性——词形或词义相近，但填入后搭配或逻辑不妥？"
    else:
        best_instr = "依据 `state.passage`，选出 `state.question` 的最佳答案。"
        support_instr = "依据 `state.passage`，选项 {L}（`state.options.{L}`）的内容是否得到原文支持？"
        support_true = "原文有明确依据，或可由原文直接推出"
        support_false = "原文无依据、与原文矛盾，或属于过度推断"
        distractor_instr = "哪个选项最具干扰性——看似正确，实则与 `state.passage` 不符或偷换了表述？"

    questions: dict[str, Any] = {
        "strategy": {
            "type": "choice",
            "instructions": "结合 `state.question` 与 `state.passage`，判断这道题考查的核心能力属于哪一类。",
            "criteria": strategy_table,
        },
    }
    # 没有候选句就不下发该判断：Choice 至少需要 1 个选项，否则接口会拒绝整次请求
    if sentences:
        questions["evidence"] = {
            "type": "choice",
            "instructions": {
                "task": "哪一句是该题答案最直接的依据？只依据 `state.passage` 判断。",
                "note": "若某句同时支持多个选项，选择对正确选项支持最直接、最不可替代的那一句。",
                "question": "state.question",
            },
            "criteria": {s["no"]: _en_clip(s["text"]) for s in sentences},
        }
    questions.update({
        "best": {
            "type": "choice",
            "instructions": best_instr,
            "criteria": options,
        },
        "distractor": {
            "type": "choice",
            "instructions": distractor_instr,
            "criteria": options,
        },
    })
    for L in letters:
        questions[f"support_{L}"] = {
            "type": "noul",
            "instructions": support_instr.format(L=L),
            "criteria": {"true": support_true, "false": support_false},
        }

    legends = {
        "strategy": strategy_table,
        "evidence": {s["no"]: _en_clip(s["text"], 320) for s in sentences},
        "option": options,
    }
    return state, questions, legends


def en_compose(item: dict, data: dict, legends: dict) -> dict:
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
# 数学卷（新Ⅰ卷）——公式型题目，无篇章可定位
# ==========================================================================

# 高中数学考点（新高考范围）
POINTS = {
    "set_logic": "集合与常用逻辑用语",
    "complex": "复数",
    "function": "函数与导数",
    "trig": "三角函数与解三角形",
    "sequence": "数列",
    "solid": "立体几何",
    "analytic": "平面解析几何",
    "prob_stat": "概率与统计",
    "vector": "平面向量与空间向量",
    "inequality": "不等式",
}
STRATEGIES = {
    "direct": "直接计算：按定义或公式逐步算出结果",
    "verify": "逐项验证：把选项代回题设检验",
    "special": "特殊值/特殊情形代入，排除不可能项",
    "graph": "数形结合：借助图象或几何直观",
    "equiv": "等价转化：把条件变形为熟悉的形式",
    "counter": "构造反例或反证",
}
THRESHOLD = 0.5          # 多选题：Noul 概率达到该值即认为该选项入选
WEAK_BAND = (0.35, 0.65)  # 多选：全部选项都落在该区间说明判断无区分度


def _math_kinds(it: dict) -> tuple[bool, bool]:
    k = it.get("kind", "")
    return k.endswith("single"), k.endswith("multi")


def math_build(item: dict, passage: dict | None = None) -> tuple[Any, dict, dict]:
    options: dict[str, str] = item.get("options") or {}
    letters = sorted(options)
    single, multi = _math_kinds(item)

    state: dict[str, Any] = {
        "subject": "数学",
        "question": item["stem"],
        "options": options,
    }
    if item.get("subs"):
        state["sub_questions"] = item["subs"]
    if item.get("table"):
        state["table"] = item["table"]
    if item.get("crit_table"):
        state["critical_values"] = item["crit_table"]
    if item.get("figures"):
        # 题图无法作为文本提供，必须如实告知，避免模型凭空假设图形
        state["figures_not_provided"] = item["figures"]
    state["answer_type"] = "多选（可能有多个正确选项）" if multi else "单选（只有一个正确选项）"

    questions: dict[str, Any] = {
        "point": {
            "type": "choice",
            "instructions": "这道题主要考查 `state.question` 的哪个知识板块？",
            "criteria": POINTS,
        },
        "strategy": {
            "type": "choice",
            "instructions": "解决 `state.question` 最有效的方法是哪一种？",
            "criteria": STRATEGIES,
        },
    }
    if single:
        questions["best"] = {
            "type": "choice",
            "instructions": "依据 `state.question` 的条件逐步计算，选出唯一正确的选项。",
            "criteria": options,
        }
        questions["trap"] = {
            "type": "choice",
            "instructions": "哪个选项**不是正确选项**，却最可能因计算或概念失误被误选？",
            "criteria": options,
        }
        for L in letters:
            questions[f"opt_{L}"] = {
                "type": "noul",
                "instructions": f"选项 {L}（`state.options.{L}`）是否为 `state.question` 的正确答案？",
                "criteria": {"true": "经计算或推理，该选项正确", "false": "该选项错误或不满足题设"},
            }
    else:
        for L in letters:
            questions[f"opt_{L}"] = {
                "type": "noul",
                "instructions": f"选项 {L}（`state.options.{L}`）这个结论本身是否成立？",
                "criteria": {"true": "经推理或计算，该结论成立", "false": "该结论不成立或无法推出"},
            }
        questions["count_hint"] = {
            "type": "choice",
            "instructions": "本题的正确选项最可能共有几个？（仅用于交叉参考）",
            "criteria": {str(k): f"{k} 个正确选项" for k in (1, 2, 3, 4)},
        }

    legends = {"point": POINTS, "strategy": STRATEGIES, "option": options}
    return state, questions, legends


def math_compose(item: dict, data: dict, legends: dict) -> dict:
    ans = data.get("answers", {})
    letters = sorted(item.get("options") or {})
    single, multi = _math_kinds(item)

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


# ==========================================================================
# 行测卷——复用上方英语卷骨架，只换表格与包装材料
# ==========================================================================

CN_STRATEGY = {
    "vocab_fill": "选词填空：辨析近义词，看搭配与语境",
    "sentence": "语句表达：语病、歧义、语气与结构",
    "main_idea": "主旨概括：概括文段中心或作者意图",
    "detail": "细节理解：定位文中具体信息",
    "infer": "推断判断：依据文中信息作合理推断",
    "logic": "逻辑判断：推理规则的运用",
    "definition": "定义判断：严格依据给出的定义",
    "analogy": "类比推理：词项间逻辑关系",
    "number": "数字推理：数列规律",
    "math": "数学运算：列式与计算",
    "data": "资料分析：从图表或数据中提取与计算",
    "common_sense": "常识判断：经济金融法律等常识",
    "english": "英语单选：词汇、语法与搭配",
}

CN_STRATEGY_HINT = [
    (re.compile(r"选词填空"), "vocab_fill"),
    (re.compile(r"语句表达|病句|歧义"), "sentence"),
    (re.compile(r"逻辑判断|逻辑推理"), "logic"),
    (re.compile(r"定义判断"), "definition"),
    (re.compile(r"类比推理"), "analogy"),
    (re.compile(r"数字推理"), "number"),
    (re.compile(r"数学运算"), "math"),
    (re.compile(r"资料分析"), "data"),
    (re.compile(r"常识|金融|经济|法律"), "common_sense"),
    (re.compile(r"英语|English|单项选择", re.I), "english"),
]


def _kind_of(item: dict) -> str:
    sec = f"{item.get('section_name', '')}{item.get('kind', '')}"
    if "选词填空" in sec:
        return "cloze"          # 填空类：问"填入后是否合适"
    return "mcq"


def _cn_sentences(text: str) -> list[dict]:
    parts = re.split(r"(?<=[。！？；.!?;])\s*", text or "")
    return [{"no": f"S{i+1}", "text": p.strip()}
            for i, p in enumerate(parts) if p.strip()]


def cn_build(item: dict, passage: dict | None = None) -> tuple[Any, dict, dict]:
    it = dict(item)
    it["kind"] = _kind_of(item)
    # 把材料包装成"篇章"结构，使「证据定位」可用
    mat = (item.get("material") or "").strip()
    # 有些题的材料只是"请开始答题："这类版式文字，不能当材料用；此时改用题干自身作句子来源，
    # 否则「证据定位」会没有候选句可判断（JEV 要求 Choice 至少有一个选项）
    if len(mat) < 15:
        mat = item.get("stem", "")
    psg = {"sentences": _cn_sentences(mat)} if mat else (passage or None)
    if not it.get("passage_id"):
        it["passage_id"] = "__material__"

    state, questions, legends = en_build(it, psg)
    # 题型表换成行测分类；题干里能看出题型时给一个提示性的候选顺序
    questions["strategy"] = {
        "type": "choice",
        "instructions": "这道题属于行测中的哪一类？依据 `state.question` 与材料判断。",
        "criteria": CN_STRATEGY,
    }
    legends["strategy"] = CN_STRATEGY
    if psg and psg.get("sentences"):
        legends["evidence"] = {s["no"]: s["text"][:320] for s in psg["sentences"]}
    return state, questions, legends


cn_compose = en_compose


# ============================================================================
# 参照系注册表
# ============================================================================
# key 是重构前 papers.py 的 orchestrator 字段写过的模块名。S3 之后 papers 已改指 spec，
# 这张表不再被生产路径使用，只剩 tests/golden_compose.py 按它取 S0 的 build 做对照。
# 表本身随之冻结：新增试卷不需要动这里，只有要让门禁对照新科目时才加条目。
ORCHESTRATORS: dict[str, dict[str, object]] = {
    "questions": {"build": en_build, "compose": en_compose},
    "questions_math": {"build": math_build, "compose": math_compose},
    "questions_cn": {"build": cn_build, "compose": cn_compose},
}
