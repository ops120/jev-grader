"""零漂移门禁 1：build() 签名基线。

证明「今天的 build() 和基线那一刻输出逐字节相同」。它对载荷内容最敏感，但**分不清**
「载荷变了」和「题目集合/重编号变了」——那一个问题由 tests/golden_replay.py 回答。
两道一起绿才叫「发给 JEV 的东西一字未变」。

跑法：python tests/golden_build.py        退出码 0 = 无漂移

基线自 S0（重构前）在本机 5 份真实题库上测得；行测三份在 S5 重钉过一次，
被替换的 S0 签名与重钉理由都在 S0_HISTORY 里，不删。

题库文件不入仓库（版权归原权利人），因此缺库时本测试跳过而不是失败——
但请注意：跳过意味着这道最强的门禁没有生效，不能据此认为改动安全。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import papers  # noqa: E402
from judge import engine, spec as spec_mod  # noqa: E402

# 试卷 id -> (build() 全量输出的 SHA-256, 参与计算的可作答题数)
# 基线是 S0（重构前）在本机 5 份真实题库上测得的值，此后不随实现变化。
# 要改这些值，必须能说清为什么；git diff 会盯着它们。
BASELINE = {
    "english": ("2fec92be56a8cb2799e2200af02fee20207aff56aa9155b2163a9590669e2162", 35),
    "math": ("e464b4813d2cc1c9469caba6c1f55d4127e8e231e1b4371683b14736d38eb864", 11),
    "xc_fs": ("9c21d7a6680dda766523a9ca525321865e8313b94a1dd8e4544660130916be27", 113),
    "xc_ds": ("c13740199e1a8af8bbd916cc234f6f05f40cdf71bd23edfad5a4d1f320cd08c7", 112),
    "xc_zf": ("63a56fabe29857dc6bd58425471d3f9416c657504fc002867bb615c0cedf5e1b", 123),
}

# S5 重钉了一份基线，原因记录如下——被替换的值不删，因为「签名变了」这件事
# 本身就是这份门禁要留的证据。
#
# 行测三份的解析器改薄壳走 bank.py 时修掉了静默丢题：旧实现只在集齐 ≥3 个选项
# 时才 append，所以题干或选项在 PDF 文本层残缺的题**直接消失**（卷面 122–135 个
# 题号只留下 104–110 条）。新实现一律保留，完整性判定降级为 answerable=False +
# Validation 记录。条目数因此 110→120 / 104→122 / 108→130，可作答题数
# 110→113 / 104→112 / 108→123。
#
# 题号变了是因为 id 按保留下来的题目顺序重编，而签名 blob 以 item["id"] 为键——
# 所以哈希必然变，且这**不代表**发给 JEV 的载荷变了。载荷有没有变由
# tests/golden_replay.py 判：它拿当年真实落盘的 request 逐字节对拍，
# 重钉时 366/366 题一致（英语 35、数学 11、行测 110/102/108）。
# 也就是说：变化的只有「哪些题进了题库」和「它们叫什么」，没有一题的载荷内容变化。
S0_HISTORY = {  # 试卷 id: (被替换的 S0 签名, S0 题数, 生效版本)
    "xc_fs": ("34ec305dacc5d9c93e80e7d8df4ea9bc8f2036afa6889360ee51347ba65238f2", 110, "S0–S4"),
    "xc_ds": ("02f9d3181aa06d4709b0193299114c3a062e93427ae029b56a666653b4e25223", 104, "S0–S4"),
    "xc_zf": ("32a7b307711cc7610faf80e4dbe33f8cc6589b4d7e9d1c63dab72d5b46cc47de", 108, "S0–S4"),
}

SERIALIZE = lambda o: json.dumps(o, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"))


def signature(paper_id: str) -> tuple[str, int]:
    """按题号顺序，对每道可作答题调 build()，把三元组整体哈希。"""
    cfg = papers.PAPERS[paper_id]
    exam = json.loads(cfg["exam"].read_text(encoding="utf-8"))
    passages = {p["id"]: p for p in exam.get("passages", [])}
    blob = {}
    for item in sorted(exam["items"], key=lambda x: x["no"]):
        if not item.get("answerable"):
            continue
        sp = spec_mod.resolve(cfg["specs"], item)
        state, questions, legends = engine.build(sp, item,
                                                 passages.get(item.get("passage_id")))
        blob[item["id"]] = {"state": state, "questions": questions, "legends": legends}
    digest = hashlib.sha256(SERIALIZE(blob).encode("utf-8")).hexdigest()
    return digest, len(blob)


def main() -> int:
    failures: list[str] = []
    skipped: list[str] = []
    for pid, (want, want_n) in BASELINE.items():
        bank = papers.PAPERS[pid]["exam"]
        if not bank.exists():
            skipped.append(pid)
            print(f"  SKIP  {pid:<8} 题库缺失（{bank.name}），这道门禁未生效")
            continue
        got, n = signature(pid)
        if (got, n) != (want, want_n):
            failures.append(f"{pid}: 签名 {got[:12]}…/{n} 题，应为 {want[:12]}…/{want_n} 题")
            print(f"  FAIL  {pid:<8} {got[:12]}… {n} 题")
        else:
            print(f"  OK    {pid:<8} {got[:12]}… {n} 题")

    if failures:
        print("\nbuild() 签名与基线不同。可能是发给 JEV 的载荷变了，也可能只是题目集合或"
              "重编号变了——这道门禁分不出来。")
        print("要确认载荷本身，跑 tests/golden_replay.py（与当年真实落盘的 request 逐字节对拍）：")
        print("它一致而本道不一致 = 题目集合/编号变了，载荷没变；此时才允许重钉基线并写明理由。")
        for f in failures:
            print("  -", f)
        return 1
    if skipped:
        print(f"\n通过，但 {len(skipped)} 份题库缺失故未校验（本机需自备题库才能跑这道门禁）")
    else:
        print(f"\n通过：{len(BASELINE)} 份题库、{sum(n for _, n in BASELINE.values())} 题的 "
              f"build() 输出与基线逐字节一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
