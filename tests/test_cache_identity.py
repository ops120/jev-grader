"""门禁 6：缓存必须按内容认领，不能按文件名认领。

题库重新生成后 item id 会重编，`traces/X73.json` 完全可能装着另一道题的判定。
solver 早先只判断"文件在不在"，于是会把别人的答案当成本题答案直接返回——
界面上一切正常，答案是错的，而且不花任何配额所以没有任何迹象。

这条门禁把三种情形钉住：载荷一致才复用；载荷不一致必须重算；没有 request 的
旧轨迹必须重算。用一个"被调用就抛"的假客户端来证明它到底有没有复用缓存，
因此零配额。

跑法：python tests/test_cache_identity.py     退出码 0 = 四种情形都按预期

目标 = 本机题库里第一份有 ≥2 道可作答题的卷；本机没有题库时退回随仓库分发的
模板卷（走生产上传管线取信封），所以这道门禁在任何机器上都真实生效。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _targets import all_targets  # noqa: E402
import solver  # noqa: E402
from judge import engine, spec as spec_mod  # noqa: E402


class Boom:
    """被调用就说明 solver 决定重算——用它把"复用/重算"变成一个可观测事件。"""

    async def ask(self, state: dict, questions: dict) -> dict:
        raise RuntimeError("CLIENT_WAS_CALLED")


def payload_of(cfg: dict, item: dict, passage: dict | None) -> dict:
    sp = spec_mod.resolve(cfg["specs"], item)
    state, questions, _ = engine.build(sp, item, passage)
    return {"state": state, "questions": questions}


async def run_case(cfg: dict, item: dict, passage: dict | None, trace) -> dict:
    """trace 可以是 dict（正常轨迹），也可以是一段坏文本（模拟读不出）。"""
    with tempfile.TemporaryDirectory() as tmp:
        work = dict(cfg)
        work["traces"] = Path(tmp)
        body = trace if isinstance(trace, str) else json.dumps(trace, ensure_ascii=False)
        (work["traces"] / f"{item['id']}.json").write_text(body, encoding="utf-8")
        sem = asyncio.Semaphore(1)
        return await solver.solve_one(Boom(), item, passage, sem, False, work)


def main() -> int:
    pick = None
    for t in all_targets():                    # 本机题库优先，其次模板卷（永远存在）
        if t["error"]:
            if not t["local"]:
                print(f"模板卷读不出：{t['error']}")
                return 1
            continue
        if len([i for i in t["envelope"]["items"] if i.get("answerable")]) >= 2:
            pick = t
            break
    if pick is None:
        print("  SKIP  没有任何含 ≥2 道可作答题的题库，这道门禁未生效")
        return 0
    cfg = {"id": pick["id"], "specs": pick["specs"], "traces": pick["traces"] or Path(".")}
    exam = pick["envelope"]
    by_passage = {p["id"]: p for p in exam.get("passages", [])}
    answerable = [i for i in exam["items"] if i.get("answerable")]
    # 取两道题：一道当"本题"，一道提供"别人的请求"当串题缓存
    item, other = answerable[0], answerable[1]
    passage = by_passage.get(item.get("passage_id"))
    mine = payload_of(cfg, item, passage)
    theirs = payload_of(cfg, other, by_passage.get(other.get("passage_id")))
    assert engine.fingerprint(**mine) != engine.fingerprint(**theirs), \
        "两道题的载荷竟然相同，这个用例选得没意义"
    print(f"  目标题库：{pick['id']}（{'本机' if pick['local'] else '随仓库模板卷'}）·"
          f"本题 {item['id']}、串题缓存用 {other['id']}")

    failures: list[str] = []

    def check(name: str, got: dict, want_reuse: bool) -> None:
        called = "CLIENT_WAS_CALLED" in str(got.get("detail", ""))
        reused = got.get("status") == "cached"
        if reused == want_reuse and (reused or called):
            print(f"  OK    {name}：{'复用' if reused else '重算'}（符合预期）")
            return
        print(f"  FAIL  {name}：status={got.get('status')} 复用={reused} 调用客户端={called}，"
              f"期望{'复用缓存且不调用客户端' if want_reuse else '调用客户端重算'}")
        failures.append(name)

    def run(trace) -> dict:
        return asyncio.run(run_case(cfg, item, passage, trace))

    # 情形 1：载荷一致 —— 必须复用，一次调用都不发
    check("载荷一致 → 复用缓存",
          run({"model": "typesafe-jev", "answer": "A", "steps": [], "request": mine}), True)

    # 情形 2：同名文件装的是别的题（重编号后的真实事故形状）
    check("同名不同题 → 必须重算",
          run({"model": "typesafe-jev", "answer": "B", "steps": [], "request": theirs}), False)

    # 情形 3：老轨迹没落 request 载荷 —— 无从判断它属于哪道题，只能重算
    check("缓存无 request → 必须重算",
          run({"model": "typesafe-jev", "answer": "C", "steps": []}), False)

    # 情形 4：缓存文件损坏 —— 不能让整轮求解崩在这里
    try:
        check("缓存读不出 → 必须重算而非崩溃", run("{{{ 不是 JSON"), False)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  缓存文件损坏直接抛出：{type(e).__name__}: {e}")
        failures.append("坏缓存崩了整个求解")

    if failures:
        print(f"\n失败：{failures}")
        return 1
    print("\n通过：缓存只认领内容一致的轨迹；串题、无载荷、读不出的缓存一律重算并说明原因")
    return 0


if __name__ == "__main__":
    sys.exit(main())
