"""spec 驱动的 build()：把 item + passage 变成 (state, questions, legends)。

这里没有算术。全部工作是"按 spec 声明的清单，把表格与措辞填成 JEV 要的字典"。
唯一带判断的地方是**某个判断要不要下发**（``requires_state``）与**截多长**（``clip``），
两者都只决定"发给模型什么"，不决定"模型说了什么"——所以它们属于取材规则而非结论计算。

顺序即行为：questions 与 state 的键插入顺序由 spec 的声明顺序决定，落盘载荷要逐字节可比，
因此这里一律用显式有序构造，不做排序、不用推导式打乱。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .spec import Judgment, Spec

# {{name}} —— 引用一个 state 键；{L} —— 选项字母占位符。两套语法刻意分开：
# 前者在构建期展开、后者在 noul 展开时替换，混用会让"这题问的是哪个选项"变得不可读。
_STATE_REF = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def clip(text: str, n: int | None) -> str:
    """截断并加省略号。criteria 截到 240、证据图例截到 320 —— 两处不同是历史行为，保持原样。"""
    if n is None or len(text) <= n:
        return text
    return text[: n - 1] + "…"


def sentences_of(passage: dict | None) -> list[dict]:
    return list((passage or {}).get("sentences") or [])


def blank_context(spec: Spec, item: dict, passage: dict | None, kind: str) -> str:
    """取含该题空的句子及前后各一句，作为填空类题目的上下文。

    只对 spec.blank_kinds 点名的题型计算：阅读理解的篇章里同样内联了题号标记，
    无条件取上下文会给非填空题凭空塞一个 state 键，改变下发载荷。
    """
    if kind not in spec.blank_kinds:
        return ""
    sents = candidate_sentences(spec, item, passage)
    idx = next((i for i, s in enumerate(sents) if f"[{item['no']}]" in s["text"]), None)
    if idx is None:
        return ""
    lo, hi = max(0, idx - 1), min(len(sents), idx + 2)
    return " ".join(s["text"] for s in sents[lo:hi])


def candidate_sentences(spec: Spec, item: dict, passage: dict | None) -> list[dict]:
    """证据定位的候选句。

    篇章存在就用篇章；否则只有挂了 ``wrap_material_as_sentences`` 钩子的卷子
    （行测：材料没有结构化篇章，就自己按句号切）才有第二来源。
    两条路都没有时返回空表 —— 调用方据此**整个判断都不下发**。
    """
    sents = sentences_of(passage)
    if sents:
        return sents
    if "wrap_material_as_sentences" in spec.hooks:
        return list(item.get("__material_sentences__") or [])
    return []


def resolve_kind(spec: Spec, item: dict) -> str:
    """按 kind_when 首个命中推出 kind；否则取题面 kind 字段；再没有才用 kind_default。

    行测卷靠第一步把"选词填空"认成 cloze，英语／数学卷则直接沿用题库里的 kind
    （cloze／seven／math_single…）。措辞差异是有实测代价的（prd.md §5），
    所以这条链必须写在 spec 里看得见，而不是散落在 Python 的正则中。
    """
    hay = spec.kind_search_text(item)
    for pattern, kind in spec.kind_when:
        if re.search(pattern, hay):
            return kind
    return str(item.get(spec.kind_from, "") or "") or spec.kind_default


# ==========================================================================
# 具名钩子：唯一许可的两个扩展点。不做插件入口、不做每步 JS 钩子。
# ==========================================================================
def hook_wrap_material_as_sentences(item: dict) -> dict:
    """行测卷：把 item['material'] 切成句子。

    阈值 len(mat) < 15 是真的：有些题的"材料"只是「请开始答题：」这类版式文字，
    当材料用会让证据定位拿到一堆噪音，此时退回用题干自身作句子来源。
    """
    it = dict(item)
    mat = (item.get("material") or "").strip()
    if len(mat) < 15:
        mat = item.get("stem", "")
    if not mat:
        return it
    parts = re.split(r"(?<=[。！？；.!?;])\s*", mat)
    sents = [{"no": f"S{i + 1}", "text": p.strip()} for i, p in enumerate(parts) if p.strip()]
    if not sents:
        return it
    it["__material_sentences__"] = sents
    if not it.get("passage_id"):
        it["passage_id"] = "__material__"
    return it


def hook_math_answer_type(spec: Spec, item: dict, state: dict[str, Any]) -> None:
    """数学卷：单选／多选要用不同的话告诉模型答案形状。

    这个值不是题目字段、也不是常量，而是"kind 的一个后缀决定了另一段文案"——
    放进 spec 就得引入条件表达式，越过了边界，所以做成具名钩子。
    """
    kind = str(item.get("kind", "") or spec.kind_default)
    state["answer_type"] = ("多选（可能有多个正确选项）" if kind.endswith("multi")
                            else "单选（只有一个正确选项）")


# 两类钩子，按签名分家：一个改写 item（在装配 state 之前跑），一个往 state 里补派生值。
ITEM_HOOKS = {"wrap_material_as_sentences": hook_wrap_material_as_sentences}
STATE_HOOKS = {"math_answer_type": hook_math_answer_type}
HOOKS = {**ITEM_HOOKS, **STATE_HOOKS}


def run_item_hooks(spec: Spec, item: dict) -> dict:
    for h in spec.hooks:
        if h in ITEM_HOOKS:
            item = ITEM_HOOKS[h](item)
    return item


# ==========================================================================
# 措辞解析
# ==========================================================================
def _variant_text(spec: Spec, slot: str, kind: str) -> Any:
    """取槽位在给定 kind 下的文字；该 kind 没被任何条目点名时用兜底条目。

    真的会用到兜底：英语 spec 的 ``@best``／``@support`` 只为 cloze、seven 配了专属措辞，
    普通单选（kind 为 ``mcq`` 或空）就落到 kinds 为空的那条——这正是重构前 else 分支的语义。
    ``validate`` 已保证每个槽位都有兜底条目，末尾的 raise 只防内部不变量被破坏。
    """
    vs = spec.variants[slot]
    for v in vs:
        if kind in v.kinds:
            return v.text
    for v in vs:
        if not v.kinds:
            return v.text
    raise LookupError(f"spec[{spec.name}].variants[{slot}] 无可用措辞")


def resolve_instr(spec: Spec, val: Any, kind: str, state: dict[str, Any],
                  letter: str | None = None) -> Any:
    """把 {@slot} ／ {{state}} ／ {L} 三种占位解析成最终字符串。"""
    if isinstance(val, dict):
        return {k: resolve_instr(spec, v, kind, state, letter) for k, v in val.items()}
    if isinstance(val, list):
        return [resolve_instr(spec, v, kind, state, letter) for v in val]
    if not isinstance(val, str):
        return val
    if val.startswith("@"):
        return resolve_instr(spec, _variant_text(spec, val[1:], kind), kind, state, letter)
    out = _STATE_REF.sub(lambda m: str(state.get(m.group(1), "")), val)
    if letter is not None:
        out = out.replace("{L}", letter)
    return out


# ==========================================================================
# 载荷指纹
# ==========================================================================
def fingerprint(state: dict, questions: dict) -> str:
    """一次请求的稳定指纹。solver 用它判断缓存里那条轨迹是不是**这道题**的，
    门禁用它把重建载荷与当年真实落盘的 request 逐字节对拍——同一份序列化，
    两边才不会一个判过、一个判不过。

    题号不能当身份：题库重新生成后 id 会重编，同一份轨迹文件可能已经属于别的题。
    """
    import json
    return json.dumps({"state": state, "questions": questions},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ==========================================================================
# build
# ==========================================================================
def build(spec: Spec, item: dict, passage: dict | None = None
          ) -> tuple[dict, dict[str, Any], dict[str, Any]]:
    """返回 (state, questions, legends)。legends 供界面把判定键翻成中文。"""
    item = run_item_hooks(spec, item)
    kind = resolve_kind(spec, item)
    options: dict[str, str] = item.get("options") or {}
    letters = sorted(options)
    sents = candidate_sentences(spec, item, passage)

    # ---- state -----------------------------------------------------------
    state: dict[str, Any] = {}
    for f in spec.state_from:
        if f.src == "passage_sentences":
            value: Any = " ".join(s["text"] for s in sents)
        elif f.src == "blank_context":
            value = blank_context(spec, item, passage, kind)
        elif f.src == "stem":
            value = item.get("stem", "")
        else:
            value = item.get(f.source or f.key, f.default)
        if f.if_nonempty and not value:
            continue
        if value is None and not f.optional:
            value = f.default
        state[f.key] = value
    state.update(spec.state_literal)
    for h in spec.hooks:
        if h in STATE_HOOKS:
            STATE_HOOKS[h](spec, item, state)

    # ---- questions -------------------------------------------------------
    questions: dict[str, Any] = {}
    for qk in spec.judgment_order:
        j = spec.judgments[qk]
        if j.requires_state is not None and not _state_ref(spec, j.requires_state, state):
            continue        # 没候选就不下发：choice 至少要有 1 个 criteria，否则整次请求被拒
        instr = resolve_instr(spec, j.instructions, kind, state)
        if j.type == "noul":
            tmpl = resolve_instr(spec, j.option_template, kind, state)
            tf = {"true": resolve_instr(spec, j.true_false[0], kind, state),
                  "false": resolve_instr(spec, j.true_false[1], kind, state)}
            for L in letters:
                questions[f"{j.qkey}_{L}"] = {
                    "type": "noul",
                    "instructions": tmpl.replace("{L}", L),
                    "criteria": {"true": tf["true"], "false": tf["false"]},
                }
            continue
        entry: dict[str, Any] = {"type": "choice", "instructions": instr}
        crit = _criteria(spec, j, options, state, sents, kind)
        if crit is not None:
            entry["criteria"] = crit
        questions[qk] = entry

    # ---- legends ---------------------------------------------------------
    # 图例键**只**来自 judgments.legend_key：配错在加载期就被 spec.validate 拦住，
    # 不会变成界面上的空白。证据 criteria 截 240、证据图例截 320 是两个不同的长度
    #（同一句话在下发载荷与展示文案里长度不同），分别由 candidates.clip 与
    # judgment.display_clip 声明——少写 display_clip 就会静默把展示文案也截成 240。
    legends: dict[str, Any] = {}
    for qk in spec.judgment_order:
        j = spec.judgments[qk]
        if j.legend_key is None:
            continue
        if j.legend_key == "options":
            legends.setdefault("option", options)
        elif j.legend_key == "sentences":
            legends.setdefault("evidence",
                               {s["no"]: clip(s["text"], j.display_clip
                                              or (j.candidates.clip if j.candidates else None))
                                for s in sents})
        elif j.legend_key.startswith("tables."):
            t = j.legend_key.split(".", 1)[1]
            legends.setdefault(t, spec.table(t, kind))
    return state, questions, legends


def _state_ref(spec: Spec, ref: str, state: dict[str, Any]) -> Any:
    """解析 'state.<key>' 引用。前缀是必须的——与 criteria 用同一套写法，
    免得读 spec 的人去猜一个裸名字到底是状态键还是别的什么。"""
    if not ref.startswith("state."):
        raise LookupError(f"spec[{spec.name}]: requires_state 需写成 'state.<key>'，实为 {ref!r}")
    return state.get(ref.split(".", 1)[1])


def _criteria(spec: Spec, j: Judgment, options: dict, state: dict,
              sents: list[dict], kind: str) -> dict | None:
    ref = j.criteria_ref
    if ref == "options":
        return options
    if ref and ref.startswith("tables."):
        return spec.table(ref.split(".", 1)[1], kind)
    if ref and ref.startswith("state."):
        v = state.get(ref.split(".", 1)[1])
        return v if isinstance(v, dict) else None
    if j.criteria_inline is not None:
        return dict(j.criteria_inline)
    if j.candidates is not None:
        return {s["no"]: clip(s["text"], j.candidates.clip) for s in sents}
    return None
