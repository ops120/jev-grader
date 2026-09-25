"""零漂移门禁 2：与历史真实请求对拍。

solver.py 会把每次请求的 state 与 questions 原样落在轨迹里的 request 字段，
所以这里可以拿「当年真的发给过 JEV 的载荷」当金标准，逐题比对今天的 build()。
它比基线哈希更强：哈希只保证「前后一致」，这条保证「与线上跑过的那次一致」。

跑法：python tests/golden_replay.py       退出码 0 = 全部逐字节一致

缺题库或缺轨迹时跳过并如实说明跳过了多少——不要把它们当成通过。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import papers  # noqa: E402
from judge import engine, spec as spec_mod  # noqa: E402


def main() -> int:
    total = compared = 0
    bad: list[str] = []
    for pid, cfg in papers.PAPERS.items():
        if not cfg["exam"].exists():
            print(f"  SKIP  {pid:<8} 题库缺失")
            continue
        exam = json.loads(cfg["exam"].read_text(encoding="utf-8"))
        passages = {p["id"]: p for p in exam.get("passages", [])}
        n_ok = n_have = n_item = 0
        for item in exam["items"]:
            if not item.get("answerable"):
                continue
            n_item += 1
            trace_file = cfg["traces"] / f"{item['id']}.json"
            if not trace_file.exists():
                continue
            stored = json.loads(trace_file.read_text(encoding="utf-8")).get("request")
            if not stored:
                continue
            n_have += 1
            sp = spec_mod.resolve(cfg["specs"], item)
            state, questions, _ = engine.build(sp, item,
                                               passages.get(item.get("passage_id")))
            if engine.fingerprint(state, questions) == engine.fingerprint(
                    stored.get("state") or {}, stored.get("questions") or {}):
                n_ok += 1
            else:
                bad.append(f"{pid}/{item['id']}")
        total += n_item
        compared += n_have
        print(f"  {'OK  ' if n_ok == n_have else 'FAIL'}  {pid:<8} {n_ok}/{n_have} 题与历史请求一致"
              f"（可作答题 {n_item}）")

    if bad:
        print(f"\n与历史真实请求不一致的题（{len(bad)}）：{bad[:20]}{' …' if len(bad) > 20 else ''}")
        return 1
    if compared < total:
        print(f"\n通过，但只有 {compared}/{total} 题有历史轨迹可比对，其余仅由基线哈希覆盖")
    else:
        print(f"\n通过：全部 {compared} 题的重建请求与历史上真实发给 JEV 的载荷逐字节一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
