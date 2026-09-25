"""并发求解：解析题目 -> 原子判断 -> 组合轨迹 -> 落盘缓存。

用法:
  python src/solver.py                      # 英语卷，求解全部可作答题目
  python src/solver.py --paper math         # 数学卷
  python src/solver.py --paper math 1 2 3   # 数学卷，只求解指定题号
  python src/solver.py --force 26           # 忽略缓存重算
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import papers
from judge import engine, spec as spec_mod, strategies
from jev_client import JevClient, JevError
import bank

ROOT = Path(__file__).resolve().parent.parent
CONCURRENCY = 4


def load_exam(paper_id: str | None) -> dict:
    cfg = papers.get(paper_id)
    if not cfg["exam"].exists():
        sys.exit(f"缺少 {cfg['exam']}，请先运行解析脚本")
    try:
        return bank.read_bank(cfg["exam"])
    except bank.BankVersionError as e:
        sys.exit(f"{cfg['exam']}: {e}")


async def solve_one(client: JevClient, item: dict, passage: dict | None,
                    sem: asyncio.Semaphore, force: bool, cfg: dict) -> dict:
    rid = item["id"]
    sp = spec_mod.resolve(cfg["specs"], item)
    state, questions, legends = engine.build(sp, item, passage)
    want = engine.fingerprint(state, questions)

    out = cfg["traces"] / f"{rid}.json"
    stale = None
    if out.exists() and not force:
        try:
            cached = json.loads(out.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            stale = f"缓存轨迹读不出（{type(e).__name__}）"
        else:
            if not cached.get("model"):
                stale = "缓存轨迹没有 model 字段，不是完整判定"
            elif not cached.get("request"):
                stale = "缓存轨迹没落 request 载荷，无从判断它属于哪道题"
            elif engine.fingerprint(cached["request"].get("state") or {},
                                    cached["request"].get("questions") or {}) != want:
                # 题库重新生成后 id 会重编：同名文件可能已是另一道题的判定。
                # 这时复用就是把别人的答案当成本题答案报出去。
                stale = "缓存轨迹的 request 与本题当前载荷不一致（题目内容或编号已变）"
            else:
                return {"id": rid, "status": "cached", "answer": cached.get("answer")}

    async with sem:
        t0 = time.perf_counter()
        try:
            data = await client.ask(state, questions)
        except JevError as e:
            return {"id": rid, "status": "error", "detail": str(e)}
        except Exception as e:  # 网络类异常
            return {"id": rid, "status": "error", "detail": f"{type(e).__name__}: {e}"}
        wall = round((time.perf_counter() - t0) * 1000)

    trace = strategies.compose(sp.compose_strategy, sp, item, data, legends)
    trace["wall_ms"] = wall
    trace["n_questions"] = len(questions)
    trace["request"] = {"state": state, "questions": questions}
    cfg["traces"].mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"id": rid, "status": "ok", "answer": trace.get("answer"),
              "agreement": trace.get("agreement"), "weak": trace.get("weak"),
              "ms": trace.get("latency_ms")}
    if stale:
        result["discarded"] = stale
    return result


async def main() -> None:
    argv = sys.argv[1:]
    paper_id = papers.DEFAULT
    if "--paper" in argv:
        i = argv.index("--paper")
        paper_id = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    force = "--force" in argv
    args = [a for a in argv if not a.startswith("-")]

    cfg = papers.get(paper_id)
    exam = load_exam(paper_id)
    # 开跑前先把 spec 全部加载：spec 写错要在烧配额之前报错，而不是跑到第一题才炸
    for _spec_name in cfg["specs"]:
        spec_mod.get(_spec_name)
    by_id = {p["id"]: p for p in exam.get("passages", [])}
    todo = [i for i in exam["items"] if i["answerable"]]
    if args:
        want = {int(a) for a in args if a.isdigit()}
        todo = [i for i in todo if i["no"] in want]
    if not todo:
        sys.exit("没有可作答的题目")

    cfg["traces"].mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    print(f"[{cfg['subject']}] 求解 {len(todo)} 题，并发 {CONCURRENCY} ...")
    t0 = time.perf_counter()
    async with JevClient() as client:
        results = await asyncio.gather(
            *(solve_one(client, it, by_id.get(it.get("passage_id")), sem, force, cfg)
              for it in todo))
    dt = time.perf_counter() - t0

    # 记录本次运行的墙钟信息：单题耗时已写在各自轨迹里，但并发跑完的真实总耗时只能在这里记
    try:
        (cfg["traces"] / "_run.json").write_text(json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": round(dt, 2), "concurrency": CONCURRENCY,
            "n": len(results), "subject": cfg["subject"], "paper": cfg["id"],
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    ok = [r for r in results if r["status"] in ("ok", "cached")]
    err = [r for r in results if r["status"] == "error"]
    print(f"\n完成 {len(ok)}/{len(results)}  用时 {dt:.1f}s")
    for r in sorted(ok, key=lambda x: int(re.sub(r"\D", "", x["id"]) or 0)):
        flags = ""
        if r.get("weak"):
            flags += "  依据薄弱"
        if r.get("agreement") is False:
            flags += "  分歧"
        print(f"  {r['id']:<5} -> {str(r.get('answer')):<5} {r.get('ms', 0):>5}ms{flags}")
    for r in err:
        print(f"  {r['id']:<5} !! {r['detail']}")
    discarded = [(r["id"], r["discarded"]) for r in results if r.get("discarded")]
    if discarded:
        print(f"\n弃用旧缓存后重算 {len(discarded)} 题（这些题花了一次真实调用）：")
        for rid, why in discarded:
            print(f"  {rid:<5} {why}")
    weak = [r["id"] for r in ok if r.get("weak")]
    if weak:
        print(f"\n依据薄弱（答案不可信）: {weak}")


if __name__ == "__main__":
    asyncio.run(main())
