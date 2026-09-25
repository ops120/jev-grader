"""门禁 7：题库构造层 bank.py 的纯函数测试。

为什么单独钉这一层：五份题库现在**只有 bank.py 一个构造器**，
"答案可被质疑和复核" 这句话的全部技术含量都落在这里的十几个函数上。
它是纯函数，不需要任何题库文件就能跑 —— 所以在"仓库不含题库"的机器上
这道门禁依然真实生效（别处那些门禁只能打 SKIP）。

钉住的行为，逐条对应旧代码的真实缺陷：
  align_runs      双向局部对齐：编造要抓到、漏抄要抓到；并钉住它已知的限度
                  （单字替换在 min_len=2 下不可见 —— 这是限度不是 bug，但必须写出来）
  complete_item   缺损题**永不丢弃**；reason_override 与 domain_reason 不得混用
  renumber        卷面号留在 printed_no，内部号连续，重复号入账且不截断
  build_loss      见过的号 - 保留的号 - 显式 drop 的号 = never_parsed（静默丢题的会计式）
  validation      ids 永不截断（旧代码用 missing[:3] 让 JSON 系统性低估自己的失败面）
  render_valid.   未登记的 code 必须抛，报告里不允许出现无记录支撑的自由文本
  read_bank       读到 v1 / 无版本号的旧题库直接拒，不做字段猜测式兼容
  apply_verified  有 error、有未入账损失、缺独立核验 → 不允许 verified

跑法：python tests/test_bank.py     退出码 0 = 全绿
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import bank  # noqa: E402

failures: list[str] = []
limits: list[str] = []


def ok(name: str) -> None:
    print(f"  OK    {name}")


def fail(name: str, why: str) -> None:
    print(f"  FAIL  {name}：{why}")
    failures.append(name)


def check(name: str, cond: bool, why: str) -> None:
    ok(name) if cond else fail(name, why)


def limit(name: str, note: str) -> None:
    """已核实的行为限度：断言它确实如此（防止悄悄变化），并记录下来对外说明。"""
    limits.append(f"{name}：{note}")


# ---------------------------------------------------------------------------
def run_align() -> None:
    print("\n--- align_runs：双向局部对齐 ---")

    # 回归用例：归一化会把成串符号塌成同一个 '#'（公式密集处极常见）。
    # 等值段中间仍然可能是唯一可用的对齐起点，旧实现用"跳过相邻等值令牌"的跳转表
    # 直接跳过了它，于是整段转写都被报成"原文里没有"——假编造告警。
    # 旧实现在这个用例上得到 extra=[('#@',0,2)]（把整条转写报成编造），
    # 新实现把 ',b' 全部配上，只报原文真正多出来的部分。
    extra, missing = bank.align_runs(",b", "a,,,bc", min_len=2)
    check("等值段内的对齐起点不被跳过（跳转表回归：整段转写不再被误报为编造）",
          extra == [] and [(r["text"], r["len"]) for r in missing] == [("@##", 3)],
          f"转写 ',b' 是原文 'a,,,bc' 的子集，extra 应为空；实得 extra={extra} missing={missing}")

    # 镜像：等值段出现在转写侧
    extra, missing = bank.align_runs("a,,,bc", ",b", min_len=2)
    check("等值段出现在转写侧时同样只报真正多出的部分",
          missing == [] and [(r["text"], r["len"]) for r in extra] == [("@##", 3)],
          f"实得 extra={extra} missing={missing}")

    # 反向索引若只建一次、不随配对收缩，已消耗位置会被重复认领，整题直接静默通过。
    extra, missing = bank.align_runs("甲乙", "乙甲")
    check("完全调序不出现重复消耗（反向索引每轮重建）",
          [r["text"] for r in extra] == ["甲乙"] and [r["text"] for r in missing] == ["乙甲"],
          f"实得 extra={extra} missing={missing}")

    # 关键判据：宁多报、不静默。中间那个能配上的单字如果把两侧都缝住了，
    # 漏抄就彻底消失 —— 这条钉住"配对阈值降到 1"的方案必须被拒绝。
    extra, missing = bank.align_runs("乙", "甲乙丙")
    check("单个可对上的字不得把整段漏抄缝成静默",
          extra == [] and [r["text"] for r in missing] == ["甲乙丙"],
          f"转写只抄了「乙」，原文「甲乙丙」整段没抄进去，必须告警；实得 missing={missing}")

    # 编造必须抓到：转写里有一句原文没有的中文
    src = "根据上述材料，可以得出以下结论"
    xfr = "根据上述材料，众里寻他千百度可以得出以下结论"
    extra, missing = bank.align_runs(xfr, src, min_len=2)
    joined = "".join(r["text"] for r in extra)
    check("转写中编造的中文被报为独有片段",
          "众里寻他千百度" in joined and missing == [],
          f"实得 extra={extra} missing={missing}")

    # 漏抄必须抓到：反向
    extra, missing = bank.align_runs(src, xfr, min_len=2)
    joined = "".join(r["text"] for r in missing)
    check("原文中存在而转写缺失的中文被报为漏抄",
          "众里寻他千百度" in joined and extra == [],
          f"实得 extra={extra} missing={missing}")

    # 顺序错乱：实测**抓不到**（贪心按最长公共子串配对，跨半卷调序仍算对齐）。
    # 这是内容级检查，不是顺序级检查 —— 钉住现状并对外声明，别让它躲在"双向比对"这句话里。
    extra, missing = bank.align_runs("可以得出以下结论根据上述材料",
                                     "根据上述材料，可以得出以下结论", min_len=2)
    check("整块调序的行为被钉住（当前不可见）", extra == [] and missing == [],
          f"实得 extra={extra} missing={missing}")
    if extra == [] and missing == []:
        limit("align_runs 只比内容不比顺序",
              "两个大段落互换位置仍算对齐；它检查「有没有编造/漏抄内容」，不检查「抄对顺序」")

    # 公式：令牌归一化把字母与数字一律塌成 '@'，所以**只有形状在检查范围内，内容不在**。
    shape_same = (bank.normalize_for_compare(r"$AB=\sqrt{2}$")
                  == bank.normalize_for_compare(r"$AB=\sqrt{3}$"))
    check("公式内容（数字/函数名）在令牌归一化下的可见性被钉住",
          shape_same is True,
          f"实得 {bank.normalize_for_compare(r'$AB=\\sqrt{2}$')!r} vs "
          f"{bank.normalize_for_compare(r'$AB=\\sqrt{3}$')!r}")
    if shape_same:
        limit("公式写错查不出来（文本层比对的天然盲区）",
              r"$\sqrt{2}$→$\sqrt{3}$、$\sin$→$\cos$ 归一化后逐字符相同；"
              "公式只能靠「存在性」（extract 的墨迹检查）与人工复核兜底")

    # 单字替换：min_len=2 下被当噪声滤掉，实测不可见。
    # 这正是 KNOWN_TEXT_LAYER_DEFECTS 台账（疾病/乘病）必须**人工**逐条写的根本原因——
    # 自动比对着它瞎，所以台账不能由自动比对代替。
    extra, missing = bank.align_runs("已知疾病的发生", "已知疾并的发生", min_len=2)
    check("单字替换的行为被钉住（当前不可见）", extra == [] and missing == [],
          f"实得 extra={extra} missing={missing}")
    if extra == [] and missing == []:
        limit("align_runs(min_len=2) 看不见一字之差",
              "「疾病→疾并」这类单字替换会被配成对；需要逐字符比对或人工台账兜底")

    # keep_math=False 只比文字层：公式段被剥掉
    a = bank.normalize_for_compare("设 $x>0$，求最大值", keep_math=False)
    b = bank.normalize_for_compare("设 ，求最大值")
    check("keep_math=False 剥掉 $...$ 公式段", a == b, f"{a!r} != {b!r}")


# ---------------------------------------------------------------------------
def item(**kw) -> dict:
    base = dict(no=1, printed_no=1, kind="mcq", stem="下面说法正确的是哪一项",
                options={"A": "甲", "B": "乙", "C": "丙", "D": "丁"})
    base.update(kw)
    return base


def run_complete() -> None:
    print("\n--- complete_item：缺损只降级、永不丢弃 ---")

    it, vs = bank.complete_item(item(), allowed="ABCD")
    check("完好题目可作答",
          it["answerable"] is True and it["unparseable_reason"] is None and vs == [],
          f"answerable={it['answerable']} validations={[v['code'] for v in vs]}")

    it, vs = bank.complete_item(item(options={"A": "甲"}), allowed="ABCD")
    check("只有 1 个选项 → 保留条目 + answerable=False + error 记录",
          isinstance(it, dict) and it["answerable"] is False and it["notice"]
          and any(v["severity"] == "error" for v in vs),
          f"实得 answerable={it.get('answerable')} vs={vs}")
    it, vs = bank.complete_item(item(options={}), allowed="ABCD")
    check("0 选项题目仍产出条目并给出原因",
          it["answerable"] is False and bool(it["notice"])
          and any(v["code"] in ("options_are_images", "options_incomplete") for v in vs),
          f"实得 {it}")

    it, vs = bank.complete_item(item(kind="judge", options={"A": "正确", "B": "错误"}),
                                allowed="ABCD")
    check("判断题两选项（正确/错误）就是完整的：不受「少于 3」「缺 C/D」双重误杀",
          it["answerable"] is True and vs == [],
          f"answerable={it.get('answerable')} notice={it.get('notice')!r} vs={[v['code'] for v in vs]}")

    it, vs = bank.complete_item(item(kind="judge", options={"A": "正确"}), allowed="ABCD")
    check("判断题只剩 1 个选项 → 仍是缺损（下限收窄到 2，不是放开）",
          it["answerable"] is False and any(v["severity"] == "error" for v in vs),
          f"实得 answerable={it.get('answerable')} vs={[v['code'] for v in vs]}")

    # reason_override vs domain_reason —— 这次迁移里最容易搞错的一对
    it, vs = bank.complete_item(item(), allowed="ABCD", reason_override="卷面被遮挡")
    check("reason_override（解析缺损）写进 unparseable_reason → 计入损失账目",
          it["unparseable_reason"] == "卷面被遮挡" and it["answerable"] is False,
          f"实得 {it['unparseable_reason']!r} / {it['answerable']}")

    it, vs = bank.complete_item(item(), allowed="ABCD",
                                domain_reason="听力题需音频材料，无法作答")
    check("domain_reason（本卷性质）只写 notice，不占损失账目",
          it["unparseable_reason"] is None and it["answerable"] is False
          and it["notice"] == "听力题需音频材料，无法作答",
          f"实得 unparseable_reason={it['unparseable_reason']!r} notice={it['notice']!r}")

    it, vs = bank.complete_item(item(options={"A": "甲"}), allowed="ABCD",
                                domain_reason="写作题需生成文本")
    check("领域性不可作答不得掩盖真实缺损：error 记录仍然产出",
          any(v["severity"] == "error" for v in vs) and it["answerable"] is False,
          f"实得 vs={[v['code'] for v in vs]}")

    # 字母表：拿 ABCD 要求听力题 = 把完好的题判成缺损（这次迁移真踩过的坑）
    it, vs = bank.complete_item(item(options={"A": "a", "B": "b", "C": "c"}),
                                allowed="ABC", min_options=3)
    check("按卷面实际字母表判定：A–C 的听力题不被误判为缺 D",
          it["answerable"] is True and vs == [],
          f"实得 answerable={it['answerable']} vs={[(v['code'], v['detail']) for v in vs]}")
    it, vs = bank.complete_item(item(options={"A": "a", "B": "b", "C": "c"}),
                                allowed="ABCD", min_options=3)
    check("同一道题用错字母表就会误判（记录这个反例的成因）",
          it["answerable"] is False,
          "allowed 传错时应当报错 —— 这条钉住'听力缺 D'假告警的来源")

    it, vs = bank.complete_item(item(stem=""), allowed="ABCD")
    check("空题干 → error 记录",
          any(v["code"] == "stem_suspicious" and v["severity"] == "error" for v in vs),
          f"实得 {vs}")


# ---------------------------------------------------------------------------
def run_renumber_loss() -> None:
    print("\n--- renumber / build_loss：卷面号与损失会计 ---")

    raw = [item(no=5, printed_no=5), item(no=7, printed_no=7), item(no=7, printed_no=7)]
    items, vs = bank.renumber(raw, id_prefix="Q")
    check("重编号后 no/id 连续而 printed_no 保留卷面号",
          [i["no"] for i in items] == [1, 2, 3] and [i["id"] for i in items] == ["Q1", "Q2", "Q3"]
          and [i["printed_no"] for i in items] == [5, 7, 7],
          f"实得 {[(i['no'], i['id'], i['printed_no']) for i in items]}")
    check("卷面号与序号错位时写 seq_note",
          all(i["seq_note"] for i in items),
          f"实得 {[i['seq_note'] for i in items]}")
    check("重复卷面号产出记录且列出全部号",
          any(v["code"] == "duplicate_printed_no" and v["ids"] == [7] for v in vs),
          f"实得 {vs}")

    kept = [item(no=1, printed_no=1), item(no=2, printed_no=2), item(no=3, printed_no=3)]
    loss = bank.build_loss([1, 2, 3, 4, 5], kept, dropped=[{"printed_no": 4, "reason": "选项未抽出"}])
    check("见过 - 保留 - 显式 drop = never_parsed（静默丢题的会计式）",
          loss["never_parsed"] == [5] and loss["seen_printed_nos"] == 5 and loss["kept"] == 3,
          f"5 号既没条目也没入账，必须暴露在 never_parsed；实得 {loss['never_parsed']}")
    check("显式 drop 的号不进 never_parsed（它已入账，不重复计），但 loss 仍算不干净",
          4 not in loss["never_parsed"] and bank.loss_is_clean(loss) is False,
          f"实得 never_parsed={loss['never_parsed']} clean={bank.loss_is_clean(loss)}")
    check("loss_is_clean 对空账目返回 True",
          bank.loss_is_clean({"dropped": [], "never_parsed": []}) is True, "不该有阻碍")

    n = bank.validation("never_parsed", severity="error", ids=list(range(1, 501)))
    check("validation.ids 永不截断（旧代码 missing[:3] 让账面低估失败面）",
          len(n["ids"]) == 500 and n["count"] == 500,
          f"实得 len={len(n['ids'])} count={n['count']}")


# ---------------------------------------------------------------------------
def registered_codes() -> list[str]:
    """从 validation() 的 docstring 里读出「已登记」的 code 清单。"""
    src = Path(bank.__file__).read_text(encoding="utf-8")
    body = src.split("需在此登记", 1)[1].split('"""', 1)[0]
    return re.findall(r"^\s{6}([a-z_]+)\s", body, re.M)


def run_render() -> None:
    print("\n--- render_validation：只有记录才有句子 ---")

    codes = registered_codes()
    check("能从 validation() 文档读到登记的 code 清单",
          len(codes) >= 10, f"只解析出 {codes}")
    bad = []
    for c in codes:
        try:
            bank.render_validation(bank.validation(c, severity="warn", item_id="Q1",
                                                   printed_no=1, detail="x",
                                                   ids=[1], expected=3, actual=1))
        except KeyError as e:
            bad.append(f"{c}: {e}")
    check("文档里登记的每个 code 都能渲染",
          not bad, f"未登记于 render_validation：{bad}")

    # 反方向更要命：解析器发了一个没登记的 code，不触发时静默、一触发就让
    # render_validation 抛 KeyError，整份题库生成失败。transcription_unmatched
    # 就是这么漏进去的——所以扫源码，不靠人记性。
    emitted: dict[str, list[str]] = {}
    for p in sorted(Path(bank.__file__).resolve().parent.glob("**/*.py")):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"bank\.validation\(\s*[\"']([a-z_]+)[\"']", text):
            emitted.setdefault(m.group(1), []).append(
                f"{p.name}:{text[:m.start()].count(chr(10)) + 1}")
    unregistered = {c: where for c, where in emitted.items() if c not in codes}
    check("源码里发出的每个 code 都已登记（扫 src/**，不靠人记性）",
          not unregistered,
          f"未登记于 validation() 清单与 render_validation：{unregistered}")
    check("扫描确实认到了发出点（否则上一条是空转）",
          len(emitted) >= 12, f"只扫到 {len(emitted)} 个 code：{sorted(emitted)}")

    try:
        bank.render_validation(bank.validation("totally_made_up_code", detail="x"))
    except KeyError:
        ok("未登记的 code 抛 KeyError（报告里不会出现无记录支撑的话）")
    except Exception as e:  # noqa: BLE001
        fail("未登记 code 的异常类型", f"抛的是 {type(e).__name__}，期望 KeyError")
    else:
        fail("未登记 code 必须抛", "静默返回了")

    lines = bank.render_validations([
        bank.validation("never_parsed", severity="error", ids=[3, 9]),
        bank.validation("never_parsed", severity="error", ids=[3, 9])])
    check("相同记录只渲染一行", len(lines) == 1, f"实得 {lines}")


# ---------------------------------------------------------------------------
def run_identity_audit() -> None:
    print("\n--- audit_validations：报告指着的题号必须是真的 ---")

    items, _ = bank.renumber([item(printed_no=76), item(printed_no=77)], id_prefix="X")
    good = bank.validation("options_incomplete", severity="error",
                           item_id="X1", printed_no=76)
    check("卷面号与条目一致的记录 → 不产出任何稽核记录",
          bank.audit_validations(items, [good]) == [],
          f"实得 {bank.audit_validations(items, [good])}")

    bad = bank.validation("options_incomplete", severity="error",
                          item_id="X1", printed_no=1)
    out = bank.audit_validations(items, [bad])
    check("记录自称的卷面号与它指向的条目不符 → error 级 identity_mismatch",
          len(out) == 1 and out[0]["code"] == "identity_mismatch" and out[0]["severity"] == "error",
          f"实得 {out}")
    check("稽核记录里同时写明两个号（人能一眼看出漂在哪）",
          "X1" in out[0]["detail"] and "76" in out[0]["detail"],
          f"实得 detail={out[0]['detail']!r}")

    ghost = bank.validation("stem_suspicious", severity="error",
                            item_id="X999", printed_no=5)
    out = bank.audit_validations(items, [ghost])
    check("记录指向一个不存在的条目 → 同样必须报",
          len(out) == 1 and out[0]["code"] == "identity_mismatch", f"实得 {out}")

    # 严重度分档：只有"报告指着不存在的题号说话"这一种可能是合法登记
    # （针对被丢弃题目的台账），所以它是 warn；另两种一定是接线错，必须是 error。
    orphan = bank.validation("source_defect", severity="info", printed_no=999)
    out = bank.audit_validations(items, [orphan])
    check("没有条目可指向的卷面号 → warn 而非 error（台账可以合法点名被丢弃的题）",
          len(out) == 1 and out[0]["severity"] == "warn", f"实得 {out}")
    out = bank.audit_validations(items, [good, bad, ghost])
    check("两种接线错仍是 error 级（会挡住 verified）",
          sum(1 for o in out if o["severity"] == "error") == 2, f"实得 {[(o['severity']) for o in out]}")

    dup = [bad, bad, bad]
    check("同一处身份漂移只报一条（不淹没报告，但 ids 类截断不适用）",
          len(bank.audit_validations(items, dup)) == 1,
          f"实得 {len(bank.audit_validations(items, dup))} 条")

    done = [dict(i, answerable=True, unparseable_reason=None, notice="") for i in items]
    b = bank.build_bank(subject="测试", source="t.pdf", items=done, sections=[],
                        loss=bank.build_loss([76, 77], done), validations=[bad])
    check("带身份漂移的题库不允许 verified",
          b["meta"]["verified"] is False
          and any(v["code"] == "identity_mismatch" for v in b["validations"]),
          f"实得 verified={b['meta']['verified']}")
    check("身份漂移必须渲染得出（新 code 已在 render 表登记）",
          any("identity_mismatch" in (v.get("code") or "") for v in b["validations"])
          and bool(bank.render_validation(b["validations"][-1])), "渲染路径未打通")


# ---------------------------------------------------------------------------
def run_envelope() -> None:
    print("\n--- build_bank / apply_verified / read_bank ---")

    items, _ = bank.renumber([item(options={}, printed_no=1)], id_prefix="Q")
    done, vs = bank.complete_item(items[0], allowed="ABCD")
    loss = bank.build_loss([1], [done])
    b = bank.build_bank(subject="测试", source="t.pdf", items=[done], sections=[],
                        loss=loss, validations=vs, method="m")
    check("信封字段齐备且 bank_version=2",
          b["meta"]["bank_version"] == 2 and "loss" in b["meta"] and "report" in b,
          f"实得 {sorted(b['meta'])}")
    check("verified 在无独立核验时为 False 且给出阻碍项",
          b["meta"]["verified"] is False and b["meta"]["verified_blockers"],
          f"实得 {b['meta'].get('verified')} / {b['meta'].get('verified_blockers')}")
    check("有 error 记录时阻碍 verified 并写明条数",
          any("error" in r for r in b["meta"]["verified_blockers"]),
          f"实得 {b['meta']['verified_blockers']}")

    clean = [dict(done, options={"A": "a", "B": "b", "C": "c"}, answerable=True,
                  unparseable_reason=None, notice="")]
    b2 = bank.build_bank(subject="测试", source="t.pdf", items=clean, sections=[],
                         loss=bank.build_loss([1], clean), validations=[])
    check("干净但未核验的题库 verified=False", b2["meta"]["verified"] is False, "不该自己盖章")
    b2["meta"]["verification"] = {"by": "extract/verify.py", "at": "x"}
    check("独立核验盖章 + 账目干净 → verified=True",
          bank.apply_verified(b2)["meta"]["verified"] is True,
          f"实得 {b2['meta'].get('verified_blockers')}")

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "v1.json"
        p.write_text(json.dumps({"meta": {"subject": "旧"}, "items": []}), encoding="utf-8")
        try:
            bank.read_bank(p)
        except bank.BankVersionError:
            ok("无版本号的旧题库被拒（不做字段猜测式兼容）")
        except Exception as e:  # noqa: BLE001
            fail("旧题库拒绝方式", f"抛的是 {type(e).__name__}")
        else:
            fail("旧题库必须被拒", "静默放行了")
        p2 = Path(tmp) / "v9.json"
        p2.write_text(json.dumps({"meta": {"bank_version": 9}}), encoding="utf-8")
        check("未来版本号同样被拒（不是只认得 1）",
              _raises(bank.BankVersionError, bank.read_bank, p2), "版本号向前不兼容时必须停下来")
        check("allow_v1 可以显式绕行",
              not _raises(bank.BankVersionError, bank.read_bank, p, allow_v1=True), "绕行参数失效")


def _raises(exc, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 70)
    print("门禁 7：bank.py 纯函数测试（不依赖任何题库文件）")
    print("=" * 70)
    run_align()
    run_complete()
    run_renumber_loss()
    run_render()
    run_identity_audit()
    run_envelope()

    if limits:
        print("\n已核实的限度（写进文档，不当成已通过）：")
        for s in limits:
            print(f"  · {s}")
    if failures:
        print(f"\n失败 {len(failures)} 项：{failures}")
        return 1
    print("\n通过：题库构造层的缺损永不静默、账目永不截断、报告永不脱离记录")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
