"""2026 国考《行测》解析：行测真题试卷/*.pdf 与 行招/*答案*.pdf -> data/exam_xc_*.json

这批 PDF 与前两批不同：**自带答案**。版式如下（解析版）：

    1.习近平总书记强调……下列表述正确的有几项？
    ①…… ②…… ③…… ④……
    A.1 项
    B.2 项
    C.3 项
    D.4 项
    1.D。【解析】本题考查……因此，本题答案为D 项。
    2.下一题……

因此可以直接从解析版里同时取出**题目、选项与答案**，无需再去和试卷版逐题匹配。
试卷版（不含答案）用于核对题目数量与文本一致性。

本脚本只负责「读 PDF → 原始条目」；信封组装、缺损判定、损失账目一律走 src/bank.py。

关于丢弃：旧实现把 `len(options) >= 3` 当作 append 的前置条件，缺损题直接消失，
于是 `answerable` 恒为真、"题面可能有缺损，不作答"成为永不可达的死代码，
卷面 122–135 个题号只留下 104–110 条。现在**条目一律保留**，选项在原件里是图片
（图形推理一类）的题会以 `answerable=False` + Validation 的形式出现在界面上，
并且每个见过的卷面号都要在 `meta.loss` 里对上账。

用法: python src/parse_xingce_pdf.py
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

import fitz

import bank

ROOT = Path(__file__).resolve().parent.parent
EXAM_DIR = ROOT / "行测真题试卷"
ANS_DIR = ROOT / "行招"
DATA = ROOT / "data"

# 页眉页脚（各页重复出现）
NOISE = [
    re.compile(r"^上岸咨询热线/微信：.*$"),
    re.compile(r"^要成公，选优公！圆您公职梦！$"),
    re.compile(r"^版权所有\s*翻版必究$"),
    re.compile(r"^\d{1,3}$"),
]
Q_START = bank.question_number_re(3)
OPT = re.compile(r"^\s*([A-D])\s*[.．、)]\s*(.*)$")
# 答案行：'1.D。【解析】…' 或 '1．D．【解析】…'
ANS_LINE = re.compile(r"^\s*(\d{1,3})\s*[.．、]\s*([A-D])\s*[。.．]")
# 后续小节的答案只写在解析里：「因此，本题答案为X 项」
ANS_INLINE = re.compile(r"本题答案为\s*([A-D])\s*项")
# 题号后面紧跟答案字母：'12.D。【解析】…' 是解析行，不是第 12 题的题干
ANSWER_TAIL = re.compile(r"^[A-D]\s*[。.．]")
SECTION = re.compile(r"^(第[一二三四五六七八]部分|.*（共\s*\d+\s*题[^）]*）)\s*$")
MAX_QNO = 200
ALLOWED = bank.option_letters("ABCD")


def load_pages(pdf: str) -> list[list[str]]:
    doc = fitz.open(pdf)
    pages: list[list[str]] = []
    for page in doc:
        out = []
        for blk in page.get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                t = "".join(sp["text"] for sp in ln["spans"])
                t = re.sub(r"\s+", " ", t).strip()
                if not t or any(p.match(t) for p in NOISE):
                    continue
                out.append(t)
        pages.append(out)
    return pages


def parse_answers_pdf(pdf: str) -> dict:
    """解析「答案解析」版：一题一条，含题干、选项、答案与解析。

    返回值里的 `seen` 是**扫描文本层**得到的卷面题号集合，故意不从 items 反推，
    否则「丢了哪些题」这个问题就依赖丢失的结果本身。
    """
    pages = load_pages(pdf)
    flat = [t for pg in pages for t in pg]
    # 答案行里出现过的卷面号：下面放行「小数守卫误杀」时的第三把锁
    answer_nos = {int(m.group(1)) for m in map(ANS_LINE.match, flat) if m}

    items: list[dict] = []
    seen: set[int] = set()
    dropped: list[dict] = []        # 见过卷面号却没产出条目的，逐条给原因
    produced: dict[int, dict] = {}  # 卷面号 -> 条目，迟到的答案行要回填给对应题
    rejected: list[str] = []        # 被解析算式冒充的「假题号」行
    last_qno = 0
    cur: dict | None = None
    section = ""
    part = ""
    pending = ""      # （共N题）上一行是小节名
    started = False   # 卷首「重要提示」里的注意事项也是编号的，遇到「第N部分」后才算正式开考

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            # 一律保留：完整性判定交给 bank.complete_item，它只降级、不删条目。
            items.append(cur)
            produced[cur["printed_no"]] = cur
        cur = None

    def next_question_candidate(text: str) -> bool:
        """小数守卫的例外判定。

        '6.2025 年7 月，……' 会被 looks_like_decimal_prefix 当成小数 6.2025 而整行丢弃，
        于是这道题连题干带四个选项一起消失（实测副省级卷面 6/8/20 全中）。
        三把锁同时成立才放行，避免把 '119.6 万吨' 这类解析里的数值当成新题：
        卷面号必须正好是下一题、该号在答案行里出现过、题号后面还有一句汉字。
        """
        m = Q_START.match(text)
        if not m or not started or not 1 <= int(m.group(1)) <= MAX_QNO:
            return False
        qno = int(m.group(1))
        return (qno == last_qno + 1 and qno in answer_nos
                and len(bank.cjk_only(m.group(2))) >= 8)

    i = 0
    while i < len(flat):
        t = flat[i]
        # 答案行：把答案与解析挂到当前题上（该行同时是本题的结束标志）
        ans = ANS_LINE.match(t)
        if ans:
            no = int(ans.group(1))
            if started:
                seen.add(no)          # 卷面出现过这个号，无论后面认没认出来
            if cur is not None and no == cur["printed_no"]:
                cur["standard_answer"] = ans.group(2)
                cur["explanation"] = t[ans.end():]
                flush()
            else:
                hit = produced.get(no)
                if hit is not None:
                    # 迟到的答案行：它对应的题已被下一题的题号行冲掉，回填而不是丢弃
                    if not hit.get("standard_answer"):
                        hit["standard_answer"] = ans.group(2)
                elif started:
                    dropped.append({
                        "printed_no": no,
                        "reason": "文本层只出现该卷面号的答案行（'N.X。【解析】'），"
                                  "题干行未被识别",
                    })
            i += 1
            continue
        # 只在解析里点明答案的写法（没有独立的 'N.X。' 行）
        inline = ANS_INLINE.search(t)
        if inline and cur is not None and not cur.get("standard_answer"):
            cur["standard_answer"] = inline.group(1)
            flush()
            i += 1
            continue
        # 小数守卫会误杀题干以年份开头的题，三把锁成立时放行走题号识别
        if bank.looks_like_decimal_prefix(t, 3) and not next_question_candidate(t):
            i += 1
            continue
        m = SECTION.match(t)
        if m and not Q_START.match(t):
            if "部分" in t:
                part = t
                started = True          # 正式开考
            else:
                section = pending or t  # 小节名在「（共N题…）」的上一行
                started = True
            i += 1
            continue
        m = Q_START.match(t)
        qno = int(m.group(1)) if m and 1 <= int(m.group(1)) <= MAX_QNO else None
        if qno is not None and started and not m.group(2).startswith("【") \
                and not ANSWER_TAIL.match(m.group(2)):
            if qno <= last_qno:
                # 本版式卷面题号严格递增。回落到个位数的「题号」来自解析里的算式
                # （'6 2321 。 总的增长量约为…'），不是新一道题。
                rejected.append(t[:40])
                flush()                 # 与旧实现一样冲掉当前题，避免解析文字并进题干
                i += 1
                continue
            flush()
            seen.add(qno)
            last_qno = qno
            cur = {"printed_no": qno, "stem": m.group(2), "options": {},
                   "explanation": "", "standard_answer": None,
                   "part": part, "section_name": section, "saw_bare_letter": False}
            i += 1
            continue
        if cur is not None:
            o = OPT.match(t)
            if o:
                cur["options"].setdefault(o.group(1), o.group(2).strip())
            elif cur["options"]:
                # 选项之后、答案行之前的说明文字，归入解析
                cur["explanation"] += (" " if cur["explanation"] else "") + t
            else:
                # 图形推理一类：选项是图片，文本层只剩孤零零一个字母行
                if bank.bare_letter(t, ALLOWED):
                    cur["saw_bare_letter"] = True
                cur["stem"] += (" " if cur["stem"] else "") + t
        else:
            pending = t
        i += 1
    flush()
    for it in items:
        it["options_as_image"] = bool(
            it.pop("saw_bare_letter") or bank.trailing_answer_letter(it["stem"], ALLOWED))
    return {"pages": len(pages), "items": items, "seen": sorted(seen),
            "dropped": dropped, "rejected": rejected}


def build(ans_pdf: Path, exam_pdf: Path | None) -> dict:
    parsed = parse_answers_pdf(str(ans_pdf))
    raw = parsed["items"]

    exam_pages = len(load_pages(str(exam_pdf))) if exam_pdf and exam_pdf.exists() else 0

    # 行内 kind 判定要在 attach_sections 覆写 section_name 之前做：
    # 小节名会回落到「大节名」，届时 "选词填空" 就再也匹配不上了。
    for it in raw:
        sec = it["section_name"]
        it["kind"] = "cloze" if ("逻辑填空" in sec or "选词" in sec) else "mcq"

    items, validations = bank.renumber(raw, id_prefix="X")
    for k, it in enumerate(items):
        it, vs = bank.complete_item(it, allowed=ALLOWED)
        items[k] = it
        validations.extend(vs)
    sections, items = bank.attach_sections(items)

    no_ans = [i["printed_no"] for i in items if not i.get("standard_answer")]
    if no_ans:
        validations.append(bank.validation(
            "no_standard_answer", severity="warn", ids=no_ans,
            detail=f"未取到标准答案（卷面号），共 {len(no_ans)} 道"))

    loss = bank.build_loss(parsed["seen"], items, dropped=parsed["dropped"])
    if parsed["rejected"]:
        validations.append(bank.validation(
            "source_defect", severity="info", count=len(parsed["rejected"]),
            detail=f"资料分析解析里的算式以数字开头，被误当成题号；已按「卷面题号严格递增」"
                   f"挡掉 {len(parsed['rejected'])} 行，样例：{parsed['rejected'][:3]}"))
    return bank.build_bank(
        subject="行测", source=ans_pdf.name, items=items, sections=sections,
        loss=loss, validations=validations,
        meta={"title": ans_pdf.stem.replace("试卷答案解析", "").strip(),
              "exam_source": exam_pdf.name if exam_pdf else "",
              "pages": parsed["pages"], "exam_pages": exam_pages},
        generated_by="src/parse_xingce_pdf.py",
        method="从「试卷答案解析」版直接取题目+选项+答案（版式规整，无需与试卷版逐题匹配）")


def main() -> None:
    ans_pdfs = sorted(glob.glob(str(ANS_DIR / "*行测*答案解析.pdf")))
    if not ans_pdfs:
        sys.exit(f"未找到带答案的解析版 PDF：{ANS_DIR}")
    for ap in ans_pdfs:
        ap = Path(ap)
        # 对应的试卷版（不含答案），用于记录
        key = ap.stem.replace("试卷答案解析", "").strip()
        exam_pdf = next((Path(p) for p in glob.glob(str(EXAM_DIR / "*.pdf"))
                         if key.replace("（", "(").replace("）", ")") in
                         Path(p).stem.replace("（", "(").replace("）", ")")), None)
        b = build(ap, exam_pdf)
        out = DATA / f"exam_xc_{key.replace('2026年国考《行测》', '').strip('（）')}.json"
        bank.print_bank_report(b)
        bank.write_bank(b, out)
        print(f"   已写入 {out.relative_to(ROOT)}\n")


if __name__ == "__main__":
    main()
