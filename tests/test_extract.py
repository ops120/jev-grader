"""门禁 8：抽取层（抄题定位 + 纯代码验收）的行为测试。

为什么必须有这道门禁：`verify.py` 是整个「答案可被质疑和复核」主张的盖章处，
而它在 S5 之前**一次都没有被执行过**——第一次真跑就暴露出一个不存在的参数名
（`align_runs(min_run=…)`）和一处二次归一化。一个从未执行过的验收器，
等于题库上盖着一张印好的公章。

本门禁不联网、不依赖任何真题文件：fixture PDF 用 PyMuPDF 现造，
所以 clone 之后依然真实生效（这一点和门禁 7 同规格）。

钉住的行为：
  scanned_printed_nos  卷面题号只能从原始文本层数出来（用错正则会让损失账目空转）
  pin_probes           probe 定位失败/重复/缺失必须留痕，条目降级而不是被丢掉
  slice_page_rects     双栏版面按列切，右栏条目不得把左栏正文框进自己的区域
  verify_item          编造要抓到（transcription_extra）、漏抄要抓到（…_missing）
  verify_item          页码越界 / 区域退化 → 该条目 error，但**整轮不得中断**
  verify_draft         静默丢题 → never_parsed；台账未命中 → error 且附实测差异
  apply_verified       以上任何 error 都挡住 verified
  render_bank_report   报告里每一句「第 N 题」都必须真的指向第 N 题

跑法：python tests/test_extract.py     退出码 0 = 全绿
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import fitz                                                   # noqa: E402

import bank                                                   # noqa: E402
from extract import geometry as G                             # noqa: E402
from extract import transcribe as T, verify as V              # noqa: E402

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
    limits.append(f"{name}：{note}")


# ---------------------------------------------------------------------------
# fixture：一张版式已知、内容完全可控的考卷
# ---------------------------------------------------------------------------
L1 = "1. 已知一个三角形的三个内角之和为一百八十度 则第三个角是"
L2 = "A．三十度  B．四十度  C．五十度  D．六十度"
L3 = "2. 某公司去年营收增长率为百分之十二 今年预计增长八个百分点则今年为"
L4 = "A．一点零八倍  B．一点二倍  C．一点九倍  D．二点零倍"
L5 = "3. 下列关于安全生产的说法中正确的是哪一个以及它的适用情形是什么"
L6 = "A．正确无误  B．错误百出"
L7 = "4. 第四题只印在卷面上 抄题端点把它漏了且没有交代"
# 两栏文字必须各自留在自己一半页面里（左栏起 x=40，须在 x≈300 前结束；
# 右栏起 x=320）。早先左栏那行写得太长、尾字越过 320，两栏在 x 上交叠——
# 那样「按几何分列」就无从谈起，任何实现都只能靠猜。
COL_L = "5. 左栏第五题题干写在页面左半部分"
COL_R = "6. 右栏第六题题干写在页面右半部分"
# 短题干 + 宽选项行：题干行比选项行窄。若区域横向边界只拿题干行宽当尺子，
# 行尾选项会被切出区域，模型逐字照抄也被报成"发明选项字母"（真跑 smoke 时抓到的）。
L8 = "8. 下列说法正确的一项"
L9 = "A．甲乙丙丁戊  B．己庚辛壬  C．癸子丑  D．寅卯辰巳"
# 页首说明文字：把文本层垫到正常文本页规模（≥150 字符，超过 SCANNED_PAGE_CHARS），
# 同时演示「页首的与题无关内容不会进入题目区域」——它在本题锚点上方。
L8_HEAD = ("（本页说明：本段文字仅用于让文本层达到正常文本页的字符规模。"
           "扫描页与文本页的判别按页内字符数进行，阈值之外的短页一律按扫描件处理。"
           "真正的题目区域从本题锚点开始，不会包含本行以上的任何内容。"
           "本段同时用于验证：页首与题目无关的内容不会进入任何一道题的区域，"
           "也不会被当成任何一道题的漏抄或编造，区域判据只认锚点与纵向跨度。）")
L8_HEAD_LINES = [L8_HEAD[i:i + 58] for i in range(0, len(L8_HEAD), 58)]

STEM1, OPT1 = L1[3:], {"A": "三十度", "B": "四十度", "C": "五十度", "D": "六十度"}
STEM2, OPT2 = L3[3:], {"A": "一点零八倍", "B": "一点二倍", "C": "一点九倍", "D": "二点零倍"}
STEM3, OPT3 = L5[3:], {"A": "正确无误", "B": "错误百出"}


def build_fixture(path: Path) -> None:
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    for y, text in ((60, L1), (80, L2), (120, L3), (140, L4),
                    (180, L5), (200, L6), (240, L7)):
        p.insert_text((40, y), text, fontname="china-s", fontsize=11)
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((40, 60), COL_L, fontname="china-s", fontsize=11)
    p2.insert_text((320, 60), COL_R, fontname="china-s", fontsize=11)
    p3 = doc.new_page(width=595, height=842)
    y = 50
    for ln in L8_HEAD_LINES:
        p3.insert_text((40, y), ln, fontname="china-s", fontsize=8)
        y += 12
    p3.insert_text((40, y + 8), L8, fontname="china-s", fontsize=12)
    p3.insert_text((40, y + 38), L9, fontname="china-s", fontsize=12)
    doc.save(str(path))
    doc.close()


def entry(no: int, page: int, probe: str, stem: str, opts: dict, **kw) -> dict:
    return {"printed_no": no, "page": page, "kind": kw.pop("kind", "single"),
            "probe": probe, "stem": stem, "options": opts,
            "section_name": "一、选择题", **kw}


BASE_ENTRIES = [
    entry(1, 1, "已知一个三角形的三个内角之和", STEM1, OPT1),
    entry(2, 1, "某公司去年营收增长率为",
          STEM2 + "外加一句原文里绝对没有的胡话", OPT2),
    entry(3, 1, "下列关于安全生产的说法中", STEM3, OPT3),
]


def make_draft(pdf: Path, entries: list[dict], dropped: list[dict] | None = None
               ) -> tuple[dict, list[dict], dict]:
    """entries → 带 provenance 的 v2 草稿（transcribe 的离线那半段）。"""
    doc = fitz.open(str(pdf))
    try:
        items, pin_vs, stats = T.pin_probes([dict(e) for e in entries], doc)
        draft = T.to_draft(items, dropped or [], doc, subject="fixture",
                           source=pdf.name, model="fixture-model")
    finally:
        doc.close()
    draft["validations"] = list(draft.get("validations") or []) + pin_vs
    return draft, pin_vs, stats


def codes_for(result: dict, printed_no: int | None = None) -> list[str]:
    return [v["code"] for v in result["validations"]
            if printed_no is None or v.get("printed_no") == printed_no]


def errs(result: dict, code: str) -> list[dict]:
    return [v for v in result["validations"]
            if v["code"] == code and v["severity"] == "error"]


# ---------------------------------------------------------------------------
def run_scan_and_loss(pdf: Path) -> None:
    print("\n--- 卷面题号与损失账目：防静默丢题的地基 ---")
    doc = fitz.open(str(pdf))
    try:
        seen = T.scanned_printed_nos(doc)
    finally:
        doc.close()
    check("题号只从原始文本层数出来（用模板正则会恒得空集）",
          seen == [1, 2, 3, 4, 5, 6, 8], f"实得 {seen}")

    draft, _, _ = make_draft(pdf, BASE_ENTRIES)
    loss = draft["meta"]["loss"]
    check("to_draft 必须给条目回填 section_id（漏了它左栏导航渲染为空，界面只剩一道题）",
          all(it.get("section_id") for it in draft["items"])
          and any(s.get("id") for s in draft["sections"]),
          f"实得 section_id={[it.get('section_id') for it in draft['items']]}")
    check("卷面 7 题（第 4/5/6/8 题未交）→ 差额必须整批进 never_parsed 而不是消失",
          loss["never_parsed"] == [4, 5, 6, 8], f"实得 {loss}")
    vs = bank.validation("never_parsed", severity="error", ids=[4], detail="x")
    check("draft 里确有 never_parsed 记录（build_bank 自己补的）",
          any(v["code"] == "never_parsed" for v in draft["validations"]),
          f"实得 {codes_for(draft)}")
    check("损失账目不让 verified 点亮",
          draft["meta"].get("verified") is False,
          f"meta.verified={draft['meta'].get('verified')}")

    dropped = [{"printed_no": 4, "reason": "该题选项为图形，无法文本解析"}]
    d2, _, _ = make_draft(pdf, BASE_ENTRIES, dropped)
    check("显式交代过的丢弃（第 4 题）→ 不再算静默丢失，但仍留一条 draft_dropped warn；"
          "没交代的 5/6/8 仍是静默丢失",
          d2["meta"]["loss"]["never_parsed"] == [5, 6, 8]
          and any(v["code"] == "draft_dropped" for v in d2["validations"]),
          f"实得 loss={d2['meta']['loss']} codes={codes_for(d2)}")


def run_pin(pdf: Path) -> None:
    print("\n--- probe 定位：模型交回的锚点必须真的在纸上 ---")
    draft, pin_vs, stats = make_draft(pdf, BASE_ENTRIES)
    check("三条锚点全部定位成功", stats["pinned"] == 3, f"实得 {stats}")
    prov = [i.get("provenance") for i in draft["items"]]
    check("每条都带上了页码与区域", all(p and p.get("rect") for p in prov),
          f"实得 {prov}")
    check("区域由代码推导而非模型自报（对外可说明它是物证）",
          all(p.get("rect_derived") is True for p in prov), f"实得 {prov}")
    ys = [p["rect"][1] for p in prov]
    check("同页三条目的区域按卷面顺序互不重叠",
          ys == sorted(ys) and all(b - a > 10 for a, b in zip(ys, ys[1:])),
          f"实得顶边 {ys}")

    bad = [entry(1, 1, "已知一个三角形的三个内角之和", STEM1, OPT1),
           entry(9, 1, "这段汉字在整份卷子上根本不存在", "编造的题干内容若干字", {"A": "甲"})]
    d2, vs2, _ = make_draft(pdf, bad)
    check("锚点找不到 → probe_not_found 记录",
          any(v["code"] == "probe_not_found" and v["printed_no"] == 9 for v in vs2),
          f"实得 {[(v['code'], v.get('printed_no')) for v in vs2]}")
    lost = next(i for i in d2["items"] if i.get("printed_no") == 9)
    check("锚点找不到的条目 → 降级为不可作答，但条目绝不消失",
          lost.get("answerable") is False and "钉不住" in (lost.get("notice") or ""),
          f"实得 answerable={lost.get('answerable')} notice={lost.get('notice')!r}")
    check("降级同时留一条 probe_unpinned",
          any(v["code"] == "probe_unpinned" and v["printed_no"] == 9 for v in d2["validations"]),
          f"实得 {codes_for(d2, 9)}")

    dup = [entry(1, 1, "已知一个三角形的三个内角之和", STEM1, OPT1),
           entry(2, 1, "已知一个三角形的三个内角之和", STEM2, OPT2)]
    d3, vs3, _ = make_draft(pdf, dup)
    check("两条目锚点落在同一处 → probe_duplicate，且两条都不比对",
          any(v["code"] == "probe_duplicate" for v in vs3) and len(d3["items"]) == 2,
          f"实得 {[(v['code'], v.get('printed_no')) for v in vs3]}")

    noprobe = [entry(1, 1, "", STEM1, OPT1)]
    _, vs4, _ = make_draft(pdf, noprobe)
    check("没交 probe → probe_missing（而不是默默按整页比对）",
          any(v["code"] == "probe_missing" for v in vs4),
          f"实得 {[(v['code'], v.get('printed_no')) for v in vs4]}")


def run_columns(pdf: Path) -> None:
    print("\n--- 双栏版面：区域不能横跨两栏 ---")
    entries = [entry(5, 2, COL_L[3:], COL_L[3:], {}),
               entry(6, 2, COL_R[3:], COL_R[3:], {})]
    doc = fitz.open(str(pdf))
    try:
        stream, boxes = G.page_char_stream(doc[1])
        a5 = G.rect_union(boxes[stream.find("左栏第五题"):stream.find("左栏第五题") + 6])
        a6 = G.rect_union(boxes[stream.find("右栏第六题"):stream.find("右栏第六题") + 6])
        rects, failed = G.slice_page_rects(doc[1], [(5, a5), (6, a6)])
    finally:
        doc.close()
    check("两栏各自成区：右栏区域的左边界必须落在左栏右边界的右侧",
          rects[6][0] > rects[5][2] - 60.0,
          f"左栏 {rects[5]} 右栏 {rects[6]}")
    r6 = rects[6]
    left_glyphs = [b for b in boxes if b[2] <= 300]
    inside = [b for b in left_glyphs
              if b[0] < r6[2] and b[2] > r6[0] and b[1] < r6[3] and b[3] > r6[1]]
    check("右栏区域不含左栏任何字形（逐字验证矩形包含，不是只看区域边缘）",
          not inside and r6[0] > 300,
          f"{len(inside)} 个左栏字形落进右栏区域 {r6}（页面宽 595，左栏起于 x=40）")
    check("锚点未落在同一行同一列时不误报重复", failed == [], f"实得 {failed}")


def run_verify(pdf: Path) -> None:
    print("\n--- 验收器：编造与漏抄都要抓到，且单条坏数据不能中断整轮 ---")
    draft, _, _ = make_draft(pdf, BASE_ENTRIES)
    result = V.verify_draft(draft, pdf)

    fake = [v for v in result["validations"]
            if v.get("printed_no") == 1
            and v["code"] in ("transcription_missing", "transcription_extra")]
    check("干净条目不再产生任何「漏抄/编造」记录（选项标号两侧保留、卷面题号不算漏抄后，"
          "逐字照抄的题应当一条都没有——修复前这里是恒真判据）",
          not fake, f"实得 {[(v['code'], v['severity'], v['detail'][:60]) for v in fake]}")
    clean = [v for v in result["validations"] if v.get("printed_no") == 1]
    check("第 1 题（逐字照抄）零 error 记录",
          not [v for v in clean if v["severity"] == "error"],
          f"实得 {[(v['code'], v['severity']) for v in clean]}")

    ex = [v for v in result["validations"]
          if v["code"] == "transcription_extra" and v.get("printed_no") == 2]
    check("第 2 题编造的 14 字被抓住且为 error 级",
          bool(ex) and all(v["severity"] == "error" for v in ex),
          f"实得 {[(v['code'], v['severity']) for v in ex]}")
    check("抓到的是**本题区域内**找不到（局部性），报告里带页码与区域",
          bool(ex) and "第 1 页" in ex[0]["detail"],
          f"实得 detail={ex[0]['detail'][:120] if ex else '无'}")
    check("编造告警的严重度落在第 2 题而不是别的题（身份不漂移）",
          all(v["item_id"] == "T2" for v in ex), f"实得 {[v['item_id'] for v in ex]}")

    omit = [entry(1, 1, "已知一个三角形的三个内角之和", STEM1, OPT1),
            entry(2, 1, "某公司去年营收增长率为", STEM2, OPT2),
            entry(3, 1, "下列关于安全生产的说法中", "3. 下列关于", OPT3)]
    d2, _, _ = make_draft(pdf, omit, [{"printed_no": 4, "reason": "自报丢弃"}])
    r2 = V.verify_draft(d2, pdf)
    miss = [v for v in r2["validations"]
            if v["code"] == "transcription_missing" and v.get("printed_no") == 3]
    check("原文有、转写里没有 → transcription_missing 抓到（漏抄方向）",
          bool(miss) and any("安全生产" in v["detail"] or "适用情形" in v["detail"]
                             for v in miss),
          f"实得 {[v['detail'][:80] for v in miss]}")

    broken = [entry(1, 99, "已知一个三角形的三个内角之和", STEM1, OPT1),
              entry(2, 1, "某公司去年营收增长率为", STEM2, OPT2),
              entry(3, 1, "下列关于安全生产的说法中", STEM3, OPT3),
              {"printed_no": 4, "page": 1, "kind": "single",
               "probe": "第四题只印在卷面上", "stem": "区域退化的一题",
               "options": {"A": "甲"}, "section_name": "一、选择题"}]
    d3, _, _ = make_draft(pdf, broken, [{"printed_no": 4, "reason": "自报丢弃"}])
    for it in d3["items"]:
        if it.get("printed_no") == 1:
            it["provenance"] = {"page": 99, "rect": [40, 50, 500, 70]}
        if it.get("printed_no") == 4:
            it["provenance"] = {"page": 1, "rect": [40, 60, 40, 60]}
    r3 = V.verify_draft(d3, pdf)
    check("页码越界不再打断整轮验收（该条目 error，其余照常比对）",
          len(r3["items"]) == 4
          and any(v["code"] == "provenance_unusable" and v.get("printed_no") == 1
                  for v in r3["validations"]),
          f"实得 {codes_for(r3)}")
    check("区域退化（零面积）同样报 provenance_unusable 而不是把整条转写判成编造",
          any(v["code"] == "provenance_unusable" and v.get("printed_no") == 4
              for v in r3["validations"])
          and not [v for v in r3["validations"]
                   if v.get("printed_no") == 4 and v["code"] == "transcription_extra"],
          f"实得 {[(v['code'], v.get('printed_no')) for v in r3['validations']]}")
    check("坏数据那一轮里其它条目仍然完成了比对",
          r3["meta"]["verification"]["items_with_provenance_checked"] >= 2,
          f"实得 {r3['meta']['verification']['items_with_provenance_checked']}")

    check("任何 error 都挡住 verified",
          result["meta"].get("verified") is False
          and bool(result["meta"].get("verified_blockers")),
          f"blockers={result['meta'].get('verified_blockers')}")


def run_allow_list(pdf: Path) -> None:
    print("\n--- 已知缺陷台账：只豁免它点名的那一段 ---")
    draft, _, _ = make_draft(pdf, BASE_ENTRIES)
    ex = V.verify_draft(draft, pdf)
    fabric = next(v for v in ex["validations"]
                  if v["code"] == "transcription_extra" and v.get("printed_no") == 2)
    m = re.search(r"转写独有 \d+ 字 '(.*)'｜", fabric["detail"])
    frag = m.group(1) if m else ""
    check("编造片段能从报告 detail 里原样取回（豁免登记要靠它逐字对上）",
          bool(frag) and "胡话" in frag, f"实得 frag={frag!r}")

    d2, _, _ = make_draft(pdf, BASE_ENTRIES)
    allow = [{"item_id": "T2", "printed_no": 2, "code": "transcription_extra",
              "expected_diff": frag, "evidence": "已核对原件：该处印的就是这句"}]
    r2 = V.verify_draft(d2, pdf, allow_entries=allow)
    still = [v for v in r2["validations"]
             if v["code"] == "transcription_extra" and v.get("printed_no") == 2
             and v["severity"] == "error"]
    check("与登记逐字吻合的差异被豁免（不再报 error）",
          not still, f"实得 {[v['detail'][:60] for v in still]}")
    check("豁免本身必须在报告里留痕（info 级 source_defect）",
          any(v["code"] == "source_defect" and v["severity"] == "info"
              and v.get("printed_no") == 2 for v in r2["validations"]),
          f"实得 {codes_for(r2, 2)}")

    d3, _, _ = make_draft(pdf, BASE_ENTRIES)
    bogus = [{"item_id": "T2", "printed_no": 2, "code": "transcription_extra",
              "expected_diff": "一段本次并不存在的差异", "evidence": "过期登记"}]
    r3 = V.verify_draft(d3, pdf, allow_entries=bogus)
    stale = [v for v in r3["validations"]
             if v["code"] == "source_defect" and v["severity"] == "error"]
    check("登记未命中 → error（台账不能变成永久消音器）",
          bool(stale), f"实得 {codes_for(r3)}")
    check("未命中的报错必须附上本次实测差异，否则复核者无从改登记",
          bool(stale) and "胡话" in stale[0]["detail"],
          f"实得 detail={stale[0]['detail'][:140] if stale else '无'}")

    d4, _, _ = make_draft(pdf, BASE_ENTRIES)
    cross = [{"item_id": "T1", "printed_no": 1, "code": "transcription_extra",
              "expected_diff": frag, "evidence": "试图用第 1 题豁免第 2 题"}]
    r4 = V.verify_draft(d4, pdf, allow_entries=cross)
    check("一条目的登记不能豁免另一条目的差异（跨条目不串）",
          any(v["code"] == "transcription_extra" and v.get("printed_no") == 2
              and v["severity"] == "error" for v in r4["validations"]),
          f"实得 {codes_for(r4, 2)}")


def run_short_stem(pdf: Path) -> None:
    print("\n--- 短题干 + 宽选项行：区域必须罩住整行选项 ---")
    entries = [entry(8, 3, "下列说法正确的一项", L8[3:],
                     {"A": "甲乙丙丁戊", "B": "己庚辛壬", "C": "癸子丑", "D": "寅卯辰巳"})]
    doc = fitz.open(str(pdf))
    try:
        draft, _, _ = make_draft(pdf, entries)
        result = V.verify_draft(draft, pdf)
        page = doc[2]
        tail = G.glyph_rects_for_text(page, "寅卯辰巳", [0, 0, 595, 842])
    finally:
        doc.close()
    rect = draft["items"][0]["provenance"]["rect"]
    tail_right = max(b[2] for b in tail) if tail else None
    fake = [v for v in result["validations"]
            if v["code"] in ("transcription_extra", "transcription_missing")]
    check("行尾选项的最后一个字落在题目区域之内（题干窄于选项行时不得切掉行尾）",
          tail_right is not None and rect[2] >= tail_right - 1.0,
          f"区域右缘 {rect[2]} < 行尾字形右缘 {tail_right}")
    check("逐字照抄的短题干条目不产生任何漏抄/编造记录（此前会报『发明选项字母 D』）",
          not fake, f"实得 {[(v['code'], v['severity'], v.get('detail', '')[:60]) for v in fake]}")


def run_scanned(tmp: Path) -> None:
    print("\n--- 扫描页（无文本层）：条目照常入库，验收不可用要留痕 ---")
    blank = Path(tmp) / "scanned.pdf"
    doc = fitz.open()
    doc.new_page(width=595, height=842)          # 纯扫描页：文本层为空
    doc.save(str(blank))
    doc.close()
    entries = [entry(1, 1, "扫描页上的题干照抄不误", "扫描页上的题干照抄不误",
                     {"A": "甲", "B": "乙", "C": "丙", "D": "丁"})]
    draft, _, _ = make_draft(blank, entries)
    it = draft["items"][0]
    check("扫描页条目照常入库且可作答（不被锚点缺失误杀）",
          it["answerable"] is True and not it["unparseable_reason"],
          f"answerable={it.get('answerable')} notice={it.get('notice')!r}")
    check("整卷留一条 text_layer_missing 警告（验收不可用必须说出来，不许静默）",
          any(v["code"] == "text_layer_missing" and v["severity"] == "warn"
              for v in draft["validations"]),
          f"实得 {codes_for(draft)}")


def run_report(pdf: Path) -> None:
    print("\n--- 报告：每一句「第 N 题」都必须是真的 ---")
    draft, _, _ = make_draft(pdf, BASE_ENTRIES)
    result = V.verify_draft(draft, pdf)
    lines = result["report"]
    check("报告可渲染（未登记的 code 会在这里抛 KeyError）",
          isinstance(lines, list) and len(lines) > 5, f"实得 {len(lines)} 行")
    ids = {int(i["printed_no"]) for i in result["items"] if i.get("printed_no")}
    mentioned = {int(m) for m in __import__("re").findall(r"第 (\d+) 题", "\n".join(lines))}
    check("报告里点到的卷面号都真实存在（或被台账合法点名）",
          mentioned <= ids | {4}, f"点到 {sorted(mentioned)} 而条目号 {sorted(ids)}")
    check("验收器的阈值写进了 meta（对外报告的数字必须就是实际用的数字）",
          result["meta"]["verification"]["thresholds"]["min_len"] == 2
          and result["meta"]["verification"]["thresholds"]["formula_ink_ratio"] > 0,
          f"实得 {result['meta']['verification']['thresholds']}")
    limit("公式内容不在自动校验范围内",
          "fixture 未含 MathType 公式；公式的「存在性」靠 geometry 的墨迹三分支，"
          "其正确性只能人工复核（见 protocol.md 与 classify_char 的限度说明）")


def main() -> int:
    print("=" * 70)
    print("门禁 8：抽取层（probe 定位 + 纯代码验收）")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / "fixture.pdf"
        build_fixture(pdf)
        run_scan_and_loss(pdf)
        run_pin(pdf)
        run_columns(pdf)
        run_verify(pdf)
        run_allow_list(pdf)
        run_short_stem(pdf)
        run_scanned(td)
        run_report(pdf)

    if limits:
        print("\n已核实的限度（写进文档，不当成已通过）：")
        for s in limits:
            print(f"  · {s}")
    if failures:
        print(f"\n失败 {len(failures)} 项：{failures}")
        return 1
    print("\n通过：抄题模型无权给自己盖章，盖章的验收器真的在比对，坏数据不吞结果")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
