"""门禁：上传管线 upload.ingest 的结构验收与归一化。

本仓库不做试卷提取，「用户按模板上传的题库」是唯一输入，所以这条管线就是
题库质量的第一道也是唯一一道机器关口。它必须做到：

  1. 结构错误（缺 meta 字段、坏选项字母、悬空篇章引用、spec 不认领 kind）
     一律 **error 即整卷拒绝** —— 工作台宁可没有题库，也不要一份会悄悄算错卷的题库；
  2. 可修复的问题（重复题号、小节计数不符、答案含不存在的字母、未知 kind）
     以告警随题库入库，全部出现在体检报告里；
  3. 归一化不改写用户内容：不改题干、不删条目，缺损只降级为不可作答并给原因；
  4. 随仓库分发的模板卷（templates/bank_template.json）必须始终能通过本管线——
     模板与管线是一份契约的两半，任何一半改动都要让另一半对得上。

不需要任何本机题库文件，clone 后即真实生效。
跑法：python tests/test_upload.py     退出码 0 = 全绿
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import upload  # noqa: E402

TEMPLATE = ROOT / "templates" / "bank_template.json"

failures: list[str] = []


def ok(name: str) -> None:
    print(f"  OK    {name}")


def fail(name: str, why: str) -> None:
    print(f"  FAIL  {name}：{why}")
    failures.append(name)


def check(name: str, cond: bool, why: str) -> None:
    ok(name) if cond else fail(name, why)


def codes(errors: list[dict]) -> list[str]:
    return [e.get("code") for e in errors]


def ingest(raw, name: str = "t.json"):
    return upload.ingest(raw, filename=name)


def minimal(**over) -> dict:
    """一份最小合法题库（可局部覆盖）。"""
    base = {
        "meta": {"title": "最小卷", "subject": "测试", "specs": ["english"]},
        "items": [
            {"no": 1, "stem": "Which one is right?",
             "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "answer": "A"},
        ],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
def run_template() -> None:
    print("\n--- 模板卷必须始终能通过本管线（模板与管线的契约） ---")
    raw = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    b, errors = ingest(raw, "bank_template.json")
    if b is None:
        fail("模板卷通过上传管线", f"被拒：{codes(errors)}")
        return
    m = b["meta"]
    check("模板卷通过且 6 题、5 题可作答（听力题按性质不可作答）",
          m["total_items"] == 6 and m["answerable"] == 5,
          f"实得 total={m['total_items']} answerable={m['answerable']}")
    check("模板卷没有 error/告警（它是干净样板）",
          [v for v in b["validations"] if v["severity"] in ("error", "warn")] == [],
          f"实得 {[(v['severity'], v['code']) for v in b['validations']]}")
    check("听力题如实标注不可作答原因",
          any(not i["answerable"] and "音频" in (i.get("notice") or "") for i in b["items"]),
          f"实得 {[(i['id'], i['answerable'], i['notice']) for i in b['items']]}")
    check("上传题库的 verified 恒为 False 且写明阻碍项",
          m["verified"] is False and m["verified_blockers"], "内容核验不在本项目范围内，必须如实亮出")
    check("篇章切句与题空登记到位（Q3 依赖 [3] 空位）",
          b["passages"][1]["blanks"] == [3] and len(b["passages"][0]["sentences"]) >= 4,
          f"实得 blanks={b['passages'][1]['blanks']}")
    check("答案保留（供改卷对照）", m["answered"] == 5, f"实得 answered={m['answered']}")


def run_rejects() -> None:
    print("\n--- error 即整卷拒绝 ---")

    b, errs = ingest(minimal(meta={"subject": "x", "specs": ["english"]}))
    check("缺 meta.title → 拒绝", b is None and "upload_item_invalid" in codes(errs),
          f"实得 b={'None' if b is None else 'bank'} codes={codes(errs)}")

    b, errs = ingest(minimal(meta={"title": "t", "subject": "s", "specs": ["nope"]}))
    check("meta.specs 指向不存在的 spec → 拒绝（spec_unknown）",
          b is None and "spec_unknown" in codes(errs), f"实得 {codes(errs)}")

    bad = minimal()
    bad["items"][0]["passage"] = "P99"
    b, errs = ingest(bad)
    check("条目引用不存在的篇章 → 拒绝（passage_unknown）",
          b is None and "passage_unknown" in codes(errs), f"实得 {codes(errs)}")

    bad = minimal()
    bad["items"][0]["options"] = {"A": "a", "B": "b", "C": "c", "H": "h"}
    b, errs = ingest(bad)
    check("选项字母超出 A–G → 拒绝（options_letters_invalid）",
          b is None and "options_letters_invalid" in codes(errs), f"实得 {codes(errs)}")

    bad = minimal()
    bad["items"][0]["stem"] = 123
    b, errs = ingest(bad)
    check("stem 不是字符串 → 拒绝（不静默转型）",
          b is None and "upload_item_invalid" in codes(errs), f"实得 {codes(errs)}")

    b, errs = ingest({"meta": {"title": "t", "subject": "s", "specs": ["english"]},
                      "items": []})
    check("空 items → 拒绝", b is None and "upload_item_invalid" in codes(errs),
          f"实得 {codes(errs)}")

    b, errs = ingest(minimal(), name="t.json")
    check("最小合法题库本身通过（对照组）", b is not None, f"实得 {codes(errs)}")


def run_math_kind() -> None:
    print("\n--- 数学卷（无兜底 spec）：kind 必须被认领 ---")

    base = {
        "meta": {"title": "数学", "subject": "数学", "specs": ["math", "math_multi"]},
        "items": [{"no": 1, "stem": "已知 $x>0$，求最小值",
                   "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                   "kind": "math_single", "answer": "B"}],
    }
    b, errs = ingest(base)
    check("给了 kind=math_single → 通过", b is not None, f"实得 {codes(errs)}")

    nokind = copy.deepcopy(base)
    del nokind["items"][0]["kind"]
    b, errs = ingest(nokind)
    check("缺 kind → 拒绝（kind_unclaimed，逐条点名而不是悄悄套错编排）",
          b is None and "kind_unclaimed" in codes(errs), f"实得 {codes(errs)}")

    wrong = copy.deepcopy(base)
    wrong["items"][0]["kind"] = "mcq"
    b, errs = ingest(wrong)
    check("kind=mcq 在数学卷不被任何 spec 认领 → 同样拒绝",
          b is None and "kind_unclaimed" in codes(errs), f"实得 {codes(errs)}")


def run_warns() -> None:
    print("\n--- 告警随题库入库（可修复的问题不拒绝） ---")

    dup = minimal(items=[
        {"no": 1, "stem": "Which one is right?", "options": {"A": "a", "B": "b", "C": "c"} , "answer": "A"},
        {"no": 1, "stem": "Which one is right too?", "options": {"A": "a", "B": "b", "C": "c"}, "answer": "B"},
    ])
    b, errs = ingest(dup)
    check("重复题号 → 通过但记 duplicate_printed_no，两条都保留",
          b is not None and len(b["items"]) == 2
          and any(v["code"] == "duplicate_printed_no" for v in b["validations"]),
          f"实得 {codes(errs)} items={len(b['items']) if b else 0}")

    bad_ans = minimal(items=[
        {"no": 1, "stem": "Which one is right?", "options": {"A": "a", "B": "b", "C": "c"},
         "answer": "AZ"},
    ])
    b, errs = ingest(bad_ans)
    check("答案含不存在的字母 → 通过、忽略该字母、记告警",
          b is not None and b["items"][0]["standard_answer"] == "A"
          and any(v["code"] == "standard_answer_invalid" for v in b["validations"]),
          f"实得 answer={b['items'][0]['standard_answer'] if b else None} codes={codes(errs)}")

    unk = minimal()
    unk["items"][0]["kind"] = "unheard_of_kind"
    b, errs = ingest(unk)
    check("未知 kind → 通过（spec 兜底）但记 kind_unknown",
          b is not None and any(v["code"] == "kind_unknown" for v in b["validations"]),
          f"实得 {codes(errs)}")

    secl = minimal(sections=[{"name": "别的小节", "per": 2}])
    b, errs = ingest(secl)
    check("条目小节未声明 → 自动补建 + 记告警；声明了却零条目 → 计数不符告警",
          b is not None
          and any(v["code"] == "section_undeclared" for v in b["validations"])
          and any(v["code"] == "section_count_mismatch" for v in b["validations"]),
          f"实得 {codes(errs)}")

    nono = minimal(items=[{"stem": "Which one is right?",
                           "options": {"A": "a", "B": "b", "C": "c"}, "answer": "A"}])
    b, errs = ingest(nono)
    check("条目缺 no → 自动编号并记 info（不静默）",
          b is not None and b["items"][0]["printed_no"] == 1
          and any(v["code"] == "item_no_missing" for v in b["validations"]),
          f"实得 {b['items'][0]['printed_no'] if b else None} codes={codes(errs)}")


def run_degrade() -> None:
    print("\n--- 归一化只降级、不改写、不删条目 ---")

    noopt = minimal(items=[{"no": 1, "kind": "writing", "stem": "写一篇短文，谈谈你的看法。"}])
    b, errs = ingest(noopt)
    check("写作题（天然无选项）→ 保留条目、按性质不可作答，不判成缺损",
          b is not None and len(b["items"]) == 1 and not b["items"][0]["answerable"]
          and "文本" in (b["items"][0]["notice"] or "")
          and b["items"][0]["unparseable_reason"] is None,
          f"实得 {codes(errs)}")

    short = minimal(items=[{"no": 1, "stem": "短", "options": {"A": "a", "B": "b", "C": "c"}}])
    b, errs = ingest(short)
    check("题干过短 → 通过入库但记 stem_suspicious 告警（不删题）",
          b is not None and any(v["code"] == "stem_suspicious" for v in b["validations"]),
          f"实得 {codes(errs)}")

    three = minimal(items=[{"no": 1, "stem": "Which one is right?",
                            "options": {"A": "a", "B": "b", "C": "c"}, "answer": "C"}])
    b, errs = ingest(three)
    check("三选项题按题目自己的字母表判定，不被判成缺 D",
          b is not None and b["items"][0]["answerable"] is True
          and not any(v["code"] == "options_incomplete" for v in b["validations"]),
          f"实得 {codes(errs)}")

    loss = None
    if b is not None:
        loss = b["meta"]["loss"]
    check("损失账目：清单里出现的题号 == 保留条目（上传不丢题）",
          loss is not None and loss["seen_printed_nos"] == loss["kept"] == 1
          and loss["never_parsed"] == [] and loss["dropped"] == [],
          f"实得 {loss}")


def run_declared() -> None:
    print("\n--- 显式声明不可作答（旧信封迁移依赖它） ---")

    declared = minimal(items=[
        {"no": 1, "kind": "math_solution", "stem": "解答题（需完整推导，本工作台不可作答）",
         "answerable": False, "notice": "解答题需完整推导与书写，JEV 不生成推导文本"},
        {"no": 2, "kind": "math_single", "stem": "已知 $x>0$，求最小值",
         "options": {"A": "1", "B": "2", "C": "3", "D": "4"}, "answer": "B"},
    ])
    declared["meta"] = {"title": "数学", "subject": "数学", "specs": ["math", "math_multi"]}
    b, errs = ingest(declared)
    check("无选项但显式声明不可作答的条目不再判缺损，整卷照常入库",
          b is not None and b["items"][0]["answerable"] is False
          and "推导" in (b["items"][0]["notice"] or ""),
          f"实得 {codes(errs)}")
    check("不可作答条目的 kind 不必被任何 spec 认领（它根本不会被求解）",
          b is not None and not any(e["code"] == "kind_unclaimed" for e in errs),
          f"实得 {codes(errs)}")
    check("声明不可作答逐条登记（declared_unanswerable，info）",
          b is not None and any(v["code"] == "declared_unanswerable"
                                and v["severity"] == "info" for v in b["validations"]),
          f"实得 {[(v['code'], v['severity']) for v in b['validations']]}")
    check("可作答的数学条目仍受 kind 认领约束（对照）",
          b is not None and b["items"][1]["answerable"] is True, "对照组应当可作答")


def run_id_prefix() -> None:
    print("\n--- meta.id_prefix（轨迹认领的前缀） ---")

    b, errs = ingest(minimal(meta={"title": "t", "subject": "s", "specs": ["english"],
                                   "id_prefix": "M"}))
    check("合法前缀被接受并写进信封", b is not None and b["meta"].get("id_prefix") == "M",
          f"实得 {b['meta'].get('id_prefix') if b else codes(errs)}")
    b, errs = ingest(minimal(meta={"title": "t", "subject": "s", "specs": ["english"],
                                   "id_prefix": "MM"}))
    check("非法前缀（多字符）→ 拒绝", b is None and "upload_item_invalid" in codes(errs),
          f"实得 {codes(errs)}")
    b, errs = ingest(minimal())
    check("缺省前缀为 Q", b is not None and b["meta"].get("id_prefix") == "Q",
          f"实得 {b['meta'].get('id_prefix') if b else codes(errs)}")


def run_section_part() -> None:
    print("\n--- 小节声明缺 part 时按名字认领（不丢 per/full） ---")

    sec = minimal(
        sections=[{"name": "阅读理解", "per": 2, "count": 1}],
        items=[{"no": 1, "part": "一", "section": "阅读理解", "stem": "Which one is right?",
                "options": {"A": "a", "B": "b", "C": "c"}}])
    b, errs = ingest(sec)
    check("声明没写 part、条目写了 → 同名唯一即认领，per 保留",
          b is not None and b["sections"][0].get("per") == 2
          and b["items"][0]["section_id"] == b["sections"][0]["id"],
          f"实得 {b['sections'] if b else codes(errs)}")
    check("不产生 section_undeclared / count_mismatch 噪音",
          b is not None and not any(v["code"] in ("section_undeclared", "section_count_mismatch")
                                    for v in b["validations"]),
          f"实得 {codes(errs)}")


def run_id() -> None:
    print("\n--- 文件名 → 试卷 id ---")
    check("中文与普通字符保留", upload.paper_id_from_filename("期末模拟卷.json") == "期末模拟卷",
          f"实得 {upload.paper_id_from_filename('期末模拟卷.json')!r}")
    check("路径分隔符被替换（不允许借文件名写出目录）",
          upload.paper_id_from_filename("../../etc/evil.json") == "evil",
          f"实得 {upload.paper_id_from_filename('../../etc/evil.json')!r}")
    check("Windows 非法字符替换为下划线",
          upload.paper_id_from_filename('a:b*c?.json') == "a_b_c_",
          f"实得 {upload.paper_id_from_filename('a:b*c?.json')!r}")
    check("空名有兜底", upload.paper_id_from_filename(".json") == "bank",
          f"实得 {upload.paper_id_from_filename('.json')!r}")


def main() -> int:
    print("=" * 70)
    print("门禁：上传管线（模板契约 + 结构验收 + 归一化），不依赖任何本机题库")
    print("=" * 70)
    run_template()
    run_rejects()
    run_math_kind()
    run_warns()
    run_degrade()
    run_declared()
    run_id_prefix()
    run_section_part()
    run_id()
    if failures:
        print(f"\n失败 {len(failures)} 项：{failures}")
        return 1
    print("\n通过：模板与管线互相对得上；结构错误拒绝整卷，可修复问题全部入账，缺损只降级")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
