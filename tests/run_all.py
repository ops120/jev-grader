"""一次跑完全部零配额门禁：不联网、不消耗 JEV 额度，每次提交前都该过。

跑法：python tests/run_all.py        退出码 0 = 全绿

每道门禁各管一段，缺一不可：
  golden_build        发给 JEV 的载荷与 S0 基线哈希一致（最强，且不依赖任何轨迹文件）
  golden_replay       今天重建的载荷与历史上真实发过的那次逐字节一致
  golden_compose      spec + 命名策略与重构前的两份 S0 副本等价
  test_judge_contract 作为参照系的 S0 副本自身没被"顺手修一下"
  test_solver_replay  build -> ask -> compose -> 落盘 整条生产接线重放
  test_cache_identity 缓存按内容认领：题库重编号后不把别的题的判定当本题答案
  test_bank           题库构造层 bank.py 的纯函数测试（唯一不需要题库文件的门禁，
                      所以也是新 clone 的机器上唯一真实生效的那道）
  test_extract        抽取层（probe 定位 + 纯代码验收）的 fixture 测试，同样不需要题库文件

题库不入库，所以在没有题库的机器上多数门禁会打印 SKIP：那不是通过，是未生效。
本脚本如实把 skipped 数带进汇总，禁止把"跳过"读成"安全"。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATES = ["golden_build.py", "golden_replay.py", "golden_compose.py",
         "test_judge_contract.py", "test_solver_replay.py", "test_cache_identity.py",
         "test_bank.py", "test_extract.py"]


def main() -> int:
    results = []
    for gate in GATES:
        proc = subprocess.run([sys.executable, str(HERE / gate)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(HERE.parent))
        out = (proc.stdout or "") + (proc.stderr or "")
        results.append((gate, proc.returncode, out))
        print(f"\n{'=' * 70}\n$ python tests/{gate}   -> exit {proc.returncode}\n{'=' * 70}")
        print(out.rstrip())

    failed = [g for g, rc, _ in results if rc != 0]
    # 只认行首的 SKIP 标记。早先是拿整段输出做 "跳过" in out 的子串匹配，
    # 于是任何一条断言名字里带「跳过」两个字，都会把一道**真的跑过**的门禁
    # 报成"因缺数据而未生效"——这类误报会让人整体低估覆盖面。
    skipped = [g for g, _, out in results
               if any(ln.strip().startswith("SKIP") for ln in out.splitlines())]
    print("\n" + "=" * 70)
    print(f"门禁汇总：{len(results) - len(failed)}/{len(results)} 通过"
          + (f"；失败 {failed}" if failed else "；全部通过"))
    if skipped:
        print(f"其中 {len(skipped)} 道因缺少本机题库/轨迹而未生效：{skipped}"
              f" —— 这不是通过，别据此认为改动安全")
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
