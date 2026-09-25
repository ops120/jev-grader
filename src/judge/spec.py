"""编排 spec 的数据模型与静态校验。

spec 承载三件事（边界见 ``judge/__init__.py``）：表格与措辞、判定清单、state 组装。
它**不**承载任何读取数值概率的逻辑——那是三个具名策略，写在 ``strategies.py``。

正因为 spec 里全是"表与字符串"，绝大多数配错的后果都是**静默的**：
criteria 引用了不存在的 state 键，JEV 会拒绝整次请求；legend 写错，界面显示空图例；
kind_when 漏了一种题型，那道题会静默套用别的措辞——而 prd.md §5 记着这类错误的代价是
「七选五全部出现伪分歧」。所以本模块在加载时把能查的全部查一遍：宁可启动即炸，
也不要让一份配置错误变成一批看起来正常、实际不可信的轨迹。

这里只查两样东西：**引用完整性**与**形状封闭性**。不做表达式求值，不加插件入口。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPECS_DIR = Path(__file__).resolve().parent / "specs"

# 今天真实存在的三种步骤形状，由 368 条历史轨迹穷举得出（数量亦标注于此）。
# 组合策略只会因为"某个判断缺失"而少输出其中几步，绝不会多出第四种形状；
# 因此一个 spec 若推出未知形状，说明它的 step 归属配错了。
ALLOWED_STEP_SHAPES: frozenset[tuple[str, ...]] = frozenset({
    ("strategy", "evidence", "support", "best", "distractor"),   # ×357
    ("point", "strategy", "support", "best", "trap"),            # ×8
    ("point", "strategy", "support", "decide", "count_hint"),    # ×3
})

# state_from 条目允许的字段。刻意不含 when/expr：条件装配留给具名钩子。
_STATE_KEYS = {"src", "key", "from", "default", "flags"}
_STATE_FLAGS = {"optional", "if_nonempty"}
_ALLOWED_SRC = {"item", "passage_sentences", "blank_context", "stem"}
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# 占位符两套写法：{{state_key}} 引用状态键，{L} 是选项字母的字面量。
# 校验时都归一成一个名字，好让"这条指令引用了什么"只有一套问法。
_PLACEHOLDER = re.compile(r"\{\{?([A-Za-z_][A-Za-z0-9_]*)\}?\}")
_LITERAL_SLOT = "L"


class SpecError(ValueError):
    """spec 不合法。消息里带 spec 名与字段路径，可直接定位到 YAML 的那一行。"""


@dataclass(frozen=True)
class StateField:
    """题目／篇章的一个字段如何进入 state。"""
    src: str                                  # item | passage_sentences | blank_context | stem
    key: str                                  # state 键名
    source: str = ""                          # 题面来源键；空 = 与 key 同名
    optional: bool = False                    # 允许整项缺席（blank_context 找不到空位时）
    if_nonempty: bool = False                 # 值为假则跳过（材料为空时不写 options）
    default: Any = None                       # 非 optional 且缺失时的取值

    @staticmethod
    def parse(raw: Any, where: str) -> "StateField":
        if isinstance(raw, str):
            return StateField(src="item", key=raw, optional=True, if_nonempty=True)
        if not isinstance(raw, dict):
            raise SpecError(f"{where}: state_from 条目需为字符串或映射")
        bad = set(raw) - _STATE_KEYS
        if bad:
            raise SpecError(f"{where}: 不支持的字段 {sorted(bad)}"
                            f"（spec 不接受表达式或条件，请用 hooks 或策略）")
        flags = raw.get("flags") or []
        if not isinstance(flags, list):
            raise SpecError(f"{where}.flags: 需为列表")
        bad_f = set(flags) - _STATE_FLAGS
        if bad_f:
            raise SpecError(f"{where}.flags: 未知标志 {sorted(bad_f)}，只允许 {sorted(_STATE_FLAGS)}")
        src = str(raw.get("src", "item"))
        if src not in _ALLOWED_SRC:
            raise SpecError(f"{where}.src: {src!r} 不在 {sorted(_ALLOWED_SRC)}")
        key = raw.get("key")
        if not isinstance(key, str) or not key:
            raise SpecError(f"{where}.key: 必填，state 键名（可与 src 同名）")
        # from：题库字段名与 state 键名不一致时的显式改名（subs → sub_questions 等）。
        source = raw.get("from")
        if source is not None and (not isinstance(source, str) or not source):
            raise SpecError(f"{where}.from: 需为非空字符串（题面来源键）")
        return StateField(src=src, key=key, source=str(source or ""),
                          optional="optional" in flags,
                          if_nonempty="if_nonempty" in flags, default=raw.get("default"))


@dataclass(frozen=True)
class Candidates:
    """choice 判断的候选集：从哪里取、截多长。"""
    source: str                     # 'sentences'
    clip: int | None = None         # criteria 用 240、证据图例用 320 —— 差别的唯一来源

    @staticmethod
    def parse(raw: Any, where: str) -> "Candidates":
        if not isinstance(raw, dict):
            raise SpecError(f"{where}: candidates 需为映射")
        bad = set(raw) - {"source", "clip"}
        if bad:
            raise SpecError(f"{where}.candidates: 不支持的字段 {sorted(bad)}")
        source = raw.get("source")
        if source != "sentences":
            raise SpecError(f"{where}.candidates.source: 目前只有 'sentences' 有实现依据，"
                            f"实为 {source!r}")
        clip = raw.get("clip")
        if clip is not None and (isinstance(clip, bool) or not isinstance(clip, int) or clip <= 1):
            raise SpecError(f"{where}.candidates.clip: 需为大于 1 的整数")
        return Candidates(source=str(source), clip=clip)


@dataclass(frozen=True)
class Judgment:
    """下发给 JEV 的一个原子判断。

    ``qkey`` 是 questions／响应 answers 里的键前缀，``step`` 是它在轨迹 steps 里的归属步骤。
    多对一（四个 noul 共用 support 步）由策略决定，不在这里表达。
    """
    qkey: str
    type: str                                   # choice | noul
    step: str
    instructions: Any = None                    # str 或 {task, note, question}；可含 {{state}} 与 @{slot}
    option_template: str | None = None          # noul：含 {{L}} 的措辞
    true_false: tuple[str, str] | None = None   # noul 的两个判据
    criteria_ref: str | None = None             # 'options' | 'tables.<name>' | 'state.<key>'
    criteria_inline: dict[str, str] | None = None
    candidates: Candidates | None = None
    per_option: bool = False                    # noul 是否按选项展开
    requires_state: str | None = None           # 该 state 引用为空则整个判断不下发
    legend_key: str | None = None               # 该判断产出的图例键
    display_clip: int | None = None             # 同一文本进图例时的截断长度（可与 criteria.clip 不同）
    desc_fmt: str | None = None                 # 少数步骤的 desc 不是 legend.get(choice)


@dataclass(frozen=True)
class KindVariant:
    """同一措辞槽位在不同题型下的变体（mcq ／ cloze ／ seven）。"""
    kinds: tuple[str, ...]              # 命中的 kind；空元组 = 兜底
    text: Any


@dataclass(frozen=True)
class StepMeta:
    key: str
    label: str
    sentence: bool = False              # 携带 sentence → 前端据此联动左栏高亮
    reveal: bool = False                # 该步的 choice 就是答案 → 前端据此揭晓
    summary_fmt: str | None = None      # group 步摘要，如 "入选 {answer}"
    order: int = -1                     # 步骤顺序；-1 = 不参与形状（尚未声明）


@dataclass(frozen=True)
class RuleText:
    """composition_rule 的文案槽位。**分支结构留在策略里，这里只有文字。**

    weak 分支在单选／多选下说的是两件不同的事（"最高也只有 X%" vs "各选项都挤在 0.5 附近"），
    所以拆成两个槽位而不是在 spec 里写条件。
    """
    base: str
    weak_single: str = ""
    weak_set: str = ""
    agree: str = ""
    disagree: str = ""


@dataclass(frozen=True)
class KindTable:
    """按题型取不同的表：英语卷的「题型识别」在 mcq／cloze／seven 下是三张不同的表。

    仍然只是一次查表，不做条件求值：表名只有一个，取哪张由 kind 决定，
    kind 没点名时用 default（＝ legacy 的 else 分支）。
    """
    default: dict[str, str]
    by_kind: dict[str, dict[str, str]] = field(default_factory=dict)

    def resolve(self, kind: str) -> dict[str, str]:
        return self.by_kind.get(kind, self.default)


@dataclass
class Spec:
    name: str
    compose_strategy: str
    answer_shape: str                   # single | set
    trace_fields: dict[str, Any] = field(default_factory=dict)
    state_literal: dict[str, Any] = field(default_factory=dict)
    state_from: tuple[StateField, ...] = ()
    kind_when: tuple[tuple[str, str], ...] = ()
    kind_when_fields: tuple[str, ...] = ("section_name", "kind")
    kind_from: str = "kind"             # 没有 kind_when 命中时，kind 直接取该题面字段
    kind_default: str = ""
    covers_kinds: tuple[str, ...] = ()  # 本 spec 负责哪些 kind；空＝该卷的默认 spec
    blank_kinds: tuple[str, ...] = ("cloze", "seven")   # 有空可填的题型：决定 blank_context
    tables: dict[str, dict[str, str] | KindTable] = field(default_factory=dict)
    judgments: dict[str, Judgment] = field(default_factory=dict)
    variants: dict[str, tuple[KindVariant, ...]] = field(default_factory=dict)
    steps: dict[str, StepMeta] = field(default_factory=dict)
    hooks: tuple[str, ...] = ()
    rules: RuleText | None = None
    display_name: str = ""
    trace_version_const: int = 2

    def table(self, name: str, kind: str) -> dict[str, str]:
        """按 kind 取一张判据表。图例名＝表名，界面按这个名字读 legends。"""
        t = self.tables[name]
        return t.resolve(kind) if isinstance(t, KindTable) else t

    @property
    def judgment_order(self) -> tuple[str, ...]:
        """声明顺序即下发顺序——落盘载荷要逐字节可比，故不能排序。"""
        return tuple(self.judgments)

    @property
    def shape(self) -> tuple[str, ...]:
        """全判断齐备时应产出的步骤序列。

        顺序取自 steps.order 而不是判断的归属顺序：多选卷的「阈值判定」这一步完全由代码
        产出、背后没有任何 JEV 判断，只能显式声明位置。
        """
        ordered = sorted(self.steps.values(), key=lambda m: (m.order, m.key))
        return tuple(m.key for m in ordered if m.order >= 0)

    def kind_search_text(self, item: dict) -> str:
        return "".join(str(item.get(f, "") or "") for f in self.kind_when_fields)

    def trace_version(self) -> int:
        """轨迹格式版本。**不因新增可选字段而递增**：那会把 368 条本地缓存全部标记为
        「轨迹过旧」，逼一次烧配额的重量级重算。只在破坏性变更时才改这个值。"""
        return self.trace_version_const

    def state_keys(self) -> set[str]:
        """build() 可能写进 state 的全部键名，用于校验指令里的 {{...}} 引用。"""
        keys = {f.key for f in self.state_from} | set(self.state_literal)
        return keys


def _need(d: dict, key: str, where: str, types: tuple) -> Any:
    if key not in d:
        raise SpecError(f"{where}: 缺少必填字段 {key!r}")
    v = d[key]
    if not isinstance(v, types) or isinstance(v, bool) and str not in types:
        raise SpecError(f"{where}.{key}: 类型应为 {types}，实为 {type(v).__name__}")
    return v


def _slots(text: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(text, str):
        out |= set(_PLACEHOLDER.findall(text))
    elif isinstance(text, dict):
        for v in text.values():
            out |= _slots(v)
    elif isinstance(text, (list, tuple)):
        for v in text:
            out |= _slots(v)
    return out


def _parse_judgment(raw: Any, where: str) -> Judgment:
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: 判定条目需为映射")
    qkey = _need(raw, "qkey", where, (str,))
    jtype = _need(raw, "type", f"{where}[{qkey}]", (str,))
    if jtype not in ("choice", "noul"):
        raise SpecError(f"{where}[{qkey}].type: 只支持 choice|noul，实为 {jtype!r}。"
                        f"score_only 没有轨迹证据，刻意未实现")
    bad = set(raw) - {"qkey", "type", "step", "instructions", "option_template",
                      "true", "false", "criteria", "candidates", "per_option",
                      "requires_state", "legend_key", "display_clip", "desc_fmt"}
    if bad:
        raise SpecError(f"{where}[{qkey}]: 不支持的字段 {sorted(bad)}")
    dclip = raw.get("display_clip")
    if dclip is not None and (isinstance(dclip, bool) or not isinstance(dclip, int) or dclip <= 1):
        raise SpecError(f"{where}[{qkey}].display_clip: 需为大于 1 的整数")
    crit = raw.get("criteria")
    ref, inline = None, None
    if isinstance(crit, str):
        ref = crit
    elif isinstance(crit, dict):
        inline = {str(k): str(v) for k, v in crit.items()}
    elif crit is not None:
        raise SpecError(f"{where}[{qkey}].criteria: 需为来源名或字面映射")
    cand = raw.get("candidates")
    tf = None
    if "true" in raw or "false" in raw:
        if "true" not in raw or "false" not in raw:
            raise SpecError(f"{where}[{qkey}]: true 与 false 必须成对出现")
        tf = (str(raw["true"]), str(raw["false"]))
    return Judgment(
        qkey=qkey, type=jtype, step=_need(raw, "step", f"{where}[{qkey}]", (str,)),
        instructions=raw.get("instructions"), option_template=raw.get("option_template"),
        true_false=tf, criteria_ref=ref, criteria_inline=inline,
        candidates=Candidates.parse(cand, where) if cand is not None else None,
        per_option=bool(raw.get("per_option")),
        requires_state=raw.get("requires_state"),
        legend_key=raw.get("legend_key"), display_clip=dclip, desc_fmt=raw.get("desc_fmt"),
    )


def _parse_variant(raw: Any, where: str) -> KindVariant:
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: 措辞变体需为映射")
    bad = set(raw) - {"kinds", "text"}
    if bad:
        raise SpecError(f"{where}: 不支持的字段 {sorted(bad)}")
    kinds = raw.get("kinds") or []
    if not isinstance(kinds, list):
        raise SpecError(f"{where}.kinds: 需为列表")
    for k in kinds:
        if not _KIND_RE.match(str(k)):
            raise SpecError(f"{where}.kinds: kind 需为小写标识符，实为 {k!r}")
    if "text" not in raw:
        raise SpecError(f"{where}: 缺少 text")
    return KindVariant(kinds=tuple(str(k) for k in kinds), text=raw["text"])


def _parse_step(raw: Any, key: str, where: str) -> StepMeta:
    if not isinstance(raw, dict):
        raise SpecError(f"{where}.steps[{key}]: 需为映射")
    bad = set(raw) - {"label", "sentence", "reveal", "summary_fmt", "order"}
    if bad:
        raise SpecError(f"{where}.steps[{key}]: 不支持的字段 {sorted(bad)}"
                        f"（图例名由 tables 的键名决定，不在这里配）")
    order = raw.get("order", -1)
    if isinstance(order, bool) or not isinstance(order, int):
        raise SpecError(f"{where}.steps[{key}].order: 需为整数")
    return StepMeta(key=key, label=_need(raw, "label", f"{where}.steps[{key}]", (str,)),
                    sentence=bool(raw.get("sentence")),
                    reveal=bool(raw.get("reveal")), summary_fmt=raw.get("summary_fmt"),
                    order=int(order))


def parse_spec(data: dict, name: str = "?") -> Spec:
    """从已解析的映射构造 Spec 并完成静态校验。抛 SpecError 即为不通过。"""
    where = f"spec[{name}]"
    if not isinstance(data, dict):
        raise SpecError(f"{where}: 顶层需为映射")
    bad = set(data) - {"name", "display_name", "compose_strategy", "answer_shape",
                       "trace_fields", "state_literal", "state_from", "kind_when",
                       "kind_when_fields", "kind_from", "kind_default", "covers_kinds",
                       "blank_kinds",
                       "tables", "judgments", "variants", "steps", "hooks", "rules",
                       "trace_version"}
    if bad:
        raise SpecError(f"{where}: 不支持的顶层字段 {sorted(bad)}")
    tv = data.get("trace_version", 2)
    if isinstance(tv, bool) or not isinstance(tv, int):
        raise SpecError(f"{where}.trace_version: 需为整数")

    kw = []
    for pair in (data.get("kind_when") or []):
        if not isinstance(pair, list) or len(pair) != 2:
            raise SpecError(f"{where}.kind_when: 每项需为 [正则, kind] 两元素列表")
        try:
            re.compile(pair[0])
        except re.error as e:
            raise SpecError(f"{where}.kind_when: 正则 {pair[0]!r} 无法编译：{e}") from None
        if not _KIND_RE.match(str(pair[1])):
            raise SpecError(f"{where}.kind_when: kind {pair[1]!r} 非法")
        kw.append((str(pair[0]), str(pair[1])))

    tables = {}
    for tname, tval in (data.get("tables") or {}).items():
        if not isinstance(tval, dict):
            raise SpecError(f"{where}.tables[{tname}]: 需为映射")
        if "by_kind" in tval or "default" in tval:
            bad_t = set(tval) - {"by_kind", "default"}
            if bad_t:
                raise SpecError(f"{where}.tables[{tname}]: 按题型取表时只允许 by_kind／default，"
                                f"多出 {sorted(bad_t)}")
            if "default" not in tval:
                raise SpecError(f"{where}.tables[{tname}].default: 必填——它是 kind 未点名时用的表")
            if not isinstance(tval["default"], dict) or not tval["default"]:
                raise SpecError(f"{where}.tables[{tname}].default: 需为非空映射")
            by_kind = {}
            for k, sub in (tval.get("by_kind") or {}).items():
                if not _KIND_RE.match(str(k)):
                    raise SpecError(f"{where}.tables[{tname}].by_kind: kind {k!r} 非法")
                if not isinstance(sub, dict) or not sub:
                    raise SpecError(f"{where}.tables[{tname}].by_kind[{k}]: 需为非空映射")
                by_kind[str(k)] = {str(ck): str(cv) for ck, cv in sub.items()}
            tables[tname] = KindTable(
                default={str(k): str(v) for k, v in tval["default"].items()}, by_kind=by_kind)
        else:
            tables[tname] = {str(k): str(v) for k, v in tval.items()}

    judgments: dict[str, Judgment] = {}
    for raw in (data.get("judgments") or []):
        j = _parse_judgment(raw, where)
        if j.qkey in judgments:
            raise SpecError(f"{where}: qkey {j.qkey!r} 重复")
        judgments[j.qkey] = j

    variants = {}
    for slot, lst in (data.get("variants") or {}).items():
        if not isinstance(lst, list):
            raise SpecError(f"{where}.variants[{slot}]: 需为列表")
        variants[slot] = tuple(_parse_variant(x, f"{where}.variants[{slot}]") for x in lst)

    steps = {_k: _parse_step(v, _k, where) for _k, v in (data.get("steps") or {}).items()}

    rules = None
    rr = data.get("rules")
    if rr is not None:
        bad_r = set(rr) - {"base", "weak_single", "weak_set", "agree", "disagree"}
        if bad_r:
            raise SpecError(f"{where}.rules: 不支持的字段 {sorted(bad_r)}")
        rules = RuleText(**{k: str(rr[k]) for k in
                            ("base", "weak_single", "weak_set", "agree", "disagree")
                            if k in rr})

    def _kind_list(field: str, val: Any) -> tuple[str, ...] | None:
        if val is None:
            return None
        if not isinstance(val, list):
            raise SpecError(f"{where}.{field}: 需为列表")
        for k in val:
            if not _KIND_RE.match(str(k)):
                raise SpecError(f"{where}.{field}: kind {k!r} 非法")
        return tuple(str(k) for k in val)

    blank_kinds = _kind_list("blank_kinds", data.get("blank_kinds"))
    covers_kinds = _kind_list("covers_kinds", data.get("covers_kinds"))

    spec = Spec(
        name=_need(data, "name", where, (str,)),
        display_name=str(data.get("display_name") or ""),
        compose_strategy=_need(data, "compose_strategy", where, (str,)),
        answer_shape=_need(data, "answer_shape", where, (str,)),
        trace_fields=dict(data.get("trace_fields") or {}),
        state_literal=dict(data.get("state_literal") or {}),
        state_from=tuple(StateField.parse(x, f"{where}.state_from")
                         for x in (data.get("state_from") or [])),
        kind_when=tuple(kw),
        kind_when_fields=tuple(data.get("kind_when_fields") or ("section_name", "kind")),
        kind_from=str(data.get("kind_from") or "kind"),
        kind_default=str(data.get("kind_default") or ""),
        blank_kinds=blank_kinds or ("cloze", "seven"),
        covers_kinds=covers_kinds or (),
        tables=tables, judgments=judgments, variants=variants, steps=steps,
        hooks=tuple(data.get("hooks") or ()), rules=rules, trace_version_const=tv,
    )
    validate(spec)
    return spec


# 唯一许可的两个具名钩子。它们都留在 Python 而不是塞进 spec，理由分别是：
# wrap_material_as_sentences 里有个真实阈值（len(mat) < 15，判断"这段文字算不算材料"）；
# math_answer_type 是"kind 的一个后缀决定另一段文案"，写进 spec 就得引入条件表达式。
# 两者都不读 JEV 返回的概率，所以属于取材规则而非结论计算。
KNOWN_HOOKS = ("wrap_material_as_sentences", "math_answer_type")

# 可作为图例名的表名闭集——与 strategies.py 里 legends[...] 的下标一一对应。
# 新增一个名字的唯一正当理由是新增了一个组合策略；只是换卷子不该动这里。
LEGEND_TABLE_NAMES = ("strategy", "point")


def validate(spec: Spec) -> None:
    """静态校验。所有检查都在加载期完成，不留到运行时。"""
    from .strategies import STRATEGIES        # 局部导入避免环

    where = f"spec[{spec.name}]"

    # ---- 策略与形状 ------------------------------------------------------
    if spec.compose_strategy not in STRATEGIES:
        raise SpecError(f"{where}.compose_strategy: 未知策略 {spec.compose_strategy!r}，"
                        f"可用的是 {sorted(STRATEGIES)}")
    if spec.answer_shape not in ("single", "set"):
        raise SpecError(f"{where}.answer_shape: 只允许 single|set，实为 {spec.answer_shape!r}")
    want = STRATEGIES[spec.compose_strategy].step_shape
    if spec.shape != want:
        raise SpecError(f"{where}: 步骤形状 {spec.shape} 与策略 {spec.compose_strategy} "
                        f"要求的 {want} 不符")
    if spec.shape not in ALLOWED_STEP_SHAPES:
        raise SpecError(f"{where}: 步骤形状 {spec.shape} 不在历史轨迹验证过的三种之内："
                        f"{sorted(ALLOWED_STEP_SHAPES)}。要加第四种必须先有真实轨迹证据")
    no_order = [k for k, m in spec.steps.items() if m.order < 0]
    if no_order:
        raise SpecError(f"{where}.steps: 这些步骤没声明 order：{sorted(no_order)}。"
                        f"未声明的步骤不会出现在形状里，等于悄悄少一步")

    # ---- 钩子 ------------------------------------------------------------
    for h in spec.hooks:
        if h not in KNOWN_HOOKS:
            raise SpecError(f"{where}.hooks: 未知钩子 {h!r}，可用的是 {sorted(KNOWN_HOOKS)}。"
                            f"spec 不提供插件入口")

    # ---- 表名 ------------------------------------------------------------
    # 表名就是图例名：策略里读图例用的是固定词汇（legends["strategy"]／legends["point"]，
    # 另两个 option／evidence 由 legend_key 'options'／'sentences' 产出）。
    # 名字取不到会让 compose 当场 KeyError，所以这里就要拦住，而不是留到组合时。
    for tname in spec.tables:
        if tname not in LEGEND_TABLE_NAMES:
            raise SpecError(f"{where}.tables[{tname}]: 表名即图例名，可用的是 "
                            f"{sorted(LEGEND_TABLE_NAMES)}（其余图例名由 legend_key 产出）")

    # ---- state 可达性 ----------------------------------------------------
    keys = spec.state_keys()
    reachable = keys | {"L"}
    for j in spec.judgments.values():
        loc = f"{where}[{j.qkey}]"
        refs = _slots(j.instructions) | _slots(j.option_template) | _slots(j.true_false)
        unreach = refs - reachable
        if unreach:
            raise SpecError(f"{loc}: 指令引用了不可达的 state 键 {sorted(unreach)}。"
                            f"可达键：{sorted(keys)}")
        if j.criteria_ref is not None:
            ref = j.criteria_ref
            if ref == "options":
                if "options" not in keys:
                    raise SpecError(f"{loc}.criteria: 'options' 需要 state_from 里有 options 项")
            elif ref.startswith("tables."):
                t = ref.split(".", 1)[1]
                if t not in spec.tables:
                    raise SpecError(f"{loc}.criteria: 引用了不存在的表 {t!r}"
                                    f"（现有 {sorted(spec.tables)}）")
            elif ref.startswith("state."):
                if ref.split(".", 1)[1] not in keys:
                    raise SpecError(f"{loc}.criteria: state.{ref.split('.', 1)[1]} 不可达")
            else:
                raise SpecError(f"{loc}.criteria: {ref!r} 需为 'options'、'tables.<name>' "
                                f"或 'state.<key>'")
        if j.requires_state is not None:
            if not j.requires_state.startswith("state."):
                raise SpecError(f"{loc}.requires_state: 需写成 'state.<key>'，"
                                f"实为 {j.requires_state!r}")
            if j.requires_state.split(".", 1)[1] not in keys:
                raise SpecError(f"{loc}.requires_state: {j.requires_state} 不是已装配的 state 键")
        if j.legend_key is not None:
            lk = j.legend_key
            if lk == "sentences":
                if j.candidates is None:
                    raise SpecError(f"{loc}.legend_key: 'sentences' 需同时声明 candidates"
                                    f"（证据图例的截断长度取自 candidates.clip）")
            elif lk != "options" and not lk.startswith("tables."):
                raise SpecError(f"{loc}.legend_key: {lk!r} 需为 'options'、'sentences' "
                                f"或 'tables.<name>'")
            elif lk.startswith("tables.") and lk.split(".", 1)[1] not in spec.tables:
                raise SpecError(f"{loc}.legend_key: 引用了不存在的表 {lk.split('.', 1)[1]!r}"
                                f"（现有 {sorted(spec.tables)}）")
        if j.type == "noul":
            if not j.per_option:
                raise SpecError(f"{loc}: noul 判断必须 per_option=true，"
                                f"否则拿不到逐选项支持度分布")
            if j.true_false is None:
                raise SpecError(f"{loc}: noul 需要 true／false 两个判据")
            if not j.option_template:
                raise SpecError(f"{loc}: noul 需要 option_template")
            if _slots(j.option_template) - {"L"}:
                raise SpecError(f"{loc}.option_template: 只允许 {{{{L}}}} 占位符，"
                                f"实为 {sorted(_slots(j.option_template))}")
            if j.candidates is not None:
                raise SpecError(f"{loc}: noul 不使用候选集")
        else:
            if j.per_option:
                raise SpecError(f"{loc}: per_option 只用于 noul")
            has_crit = (j.criteria_ref is not None or j.criteria_inline is not None
                        or j.candidates is not None)
            if not has_crit:
                raise SpecError(f"{loc}: choice 判断必须给出 criteria 或 candidates，"
                                f"否则 JEV 会以空选项拒绝整次请求")

    # ---- 措辞槽位 --------------------------------------------------------
    used: set[str] = set()
    for j in spec.judgments.values():
        for val in (j.instructions, j.option_template, *(j.true_false or ())):
            used |= _at_slots(val)
    for slot in used:
        if slot not in spec.variants:
            raise SpecError(f"{where}: 判断引用了未定义的措辞槽位 {slot!r}")
    for slot, vs in spec.variants.items():
        if slot not in used:
            raise SpecError(f"{where}.variants[{slot}]: 没有任何判断引用该槽位（死配置）")
        if not any(not v.kinds for v in vs):
            raise SpecError(f"{where}.variants[{slot}]: 缺少 kinds 为空的兜底措辞。"
                            f"兜底就是 kind_default 与任何未点名 kind 的取值（legacy 的 else 分支），"
                            f"必须显式写出来：新增 kind 时改动点要看得见，别让它落到别处的措辞上")
        covered: dict[str, None] = {}
        for v in vs:
            for k in v.kinds:
                if k in covered:
                    raise SpecError(f"{where}.variants[{slot}]: kind {k!r} 被多条措辞重复命中")
                covered[k] = None
            miss = (_slots(v.text) - {"L"}) - keys
            if miss:
                raise SpecError(f"{where}.variants[{slot}]: 措辞引用了不可达 state 键 "
                                f"{sorted(miss)}")

    # ---- 步骤 ------------------------------------------------------------
    for j in spec.judgments.values():
        if j.step not in spec.steps:
            raise SpecError(f"{where}[{j.qkey}].step: {j.step!r} 未在 steps 中定义")
    reveal = [k for k, m in spec.steps.items() if m.reveal]
    if len(reveal) != 1:
        raise SpecError(f"{where}: 必须恰好一个 reveal 步骤（前端据此揭晓答案），实为 {reveal}。"
                        f"零个会让中栏永远停在骨架上——这正是本次要修的静默失败")
    if sum(1 for m in spec.steps.values() if m.sentence) > 1:
        raise SpecError(f"{where}: sentence 标记至多一个步骤")

    # ---- 文案 ------------------------------------------------------------
    if spec.rules is None:
        raise SpecError(f"{where}: 缺少 rules（composition_rule 的文案槽位）")
    if not spec.rules.base.strip():
        raise SpecError(f"{where}.rules.base: 不能为空")
    need_branch = STRATEGIES[spec.compose_strategy].needs_rule_slots
    missing = [s for s in need_branch if not getattr(spec.rules, s, "").strip()]
    if missing:
        raise SpecError(f"{where}.rules: 策略 {spec.compose_strategy} 会用到却缺失的槽位 "
                        f"{missing}")


def _at_slots(val: Any) -> set[str]:
    """收集 {@slot} 形式的措辞槽位引用。"""
    out: set[str] = set()
    if isinstance(val, str):
        if val.startswith("@"):
            out.add(val[1:])
    elif isinstance(val, dict):
        for v in val.values():
            out |= _at_slots(v)
    elif isinstance(val, (list, tuple)):
        for v in val:
            out |= _at_slots(v)
    return out


def load(name: str) -> Spec:
    """按名字加载 spec。**找不到就报错，绝不回退到别的 spec**——
    静默拿另一份卷子的编排去问 JEV，得到的轨迹看起来完全正常，却是另一份卷子的判断。"""
    path = SPECS_DIR / f"{name}.json"
    if not path.exists():
        raise SpecError(f"spec {name!r} 不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SpecError(f"{path.name} 不是合法 JSON：{e}") from None
    spec = parse_spec(data, name)
    if spec.name != name:
        raise SpecError(f"{path.name} 内的 name={spec.name!r} 与文件名不一致")
    return spec


_CACHE: dict[str, Spec] = {}


def get(name: str) -> Spec:
    """进程内缓存。spec 是不可变的表与措辞，没必要每题重读磁盘。"""
    if name not in _CACHE:
        _CACHE[name] = load(name)
    return _CACHE[name]


def load_all() -> dict[str, Spec]:
    return {p.stem: get(p.stem) for p in sorted(SPECS_DIR.glob("*.json"))}


def resolve(names: tuple[str, ...] | list[str], item: dict) -> Spec:
    """在一份试卷挂的若干 spec 里选出这道题该用的那个。

    数学卷的单选与多选是两套判断清单（单选有「最终判定」，多选只有逐选项 + 阈值），
    所以一个试卷条目对应一组 spec，由 kind 决定用哪个：被 covers_kinds 点名者胜，
    其余情况落到唯一那个不声明 covers_kinds 的默认 spec。
    默认 spec 多于一个或一个都没有都是注册表写错了，当场报错而不是猜一个。
    """
    specs = [get(n) for n in names]
    kind = str(item.get("kind", "") or "")
    hits = [s for s in specs if kind and kind in s.covers_kinds]
    if len(hits) > 1:
        raise SpecError(f"kind {kind!r} 被多个 spec 同时认领：{[s.name for s in hits]}")
    if hits:
        return hits[0]
    fallback = [s for s in specs if not s.covers_kinds]
    if len(fallback) != 1:
        raise SpecError(
            f"spec 组 {list(names)} 里默认 spec（不声明 covers_kinds 的那个）有 {len(fallback)} 个，"
            f"必须恰好一个：kind={kind!r} 没有被任何 covers_kinds 认领时要用它")
    return fallback[0]
