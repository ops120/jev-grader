"""三个具名组合策略：读取数值概率、产出答案与告警的唯一场所。

为什么只有三个，而且写死在 Python 里
------------------------------------
JEV 只给判断，不给结论。**把一堆概率折成一个答案、并决定什么时候报警**这件事必须可审计：
读代码就能知道 0.5 从哪来、分歧怎么判、模型自相矛盾时代码替不替它改。一旦把它变成
spec 里的表达式或"每步一个 JS 钩子"，这套工作台的核心卖点（答案能被质疑和复核）就没了。

所以本模块是封闭集合：
- ``single_choice_cross_check``   直接作答 × 逐选项支持度 两路交叉，含干扰项改写
- ``single_choice_with_trap``     同上骨架，但告警对象叫"易错选项"
- ``multi_threshold_set``         答案是**集合**：按阈值入选，无两路可比

第四个候选 ``score_only`` 刻意不做——368 条历史轨迹里没有任何一条用到 score 判定，
没有证据的配置项就是猜测。新增策略的唯一正当理由是新题型真的需要新的算术。

形状封闭性
----------
每个策略声明自己应产出的步骤序列（``step_shape``）。三条真实形状来自 368 条轨迹的穷举：
  ('strategy','evidence','support','best','distractor') ×357
  ('point','strategy','support','best','trap') ×8
  ('point','strategy','support','decide','count_hint') ×3
策略只会因为某个判断缺失而**少**输出几步，绝不会多出第四种；spec 若推出未知形状，
由 ``spec.validate`` 当场拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .spec import Spec, StepMeta

# 策略内部使用的文案槽位键，与 spec.RuleText 的属性名一一对应。
# 槽位文字在 spec.rules 里，选哪个槽位的分支结构在这里。
S_BASE = "base"
S_WEAK_SINGLE = "weak_single"
S_WEAK_SET = "weak_set"
S_AGREE = "agree"
S_DISAGREE = "disagree"


@dataclass(frozen=True)
class Strategy:
    name: str
    fn: Callable[[dict, dict, dict, Spec], dict]
    step_shape: tuple[str, ...]
    weak_threshold: float | None = None      # 单选：最高支持度低于此值即告警
    weak_band: tuple[float, float] | None = None   # 多选：全部落在带内即无区分度
    threshold: float | None = None           # 多选：达到即入选
    needs_rule_slots: tuple[str, ...] = ()   # 该策略会读哪些文案槽位（spec 必须提供）


def _pick(ans: dict, key: str) -> dict | None:
    """取一个 choice 型返回。类型不符或缺失一律当作"这个判断没有"。"""
    a = ans.get(key)
    if not a or a.get("type") != "choice":
        return None
    return {"choice": a.get("choice"), "confidence": a.get("confidence"),
            "probabilities": a.get("probabilities") or {}}


def _noul_map(ans: dict, prefix: str, letters: list[str]) -> dict[str, float | None]:
    """收集逐选项 noul。缺该判断则不进表；`noul: null` 保留为 None（两者行为不同）。"""
    out: dict[str, float | None] = {}
    for L in letters:
        a = ans.get(f"{prefix}{L}")
        if a and a.get("type") == "noul":
            out[L] = a.get("noul")
    return out


def _ranked(support: dict[str, float | None]) -> list[tuple[float, str]]:
    return sorted((v, k) for k, v in support.items() if v is not None)


def _choice_step(meta: StepMeta, c: dict, legend: dict, desc_from: str | None = None,
                 *, override_choice: str | None = None) -> dict:
    """把一个 choice 判断渲染成一步。override_choice 用于改写后的展示值。"""
    chosen = c["choice"] if override_choice is None else override_choice
    step = {
        "key": meta.key, "label": meta.label, "type": "choice",
        "choice": chosen, "confidence": c["confidence"],
        "probabilities": c["probabilities"], "legend": legend,
        "desc": legend.get(chosen, ""),
    }
    if meta.sentence:
        step["sentence"] = chosen
    if meta.reveal:
        step["reveal"] = True
    return step


def _group_step(meta: StepMeta, letters: list[str], support: dict, legend: dict,
                extra: Callable[[str], dict] | None = None) -> dict:
    items = []
    for L in letters:
        row = {"key": L, "value": support.get(L), "text": legend.get(L, "")}
        if extra:
            row.update(extra(L))
        items.append(row)
    step = {"key": meta.key, "label": meta.label, "type": "group",
            "items": items, "legend": legend}
    if meta.reveal:
        step["reveal"] = True
    return step


def _rule(spec: Spec, case: str, **fmt: Any) -> str:
    """按分支键取文案。分支结构是策略的事，文字是 spec 的事。"""
    text = getattr(spec.rules, case)
    return text.format(**fmt) if fmt else text


# ==========================================================================
# 1) single_choice_cross_check ← questions.compose
# ==========================================================================
def single_choice_cross_check(item: dict, data: dict, legends: dict, spec: Spec) -> dict:
    """英语／行测单选题。弱信号阈值 0.5 + 干扰项改写。"""
    ans = data.get("answers", {})
    letters = sorted(item.get("options") or {})
    steps: list[dict] = []

    strategy = _pick(ans, "strategy")
    evidence = _pick(ans, "evidence")
    best = _pick(ans, "best")
    distractor = _pick(ans, "distractor")
    support = _noul_map(ans, "support_", letters)

    if strategy:
        steps.append(_choice_step(spec.steps["strategy"], strategy, legends["strategy"]))
    if evidence and legends["evidence"]:
        steps.append(_choice_step(spec.steps["evidence"], evidence, legends["evidence"]))

    ranked = _ranked(support)
    top_support = ranked[-1][1] if ranked else None
    support_max = ranked[-1][0] if ranked else None
    if ranked:
        steps.append(_group_step(spec.steps["support"], letters, support, legends["option"]))

    answer = best["choice"] if best else None
    agreement = bool(answer and top_support and answer == top_support)
    thr = spec_weak_threshold(spec)
    weak = support_max is not None and support_max < thr
    if best:
        steps.append(_choice_step(spec.steps["best"], best, legends["option"]))
    if distractor:
        # 易错选项不应等于正确答案；若模型仍选了它，由代码在其余选项中取最高概率者
        d_choice = distractor["choice"]
        if d_choice == answer:
            rest = {k: v for k, v in distractor["probabilities"].items() if k != answer}
            if rest:
                d_choice = max(rest, key=rest.get)
        steps.append(_choice_step(spec.steps["distractor"], distractor, legends["option"],
                                  override_choice=d_choice))

    rule = _rule(spec, S_BASE)
    if weak:
        rule += _rule(spec, S_WEAK_SINGLE, pct=_pct(support_max))
    elif agreement:
        rule += _rule(spec, S_AGREE)
    elif answer and top_support:
        rule += _rule(spec, S_DISAGREE, answer=answer, top=top_support)

    return _trace(spec, item, data, legends, steps, answer=answer,
                  confidence=best["confidence"] if best else None, support=support,
                  top_support=top_support, support_max=support_max, weak=weak,
                  agreement=agreement, rule=rule)


# ==========================================================================
# 2) single_choice_with_trap ← questions_math.compose（单选分支）
# ==========================================================================
def single_choice_with_trap(item: dict, data: dict, legends: dict, spec: Spec) -> dict:
    """数学单选题。与 cross_check 同形，差别在两处：多一个「考点识别」前置步，
    且告警对象叫「易错选项」；改写后还会连带刷新 trap 的 desc。"""
    ans = data.get("answers", {})
    letters = sorted(item.get("options") or {})
    steps: list[dict] = []

    point = _pick(ans, "point")
    strategy = _pick(ans, "strategy")
    best = _pick(ans, "best")
    trap = _pick(ans, "trap")
    support = _noul_map(ans, "opt_", letters)

    if point:
        steps.append(_choice_step(spec.steps["point"], point, legends["point"]))
    if strategy:
        steps.append(_choice_step(spec.steps["strategy"], strategy, legends["strategy"]))

    ranked = _ranked(support)
    support_max = ranked[-1][0] if ranked else None
    top_support = ranked[-1][1] if ranked else None
    if support:
        steps.append(_group_step(spec.steps["support"], letters, support, legends["option"]))

    answer = best["choice"] if best else None
    agreement = bool(answer and top_support and answer == top_support)
    weak = support_max is not None and support_max < spec_weak_threshold(spec)
    if best:
        steps.append(_choice_step(spec.steps["best"], best, legends["option"]))
    if trap:
        trap_choice = trap["choice"]
        if trap_choice == answer:
            rest = {k: v for k, v in trap["probabilities"].items() if k != answer}
            if rest:
                trap_choice = max(rest, key=rest.get)
                trap = {**trap, "choice": trap_choice}
        steps.append(_choice_step(spec.steps["trap"], trap, legends["option"],
                                  override_choice=trap_choice))

    rule = _rule(spec, S_BASE)
    if weak:
        rule += _rule(spec, S_WEAK_SINGLE, pct=_pct(support_max))
    elif agreement:
        rule += _rule(spec, S_AGREE)
    elif answer and top_support:
        rule += _rule(spec, S_DISAGREE, answer=answer, top=top_support)

    return _trace(spec, item, data, legends, steps, answer=answer,
                  confidence=best["confidence"] if best else None, support=support,
                  top_support=top_support, support_max=support_max, weak=weak,
                  agreement=agreement, rule=rule)


# ==========================================================================
# 3) multi_threshold_set ← questions_math.compose（多选分支）
# ==========================================================================
def multi_threshold_set(item: dict, data: dict, legends: dict, spec: Spec) -> dict:
    """数学多选题。答案是一个**集合**而非单项：不使用 choice 判定，
    改为逐选项 noul 概率 ≥ threshold 的全部入选。置信度取"离 0.5 有多远"的平均。"""
    ans = data.get("answers", {})
    letters = sorted(item.get("options") or {})
    steps: list[dict] = []

    point = _pick(ans, "point")
    strategy = _pick(ans, "strategy")
    hint = _pick(ans, "count_hint")
    support = _noul_map(ans, "opt_", letters)

    if point:
        steps.append(_choice_step(spec.steps["point"], point, legends["point"]))
    if strategy:
        steps.append(_choice_step(spec.steps["strategy"], strategy, legends["strategy"]))

    ranked = _ranked(support)
    support_max = ranked[-1][0] if ranked else None
    top_support = ranked[-1][1] if ranked else None
    thr = spec_threshold(spec)
    band = spec_weak_band(spec)
    if support:
        steps.append(_group_step(spec.steps["support"], letters, support, legends["option"]))

    chosen = [L for L in letters if (support.get(L) or 0) >= thr]
    answer = "".join(chosen) if chosen else None
    decided = [(1 - abs((support[L] or 0.5) - 0.5) * 2) for L in letters if L in support]
    confidence = round(1 - sum(decided) / len(decided), 3) if decided else None
    weak = bool(support) and all(band[0] <= (support.get(L) or 0) <= band[1] for L in letters)
    agreement = None      # 集合答案没有"两路一致"可言——前端不得据此反推题型

    meta = spec.steps["decide"]
    decide = _group_step(meta, letters, support, legends["option"],
                         extra=lambda L: {"pass": (support.get(L) or 0) >= thr})
    decide["summary"] = (meta.summary_fmt or "{answer}").format(answer=answer or "—")
    decide["threshold"] = thr
    steps.append(decide)
    if hint:
        step = _choice_step(spec.steps["count_hint"], hint, {})
        step["desc"] = f"模型认为最可能有 {hint['choice']} 个正确选项"
        steps.append(step)

    rule = _rule(spec, S_BASE, threshold=_num(thr))
    if weak:
        rule += _rule(spec, S_WEAK_SET)

    return _trace(spec, item, data, legends, steps, answer=answer, confidence=confidence,
                  support=support, top_support=top_support, support_max=support_max,
                  weak=weak, agreement=agreement, rule=rule)


# ==========================================================================
# 公共出口
# ==========================================================================
def _pct(v: float | None) -> str:
    return f"{v:.0%}" if v is not None else "—"


def _num(v: float) -> str:
    """阈值出现在面向人的文案里，需保持与旧实现一致的 '0.5' 写法。"""
    return f"{v:g}" if v != int(v) else str(int(v))


def _trace(spec: Spec, item: dict, data: dict, legends: dict, steps: list[dict], *,
           answer: Any, confidence: Any, support: dict, top_support: Any,
           support_max: Any, weak: bool, agreement: Any, rule: str) -> dict:
    """组装轨迹。字段顺序与旧实现一致——落盘 JSON 的可读性也是行为的一部分。"""
    trace: dict[str, Any] = {"trace_version": spec.trace_version()}
    trace.update(spec.trace_fields)
    trace.update({
        "item_id": item["id"], "no": item["no"],
        "kind": item.get("kind", spec.kind_default),
        "model": data.get("model"), "latency_ms": data.get("_latency_ms"),
        "usage": data.get("usage"), "steps": steps,
        "answer": answer, "confidence": confidence,
        "support": support, "top_support": top_support, "support_max": support_max,
        "weak": weak, "agreement": agreement,
        # 前端不再需要从 agreement 是否为 null 反推答案形状：显式告诉它。
        "answer_shape": spec.answer_shape,
        "composition_rule": rule, "legends": legends,
    })
    return trace


def spec_weak_threshold(spec: Spec) -> float:
    s = STRATEGIES[spec.compose_strategy]
    return s.weak_threshold if s.weak_threshold is not None else 0.5


def spec_threshold(spec: Spec) -> float:
    s = STRATEGIES[spec.compose_strategy]
    return s.threshold if s.threshold is not None else 0.5


def spec_weak_band(spec: Spec) -> tuple[float, float]:
    s = STRATEGIES[spec.compose_strategy]
    return s.weak_band if s.weak_band is not None else (0.35, 0.65)


STRATEGIES: dict[str, Strategy] = {
    "single_choice_cross_check": Strategy(
        "single_choice_cross_check", single_choice_cross_check,
        ("strategy", "evidence", "support", "best", "distractor"), weak_threshold=0.5,
        needs_rule_slots=(S_BASE, S_WEAK_SINGLE, S_AGREE, S_DISAGREE)),
    "single_choice_with_trap": Strategy(
        "single_choice_with_trap", single_choice_with_trap,
        ("point", "strategy", "support", "best", "trap"), weak_threshold=0.5,
        needs_rule_slots=(S_BASE, S_WEAK_SINGLE, S_AGREE, S_DISAGREE)),
    "multi_threshold_set": Strategy(
        "multi_threshold_set", multi_threshold_set,
        ("point", "strategy", "support", "decide", "count_hint"),
        weak_band=(0.35, 0.65), threshold=0.5,
        needs_rule_slots=(S_BASE, S_WEAK_SET)),
}


def compose(name: str, spec: Spec, item: dict, data: dict, legends: dict) -> dict:
    """按策略名分发。名字来自 spec，不经任何动态导入。"""
    s = STRATEGIES.get(name)
    if s is None:
        raise KeyError(f"未知组合策略 {name!r}，可用的是 {sorted(STRATEGIES)}")
    return s.fn(item, data, legends, spec)
