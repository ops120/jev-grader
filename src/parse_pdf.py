"""把 2025 高考英语试卷 PDF 解析成 v2 题库 exam.json，并打印解析体检报告。

试卷版式固定，故采用"清洗页眉页脚 -> 按固定小节标题切块 -> 分块专用提取"的状态机策略；
信封、编号、完整性判定与损失账目一律走 bank.py。

用法: python src/parse_pdf.py [试卷.pdf] [--out data/exam.json]
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bank  # noqa: E402

OUT_EXAM = ROOT / "data" / "exam.json"
OUT_RAW = ROOT / "data" / "_raw.txt"

NOISE_PATTERNS = [
    re.compile(r"^英语试卷\s*第\s*\d+\s*页（共\s*\d+\s*页）$"),
    re.compile(r"^锦宏教育微信公众号.*$"),
    re.compile(r"^绝密★启用前$"),
]

# (小节 id, 中文名, 题型, 题号范围, 该节篇章应有的题空数)
SUBSECTIONS = [
    ("L1", "听力·第一节", "listening", (1, 5), 0),
    ("L2", "听力·第二节", "listening", (6, 20), 0),
    ("R1", "阅读·第一节", "mcq", (21, 35), 0),
    ("R2", "阅读·第二节", "seven", (36, 40), 5),
    ("U1", "语言运用·第一节 完形填空", "cloze", (41, 55), 15),
    ("U2", "语言运用·第二节 语法填空", "gapfill", (56, 65), 10),
    ("W1", "写作·第一节 应用文", "writing", (66, 66), 0),
    ("W2", "写作·第二节 读后续写", "writing", (67, 67), 0),
]

SECTION_HEADERS = ["第一部分 听力", "第二部分 阅读", "第三部分 语言运用", "第四部分 写作"]

# 本卷性质决定的不可作答，与解析缺损无关。文案逐字沿用 v1：界面直接显示这句话，
# 改字等于改口径。
DOMAIN_UNANSWERABLE = {
    "listening": "听力题需音频材料，本试卷不含听力原文，无法作答",
    "gapfill": "语法填空需生成词形，而 JEV 只返回类型化判定，需候选集模式（v2）",
    "writing": "写作题需生成文本，而 JEV 不产出文本；v2 将改为按评分档次打分的评分模式",
}

# 卷面实际印刷的选项字母集：听力只有 A–C，七选五是 A–G，其余四选一。
# 拿 "ABCD" 去要求听力题会把 20 道完好的听力题判成"缺 D、题面缺损"。
ALLOWED_LETTERS = {"seven": "ABCDEFG", "listening": "ABC"}

Q_START = re.compile(r"^(\d{1,2})[.．]\s*(.*)$")
Q_WITH_OPT = re.compile(r"^(\d{1,2})[.．]\s*([A-G])[.．]\s*(.*)$")
OPT_LINE = re.compile(r"^([A-G])[.．]\s*(.*)$")
PASSAGE_MARK = re.compile(r"^([A-D])$")
LISTEN_MATERIAL = re.compile(r"^听第(\d+)段材料，回答第(\d+)(?:、|至)(\d+)题。")

Line = tuple[str, int]  # (文本, 页码)


# ---------- 载入与清洗 ----------

def load_pages(pdf_path: str) -> tuple[list[list[Line]], set[int], list[str]]:
    doc = fitz.open(pdf_path)
    size_hits: dict[tuple[int, int], int] = {}
    per_page_sizes: list[list[tuple[int, int]]] = []
    for page in doc:
        sizes = [(im[2], im[3]) for im in page.get_images(full=True)]
        per_page_sizes.append(sizes)
        for s in set(sizes):
            size_hits[s] = size_hits.get(s, 0) + 1
    total = doc.page_count
    # 每页重复出现的同尺寸图片是页脚水印，非内容图表
    watermark = {s for s, n in size_hits.items() if n > total * 0.8}

    pages: list[list[Line]] = []
    figure_pages: set[int] = set()
    raws: list[str] = []
    for idx, page in enumerate(doc):
        raw = page.get_text()
        raws.append(raw)
        lines: list[Line] = []
        for line in raw.splitlines():
            s = line.replace("\u3000", " ").strip()
            if not s or any(p.match(s) for p in NOISE_PATTERNS):
                continue
            lines.append((s, idx + 1))
        pages.append(lines)
        if any(s not in watermark for s in set(per_page_sizes[idx])):
            figure_pages.add(idx + 1)
    return pages, figure_pages, raws


# ---------- 篇章与题目构造 ----------
# 折行归一 / 段落合并 / 切句 / 篇章构造由 bank.py 统一提供（曾在四份脚本里各写一遍）

make_passage = bank.make_passage


def extract_items(lines: list[str], low: int, high: int) -> dict[int, dict]:
    items: dict[int, dict] = {}
    cur: int | None = None
    for s in lines:
        m = Q_WITH_OPT.match(s)
        if m and low <= int(m.group(1)) <= high:
            cur = int(m.group(1))
            items.setdefault(cur, {"stem": "", "options": {}})["options"][m.group(2)] = m.group(3).strip()
            continue
        m = Q_START.match(s)
        if m and low <= int(m.group(1)) <= high:
            cur = int(m.group(1))
            it = items.setdefault(cur, {"stem": "", "options": {}})
            it["stem"] = (it["stem"] + " " + m.group(2)).strip()
            continue
        m = OPT_LINE.match(s)
        if m and cur is not None and not PASSAGE_MARK.match(s):
            items[cur]["options"].setdefault(m.group(1), m.group(2).strip())
            continue
        if cur is not None and items[cur]["stem"] and not items[cur]["options"]:
            items[cur]["stem"] = (items[cur]["stem"] + " " + s).strip()
    return items


def build(out_exam: Path, pdf: str | None = None) -> dict:
    if pdf:
        pdfs = [pdf]
    else:
        # 仓库根目录同时放着数学/历史/政治卷，"取第一个" 是靠文件名排序碰运气。
        # 默认只认文件名里带"英语"的那份（真实文件名是"…考试英 语.pdf"，中间有空格），
        # 认不出来就要求显式传参。
        pdfs = [p for p in sorted(glob.glob(str(ROOT / "*.pdf")))
                if "英语" in Path(p).name.replace(" ", "")]
    if not pdfs:
        sys.exit("未找到英语试卷 PDF；请把 PDF 路径作为参数传进来")
    pages, figure_pages, raws = load_pages(pdfs[0])
    OUT_RAW.write_text("\n".join(raws), encoding="utf-8")
    flat: list[Line] = [ln for pg in pages for ln in pg]
    texts = [s for s, _ in flat]

    bounds = [(h, i) for i, s in enumerate(texts) for h in SECTION_HEADERS if s.startswith(h)]
    sub_marks = [i for i, s in enumerate(texts) if re.match(r"^第[一二三四]节[（(]", s)]

    parts: list[tuple[str, int, int]] = []
    for k, (sid, *_rest) in enumerate(SUBSECTIONS):
        if k < len(sub_marks):
            start = sub_marks[k]
            end = sub_marks[k + 1] if k + 1 < len(sub_marks) else len(flat)
            parts.append((sid, start, end))
        else:
            parts.append((sid, 0, 0))

    passages: list[dict] = []
    raw_items: list[dict] = []
    sections_meta: list[dict] = []
    validations: list[dict] = []
    seen: set[int] = set()            # 文本层出现过的卷面题号（丢失核算的分母）

    if len(bounds) != 4:
        validations.append(bank.validation(
            "section_count_mismatch", severity="warn", detail="大节标题",
            actual=len(bounds), expected=4))
    if len(sub_marks) != 8:
        validations.append(bank.validation(
            "section_count_mismatch", severity="warn", detail="小节标题",
            actual=len(sub_marks), expected=8))

    for (sid, name, kind, (low, high), want_blanks), (_, start, end) in zip(SUBSECTIONS, parts):
        seg = flat[start:end]
        lines = [s for s, _ in seg]
        part_name = next((h for h, i in bounds if i <= start), "")
        seen.update(int(m.group(1)) for s in lines
                    if (m := Q_START.match(s)) and low <= int(m.group(1)) <= high)

        def add(no: int, it: dict, *, reason: str | None = None, **kw) -> None:
            raw_items.append({
                "printed_no": no, "no": no, "section_id": sid, "section_name": name,
                "part": part_name, "kind": kind, "stem": it.get("stem", ""),
                "options": it.get("options", {}), **kw,
                **({"_reason": reason} if reason else {}),
            })

        def missing(no: int) -> str:
            return (f"卷面第 {no} 题的题号在文本层出现过，但题干/选项没解析出来" if no in seen
                    else f"卷面第 {no} 题没有出现在文本层（缺页、图片题或排版异常）")

        if kind == "mcq":
            order = [(i, s) for i, s in enumerate(lines) if PASSAGE_MARK.match(s)]
            for pos, lab in order:
                stop = len(lines)
                for i in range(pos + 1, len(lines)):
                    mm = Q_START.match(lines[i])
                    if mm and low <= int(mm.group(1)) <= high:
                        stop = i
                        break
                passages.append(make_passage(f"P{lab}", lab, seg[pos + 1:stop], figure_pages,
                                            detect_title=True))
            got = extract_items(lines, low, high)
            for no in range(low, high + 1):
                if no in got:
                    add(no, got[no], passage_id=_owner(no, lines, passages))
                else:
                    add(no, {"stem": "", "options": {}}, reason=missing(no), passage_id=None)

        elif kind == "seven":
            opt_start = next((i for i, s in enumerate(lines) if OPT_LINE.match(s)), len(lines))
            body = [ln for ln in seg[:opt_start]
                    if not ln[0].startswith(("第二节（", "阅读下面短文"))
                    and "多余选项" not in ln[0]]
            passages.append(make_passage("P7", None, body, figure_pages, detect_title=True))
            choices = {m.group(1): m.group(2).strip()
                       for s in lines[opt_start:] if (m := OPT_LINE.match(s))}
            for no in range(low, high + 1):
                add(no, {"stem": f"从 A–G 中选出适合第 {no} 空的最佳选项（两项多余）。",
                         "options": dict(choices)},
                    passage_id="P7",
                    **({"reason": f"A–G 选项一个都没解析出来，七选五无从判断"} if not choices else {}))

        elif kind == "cloze":
            first = next((i for i, s in enumerate(lines)
                          if Q_WITH_OPT.match(s) or (OPT_LINE.match(s) and not PASSAGE_MARK.match(s))),
                         len(lines))
            body = [ln for ln in seg[:first]
                    if not ln[0].startswith(("第一节（", "阅读下面短文", "第三部分"))]
            passages.append(make_passage("PCLOZE", None, body, figure_pages))
            got = extract_items(lines[first:], low, high)
            for no in range(low, high + 1):
                if no in got:
                    add(no, {"stem": f"选择适合第 {no} 空的最佳选项。", "options": got[no]["options"]},
                        passage_id="PCLOZE")
                else:
                    add(no, {"stem": "", "options": {}}, reason=missing(no), passage_id="PCLOZE")

        elif kind == "gapfill":
            body = [ln for ln in seg
                    if not ln[0].startswith(("第二节（", "阅读下面短文", "第三部分"))]
            p = make_passage("PGAP", None, body, figure_pages)
            passages.append(p)
            joined = " ".join(p["paragraphs"])
            for no in range(low, high + 1):
                m = re.search(rf"\[{no}\]____\s*\(([^)]+)\)", joined)
                hint = m.group(1) if m else ""
                add(no, {"stem": f"填入第 {no} 空：1 个单词，或括号内词的正确形式"
                                  + (f"（提示词：{hint}）" if hint else ""), "options": {}},
                    passage_id="PGAP", hint=hint, no_options=True)

        elif kind == "writing":
            body = [ln for ln in seg if not re.match(r"^第[一二]节（满分", ln[0])]
            text = " ".join(bank.join_lines_into_paragraphs("\n".join(s for s, _ in body)))
            openers = [o for o in ["I realized it was me who was at fault.",
                                   "With the biscuits my wife had made, I arrived at my brother's door."]
                       if o in text]
            add(low, {"stem": text, "options": {}}, passage_id=None,
                openers=openers, no_options=True)

        elif kind == "listening":
            got = extract_items(lines, low, high)
            mat, cur = {}, None
            for s in lines:
                m = LISTEN_MATERIAL.match(s)
                if m:
                    cur = f"第{m.group(1)}段材料（第{m.group(2)}–{m.group(3)}题）"
                mm = Q_START.match(s)
                if mm and low <= int(mm.group(1)) <= high and cur:
                    mat[int(mm.group(1))] = cur
            for no in range(low, high + 1):
                if no in got:
                    add(no, got[no], passage_id=None, material=mat.get(no, ""))
                else:
                    add(no, {"stem": "", "options": {}}, reason=missing(no),
                        passage_id=None, material="")

        sections_meta.append({
            "id": sid, "part": part_name, "name": name, "kind": kind,
            "no_from": low, "no_to": high, "per": _per_score(low, high),
            "count": sum(1 for it in raw_items if it["section_id"] == sid),
        })

        # 篇章题空数校验：七选五 5、完形 15、语法填空 10
        if want_blanks:
            for p in [p for p in passages if p["id"] in
                      {it.get("passage_id") for it in raw_items if it["section_id"] == sid}]:
                if len(p["blanks"]) != want_blanks:
                    validations.append(bank.validation(
                        "blank_count_mismatch", severity="error", detail=p["id"],
                        actual=len(p["blanks"]), expected=want_blanks))

    # ---- 编号与完整性判定 ----
    items, dup_vs = bank.renumber(raw_items, id_prefix="Q")
    validations += dup_vs
    for k, it in enumerate(items):
        kind = it["kind"]
        done, vs = bank.complete_item(
            it, allowed=ALLOWED_LETTERS.get(kind, "ABCD"),
            expects_options=not it.pop("no_options", False),
            reason_override=it.pop("_reason", None),
            domain_reason=DOMAIN_UNANSWERABLE.get(kind))
        items[k] = done
        validations += vs

    degraded = [{"printed_no": it["printed_no"], "reason": it["unparseable_reason"]}
                for it in items if it.get("unparseable_reason")]
    if degraded:
        validations.append(bank.validation(
            "never_parsed", severity="error", ids=[d["printed_no"] for d in degraded],
            detail="这些卷面题号没能产出可用题面，已按不可作答保留（不删除）"))

    fig_ids = {p["id"] for p in passages if p["has_figure"]}
    for it in items:
        if it.get("passage_id") in fig_ids:
            it["notice"] = (it["notice"] + "；" if it["notice"] else "") + \
                "本题材料含图表，图表中的数据不在文本内"

    for p in passages:
        if p["label"] and not p["sentences"]:
            validations.append(bank.validation("passage_no_sentences", severity="warn",
                                               detail=p["id"]))

    exam = bank.build_bank(
        subject="英语", source=Path(pdfs[0]).name, items=items, sections=sections_meta,
        passages=passages,
        loss=bank.build_loss(seen, items, dropped=degraded),
        validations=validations,
        meta={"pages": len(pages), "figure_pages": sorted(figure_pages),
              "title": "2025 年高考英语（全国卷）"},
        generated_by="src/parse_pdf.py",
        method="试卷版式固定：清洗页眉页脚 → 按小节标题切块 → 分块专用提取；"
               "题号以卷面为准，未解析出的题号降级保留而不删除")
    bank.write_bank(exam, out_exam)
    return exam


def _per_score(low: int, high: int) -> float:
    if low <= 5:
        return 1.5
    if low <= 20:
        return 1.5
    if low <= 40:
        return 2.5
    if low <= 55:
        return 1.0
    if low <= 65:
        return 1.5
    return 15.0 if low == 66 else 25.0


def _owner(no: int, lines: list[str], passages: list[dict]) -> str | None:
    last = None
    for i, s in enumerate(lines):
        m = PASSAGE_MARK.match(s)
        if m:
            last = m.group(1)
        mm = Q_START.match(s)
        if mm and int(mm.group(1)) == no and last:
            return next((p["id"] for p in passages if p["label"] == last), None)
    return None


def print_passages(exam: dict) -> None:
    """篇章明细：英语卷特有——它的证据图例是句子级的，篇章解析得好不好直接决定判定可信度。"""
    print()
    print("篇章：")
    for p in exam["passages"]:
        tag = f"[{p['label']}]" if p["label"] else "    "
        fig = " 含图表" if p["has_figure"] else ""
        print(f"  {p['id']:<8}{tag:<6}句 {len(p['sentences']):<4}空 {str(p['blanks']):<26}"
              f"{p['chars']:>6} 字符  页 {p['pages']}{fig}")


def main() -> None:
    argv = sys.argv[1:]
    out = OUT_EXAM
    if "--out" in argv:
        i = argv.index("--out")
        out = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    pdf = next((a for a in argv if not a.startswith("-")), None)
    exam = build(out, pdf)
    bank.print_bank_report(exam)
    print_passages(exam)


if __name__ == "__main__":
    main()
