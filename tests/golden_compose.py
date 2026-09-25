"""零漂移门禁 3：spec 与命名组合策略必须与重构前逐字节等价。

这是整个泛化重构能否成立的那道门。三份编排（561 行）被拆成
「声明式 spec（表与措辞）＋ 三个具名组合策略（算术）」，
唯一可接受的后果是：**发给 JEV 的载荷与组合出的轨迹，一个字都不许变。**

四段检查
--------
A  build 等价：spec 驱动的 build() 对每份题库全部可作答题，产出与重构前相同的
   (state, questions, legends)。
B  compose 等价：三个具名策略对合成响应语料，产出与 _legacy 冻结副本相同的轨迹。
   差异只允许是新增的可选字段（见 C）。
C  白名单差异：新增字段只有 top-level ``answer_shape`` 与 step-level ``reveal``。
   出现任何其他差异即失败——不允许"只比对存得下来的字段"这种放水。
D  步骤形态：产出的步骤键序列必须仍是重构前实测存在的那三种，
   出现第四种说明 spec 改动悄然改变了判定结构。

跑法：python tests/golden_compose.py     退出码 0 = 等价

合成响应用固定随机种子，不联网、不消耗配额。缺题库时跳过并说明跳过了什么。
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import papers  # noqa: E402
from judge import _legacy, engine, spec as spec_mod, strategies  # noqa: E402
from judge.orchestrators import ORCHESTRATORS  # noqa: E402

SERIALIZE = lambda o: json.dumps(o, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"))

# 允许新增的字段：界面靠它们摆脱"按步骤名猜语义"，旧轨迹没有它们时前端会标注"按推断"
ADDITIONAL_TOPLEVEL = {"answer_shape"}
ADDITIONAL_STEP = {"reveal"}

# 重构前从 368 条真实轨迹实测出的全部步骤形态。期望值写在这里而不是从被测代码导入，
# 否则这个断言就成了自我实现。spec 改动若悄悄换了判定结构，这里会炸。
EXPECTED_SHAPES = {
    ("strategy", "evidence", "support", "best", "distractor"),   # 英语／行测
    ("point", "strategy", "support", "best", "trap"),            # 数学单选
    ("point", "strategy", "support", "decide", "count_hint"),    # 数学多选
}

def is_subsequence(short: tuple, full: tuple) -> bool:
    """short 是否为 full 的子序列（相对顺序不变）。

    缺某个判断时 legacy 与新实现都只产出少几步的轨迹，所以"必须等于全形"是错的断言。
    真正不许变的是：步骤只能来自那三种全形，且顺序不许打乱。
    """
    it = iter(full)
    return all(any(x == y for y in it) for x in short)


def unexpected_shapes(shapes: set) -> list:
    return sorted(s for s in shapes if not any(is_subsequence(s, f) for f in EXPECTED_SHAPES))


# 迁移期对照表：试卷 id -> S0 编排名。papers.py 在 S3 已改指 spec，这张表只活在门禁里，
# 因为它的唯一作用是让"新旧等价"这句断言始终有一个不被 spec 影响的参照系。
LEGACY = {"english": "questions", "math": "questions_math",
          "xc_fs": "questions_cn", "xc_ds": "questions_cn", "xc_zf": "questions_cn"}

LEGACY_COMPOSE = {"questions": _legacy.compose,
                  "questions_cn": _legacy.compose,
                  "questions_math": _legacy.math_compose}


def strip_additives(trace: dict) -> dict:
    out = {k: v for k, v in trace.items() if k not in ADDITIONAL_TOPLEVEL}
    out["steps"] = [{k: v for k, v in s.items() if k not in ADDITIONAL_STEP}
                    for s in trace.get("steps", [])]
    return out


def responses_for(questions: dict, rng: random.Random) -> list[dict]:
    """为一份 questions 载荷造出若干组"模型回答"，覆盖每条策略的分支。

    不追求真实概率分布，只追求把 compose 里的每个分支都踩到：
    判定齐全 / 缺一项 / noul 为 null / 概率表为空 / 概率并列 /
    干扰项正好选中正确答案（触发代码改判） / 全部支持度低（依据薄弱） /
    全部支持度挤在 0.5 附近（多选无区分度）。
    """
    def one(style: str) -> dict:
        answers = {}
        for key, q in questions.items():
            crit = q.get("criteria") or {}
            if q.get("type") == "choice":
                keys = list(crit)
                if not keys:
                    continue
                if style == "empty_probs":
                    probs, choice = {}, keys[0]
                elif style == "tie":
                    choice = keys[0]
                    probs = {k: (0.5 if k == choice else 0.5 / max(1, len(keys) - 1))
                             for k in keys}
                else:
                    choice = rng.choice(keys)
                    raw = {k: rng.random() for k in keys}
                    total = sum(raw.values()) or 1.0
                    probs = {k: v / total for k, v in raw.items()}
                answers[key] = {"type": "choice", "choice": choice,
                                "confidence": round(rng.random(), 4), "probabilities": probs}
            elif q.get("type") == "noul":
                if style == "null_noul":
                    value = None
                elif style == "all_low":
                    value = round(rng.uniform(0.01, 0.3), 4)
                elif style == "mid_band":
                    value = round(rng.uniform(0.4, 0.6), 4)
                elif style == "all_high":
                    value = round(rng.uniform(0.7, 0.99), 4)
                else:
                    value = round(rng.random(), 4)
                answers[key] = {"type": "noul", "noul": value,
                                "confidence": 0.9 if value is not None else None,
                                "probabilities": {}}
        if style == "missing_one" and answers:
            answers.pop(rng.choice(list(answers)))
        return {"model": "jev-test", "_latency_ms": 123,
                "usage": {"input_tokens": 100, "output_tokens": 10},
                "answers": answers}

    styles = ["normal", "normal", "empty_probs", "tie", "null_noul",
              "all_low", "mid_band", "all_high", "missing_one"]
    out = [one(s) for s in styles]
    # 干扰项/易错项正好等于最终判定 —— 代码必须改判，这条分支只能手工构造
    base = one("high_agree")
    for dkey, bkey in (("distractor", "best"), ("trap", "best")):
        if dkey in base["answers"] and bkey in base["answers"]:
            base["answers"][dkey]["choice"] = base["answers"][bkey]["choice"]
            base["answers"][dkey]["probabilities"] = {
                k: (0.9 if k == base["answers"][bkey]["choice"] else 0.03)
                for k in (questions.get(dkey, {}).get("criteria") or {"X": ""})}
            base["answers"][bkey]["probabilities"] = {
                k: (0.9 if k == base["answers"][bkey]["choice"] else 0.03)
                for k in (questions.get(bkey, {}).get("criteria") or {"X": ""})}
    out.append(base)
    return out


def diff_paths(old: dict, new: dict, prefix: str = "") -> list[str]:
    """定位第一处理解差异，输出可读路径而不是整坨 JSON。"""
    hits: list[str] = []
    if type(old) is not type(new):
        return [f"{prefix}: 类型 {type(old).__name__} → {type(new).__name__}"]
    if isinstance(old, dict):
        for k in sorted(set(old) | set(new)):
            if k not in old:
                hits.append(f"{prefix}.{k}: 新增")
            elif k not in new:
                hits.append(f"{prefix}.{k}: 丢失")
            else:
                hits += diff_paths(old[k], new[k], f"{prefix}.{k}")
    elif isinstance(old, list):
        if len(old) != len(new):
            hits.append(f"{prefix}: 长度 {len(old)} → {len(new)}")
        for i, (a, b) in enumerate(zip(old, new)):
            hits += diff_paths(a, b, f"{prefix}[{i}]")
    elif old != new:
        hits.append(f"{prefix}: {str(old)[:60]!r} → {str(new)[:60]!r}")
    return hits


def check_build(bank: dict, spec_names: tuple, passages: dict, org: str) -> tuple[int, list[str]]:
    n = 0
    errs: list[str] = []
    for item in sorted(bank["items"], key=lambda x: x["no"]):
        if not item.get("answerable"):
            continue
        psg = passages.get(item.get("passage_id"))
        old = ORCHESTRATORS[org]["build"](item, psg)
        new = engine.build(spec_mod.resolve(spec_names, item), item, psg)
        n += 1
        if SERIALIZE(dict(zip(("state", "questions", "legends"), new))) != \
           SERIALIZE(dict(zip(("state", "questions", "legends"), old))):
            d = diff_paths(dict(zip(("state", "questions", "legends"), old)),
                           dict(zip(("state", "questions", "legends"), new)),
                           item["id"])
            errs.append(f"{item['id']}: " + "; ".join(d[:4]))
    return n, errs


def check_compose(bank: dict, org: str, spec_names: tuple, passages: dict,
                  rng: random.Random) -> tuple[int, int, list[str], set]:
    legacy_fn = LEGACY_COMPOSE[org]
    n_cases = n_items = 0
    errs: list[str] = []
    shapes: set[tuple] = set()
    for item in sorted(bank["items"], key=lambda x: x["no"]):
        if not item.get("answerable"):
            continue
        use = spec_mod.resolve(spec_names, item)
        psg = passages.get(item.get("passage_id"))
        _, questions, legends_new = engine.build(use, item, psg)
        _, _, legends_old = ORCHESTRATORS[org]["build"](item, psg)
        n_items += 1
        for data in responses_for(questions, rng):
            n_cases += 1
            old = legacy_fn(item, data, legends_old)
            new = strategies.compose(use.compose_strategy, use, item, data, legends_new)
            shapes.add(tuple(s.get("key") for s in new.get("steps", [])))
            stripped = strip_additives(new)
            if SERIALIZE(stripped) != SERIALIZE(old):
                d = diff_paths(old, stripped, item["id"])
                errs.append(f"{item['id']}/{n_cases}: " + "; ".join(d[:3]))
                if len(errs) > 6:
                    return n_items, n_cases, errs, shapes
    return n_items, n_cases, errs, shapes


SPECS_FOR: dict[str, str] = {}  # spec 名 -> 旧编排名，main 里按 papers 注册表填


def main() -> int:
    # S0 对拍只覆盖 S0 时代就存在的五份卷。S0 之后新注册的卷（README 明文支持的
    # 「接入新卷」路径）天生没有重构前的对照副本——它们该跳过，而不是把门禁打红。
    post_s0 = [pid for pid in papers.PAPERS if pid not in LEGACY]
    if post_s0:
        print(f"  NOTE  以下试卷为 S0 之后注册，无重构前对照副本，不参与本道对拍：{post_s0}")
    for pid, cfg in papers.PAPERS.items():
        if pid not in LEGACY:
            continue
        for name in cfg["specs"]:
            SPECS_FOR.setdefault(name, LEGACY[pid])
    specs = {s.name: s for s in spec_mod.load_all().values()}
    missing = set(SPECS_FOR) - set(specs)
    if missing:
        print(f"FAIL  以下 spec 不存在：{sorted(missing)}")
        return 1

    rng = random.Random(20260924)
    build_total = case_total = 0
    seen_shapes: set[tuple] = set()
    failures: list[str] = []
    for pid, cfg in papers.PAPERS.items():
        if pid not in LEGACY:
            continue                      # S0 之后注册的卷：无对照副本，见 main 开头的 NOTE
        if not cfg["exam"].exists():
            print(f"  SKIP  {pid:<8} 题库缺失，此门禁未生效")
            continue
        bank = json.loads(cfg["exam"].read_text(encoding="utf-8"))
        passages = {p["id"]: p for p in bank.get("passages", [])}
        org = LEGACY[pid]
        nb, eb = check_build(bank, cfg["specs"], passages, org)
        ni, nc, ec, shapes = check_compose(bank, org, cfg["specs"], passages, rng)
        seen_shapes |= shapes
        build_total += nb
        case_total += nc
        status = "OK  " if not (eb or ec) else "FAIL"
        print(f"  {status}  {pid:<8} build {nb} 题等价 · compose {ni} 题 × {nc} 组合成响应")
        failures += [f"{pid}/build {m}" for m in eb[:5]]
        failures += [f"{pid}/compose {m}" for m in ec[:5]]

    for name, org in SPECS_FOR.items():
        if spec_mod.get(name).compose_strategy not in strategies.STRATEGIES:
            failures.append(f"spec {name} 指向未知组合策略")
    bad_shapes = unexpected_shapes(seen_shapes)
    if bad_shapes:
        failures.append(f"出现重构前不存在的步骤（或顺序变了）：{bad_shapes}")
    full_seen = {s for s in seen_shapes if s in EXPECTED_SHAPES}
    if build_total and full_seen != EXPECTED_SHAPES and not failures:
        print(f"  提示：本次未覆盖到全形 {sorted(EXPECTED_SHAPES - full_seen)}"
              f"（对应题库缺失？）")

    if failures:
        print("\nspec/策略与重构前不等价（这是硬失败，禁止改基线绕过）：")
        for f in failures[:30]:
            print("  -", f)
        return 1
    print(f"\n通过：{build_total} 题 build 逐字节等价；"
          f"compose 在 {case_total} 组合成响应上与冻结副本等价，"
          f"差异仅限新增可选字段 {sorted(ADDITIONAL_TOPLEVEL | ADDITIONAL_STEP)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
