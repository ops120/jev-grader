"""草稿题库的验收器：**纯代码，不联网**。抄题模型无权给自己的输出盖章，盖章只发生在这里。

它替换的是数学卷旧核验机制的三个盲区
-------------------------------------
旧实现（parse_math_pdf.py 里的 zh_only + 全卷子串比对）：
  1. 只检查「转写里的中文是否出现在**整份 PDF 任意一页**」——单向、且不做局部定位。
     编造一段中文只要它的字在别处出现过就查不出来；顺序错了也查不出来。
  2. 比对前先把 $...$ 和所有非汉字字符剥掉，于是**公式天然不在检查范围内**：
     `1+5i i`→`(1+5i)i`（括号丢）、`7 倍`→`√7 倍`（根号丢）都无从暴露。
  3. `known = no == 15` 一个布尔就把第 15 题的全部告警压掉了。

本模块的做法：
  1. **双向、限定区域的 diff**：每个条目必须自带 provenance（页码 + 区域），
     在该区域内同时报告「转写有、原文无」（疑似编造）与「原文有、转写无」（疑似漏抄）。
  2. **公式存在性检查**：每个 $...$ 组必须能在其锚点邻域找到无法用文本层解释的墨迹
     （矢量线条/图像块），否则判定为未核验。MathType 区域不再能被跳过。
  3. **已知缺陷白名单**：(item_id, expected_diff, evidence) 三元组逐条登记、逐条打印，
     只有与登记内容完全吻合的差异才被豁免；一个条目不能豁免另一条目的差异。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import fitz

import sys
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bank
from . import geometry as G

# 转写里允许出现、但原文文本层一定没有的东西：LaTeX 命令、数学符号、空白。
_LATEX_CMD = re.compile(r"\\[a-zA-Z]+")
_MATH_SYMBOLS = re.compile(r"[=+\-*/^_{}()\[\]<>|.,;:!?'\"~&#%$0-9 \u00b1\u00d7\u00f7"
                           r"\u2264\u2265\u221a\u03c0\u03b1\u03b2\u03b3\u03c7\u03bb"
                           r"\u2212\u2248\u2260\u221e\u2208\u2286\u2287\u2227\u2228\u2192]")
_DOLLAR = re.compile(r"\$([^$]*)\$")

MIN_LEN_DEFAULT = 2       # 参与 diff 的最小连续片段长度（汉字令牌）；短于此的差异视为噪声
RATIO_EXTRA = 0.15        # 转写独有占转写总长的比例阈值
RATIO_MISSING = 0.15      # 原文独有占原文总长的比例阈值
ABS_EXTRA = 6             # 绝对字数阈值
ABS_MISSING = 8
FORMULA_INK_RATIO = 0.05  # 锚点邻域「非文本层墨迹 / 锚点面积」的通过线


# ---------------------------------------------------------------------------
# 条目文本 → 可比对的纯文本
# ---------------------------------------------------------------------------
def plain_text(item: dict) -> str:
    """把条目的 stem/options/subs/appendices/table 展平成一串可与人读的文本。

    保留 $...$ 之外的正文顺序，公式段整体剔除（它们由公式存在性检查单独负责）。
    """
    parts: list[str] = [item.get("stem") or ""]
    opts = item.get("options") or {}
    for letter in sorted(opts):
        parts.append(f"{letter}. {opts[letter]}")
    for key in ("subs", "appendices"):
        parts.extend(item.get(key) or [])
    table = item.get("table")
    if isinstance(table, dict):
        parts.append(" ".join(str(x) for x in (table.get("header") or [])))
        for row in table.get("rows") or []:
            parts.append(" ".join(str(x) for x in row))
    for key in ("crit_table",):
        t = item.get(key)
        if isinstance(t, dict):
            parts.append(" ".join(str(x) for x in (t.get("header") or [])))
            for row in t.get("rows") or []:
                parts.append(" ".join(str(x) for x in row))
    if item.get("material"):
        parts.append(item["material"])
    return _DOLLAR.sub(" ", "\n".join(p for p in parts if p))


def prose_seq(text: str) -> str:
    """参与文字比对的令牌序列（公式段已剔除，见 normalize_for_compare 的说明）。"""
    return bank.normalize_for_compare(text, keep_math=False)


def is_cjk_dominant(text: str) -> bool:
    n_cjk = len(bank.cjk_only(text))
    n_letters = len(re.findall(r"[A-Za-z]", text))
    return n_cjk >= max(4, n_letters)


def _token_stream(page: "fitz.Page", region: list[float]) -> tuple[list[str], list[tuple[str, list[float]]]]:
    """区域内文本层字符流：(令牌列表, [(原始字符, 包围盒)])，两者逐位对齐。

    返回**列表**而不是拼接串：pos 要用来回指 `orig` 里的包围盒，任何一次
    「再归一化」都可能改变长度或令牌含义，而这里的位置就是证据本身。
    令牌规则见 bank.classify_char：汉字保留本身，字母数字塌成 '@'，其余可见符号塌成 '#'。
    报告里则一律还原成人能读的字——「原文独有 @@##@」这种证据没人能复核。
    """
    tokens: list[str] = []
    orig: list[tuple[str, list[float]]] = []
    d = page.get_text("rawdict", clip=region)
    for blk in d["blocks"]:
        if blk.get("type") == 1 or "lines" not in blk:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    t = ch.get("c") or ""
                    tok = bank.classify_char(t) if t else None
                    if tok is None:
                        continue
                    tokens.append(tok)
                    orig.append((t, [float(v) for v in ch["bbox"]]))
    return tokens, orig


def token_boxes(orig: list[tuple[str, list[float]]]) -> list[list[float]]:
    """与令牌串逐位对齐的字形包围盒，用于把「一段差异」定位回纸上的位置。"""
    return [box for _, box in orig]


def render_source_evidence(pos: int, length: int,
                           orig: list[tuple[str, list[float]]]) -> str:
    """把令牌区间还原为原文片段（比对用抽象令牌，报告必须给人能读的字）。"""
    return "".join(orig[i][0] for i in range(pos, min(len(orig), pos + length)))


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------
def allow_key(item_id: str | None, code: str, fragment: str) -> str:
    return f"{item_id}|{code}|{bank.normalize_for_compare(fragment, keep_math=False)}"


def check_allow_list(entries: list[dict], *, used: set[str],
                     observed: dict[tuple[str | None, str], list[str]] | None = None
                     ) -> list[dict]:
    """白名单自身的完整性：重复项、从未被用到的豁免项都必须报出来。

    「登记了就没人再核对」是这类机制最常见的腐化方式。
    observed 传进来是为了让「登记未命中」这条报错**能自解释**：光说"本次并未出现"
    等于把重新核实的活儿推给一个看不到现场的人；附上本次实际差异片段，
    登记者才知道是缺陷修好了、还是对齐边界漂了。
    """
    observed = observed or {}
    out: list[dict] = []
    keys: set[str] = set()
    for e in entries:
        missing = {"item_id", "expected_diff", "evidence"} - set(e)
        if missing:
            raise VerifyError(f"已知缺陷登记缺少字段 {sorted(missing)}：{e}")
        k = allow_key(e["item_id"], e.get("code", ""), e["expected_diff"])
        if k in keys:
            out.append(bank.validation("source_defect", severity="warn",
                                       item_id=e["item_id"],
                                       detail=f"已知缺陷登记重复：{e['expected_diff']}"))
        keys.add(k)
        if k not in used:
            found = observed.get((e["item_id"], e.get("code", ""))) or []
            out.append(bank.validation(
                "source_defect", severity="error", item_id=e["item_id"],
                detail=(f"已登记的原件缺陷本次并未出现（登记 diff={e['expected_diff']!r}，"
                        f"依据={e['evidence']}）。本次该条目此方向的实测差异："
                        f"{found if found else '一条都没有'}。"
                        f"要么缺陷已被修好、要么对齐边界漂移使片段不再逐字吻合，"
                        f"必须重新核实后改写或删除该登记")))
    return out


# ---------------------------------------------------------------------------
# 单条核对
# ---------------------------------------------------------------------------
def option_marker_letters(item: dict) -> set[str]:
    """转写里作为**结构脚手架**出现的选项标号（options 字典的键）。

    为什么把它们从文字比对中剔除：这些字母在纸上的位置与顺序完全由排版决定，
    区域文本层里它们常与题干挤在同一行（'A.6B.8'）、或被 MathType 拆散，
    逐位比对只会制造假告警。发明字母有专门的检查（check_invented_letters），
    不靠这里把关。

    判据只取条目自己的 options 键，**不按外部字母表过滤**：早先这里收一个
    allowed="ABCD" 参数，于是五行选项的卷子（行测 A–E、七选五到 G）里 E/F/G
    从来没被剔除过，全被当成"转写编造了选项标号"报出来。
    """
    return {k for k in (item.get("options") or {})
            if isinstance(k, str) and len(k) == 1 and k.isascii() and k.isalpha()}


def transcript_seq(item: dict) -> tuple[list[str], list[str]]:
    """条目 → 参与文字比对的令牌**列表**，同时给出它的选项标号（供防发明检查用）。

    返回列表而非字符串：它要和 _token_stream 的令牌列表一起喂给
    bank.align_token_lists，那里一步都不能再归一化（'@' 会被再塌成 '#'）。

    标号**不再从转写里剔除**。早先剔它是为了避开排版差异，但原文侧无从剔除，
    于是每道选择题都会留下若干段长度为 2 的 "@#" 独有片段被报成"疑似漏抄"——
    一道题必然产生四条假告警，真告警就淹在里面了。两侧都保留标号，
    它们本来就该逐字配上；「卷面上压根没印这个字母」由 check_invented_letters 单独管。
    """
    return list(prose_seq(plain_text(item))), sorted(option_marker_letters(item))


def verify_item(doc: "fitz.Document", item: dict, *, min_len: int = MIN_LEN_DEFAULT,
                allow: dict[str, dict] | None = None,
                used_allow: set[str] | None = None) -> tuple[list[dict], dict]:
    """核对一个条目，返回 (校验记录, 摘要数据)。任何情况下都不修改、也不丢弃 item。

    单条目的**数据**问题（页码越界、区域退化、文本层读空）一律降级为该条目的
    error 记录后返回，绝不向上抛：一题的 provenance 写坏不该让整卷验收无结果，
    更不该让调用方以为"跑通了"。真正抛 VerifyError 的只有"这份 PDF 根本打不开/
    草稿不是 v2"这类整轮无法继续的前提。
    """
    allow = allow or {}
    vs: list[dict] = []
    iid = item.get("id")
    pn = item.get("printed_no")
    prov = item.get("provenance") or {}
    page_no = prov.get("page")
    rect = prov.get("rect")
    if page_no is None or rect is None:
        vs.append(bank.validation("provenance_missing", severity="error", item_id=iid,
                                  printed_no=pn,
                                  detail="条目没有页码/区域来源，无法与原文比对"))
        return vs, {"checked": False}

    def unusable(why: str) -> tuple[list[dict], dict]:
        vs.append(bank.validation("provenance_unusable", severity="error",
                                  item_id=iid, printed_no=pn, detail=why))
        return vs, {"checked": False}

    try:
        idx = int(page_no) - 1
    except (TypeError, ValueError):
        return unusable(f"provenance.page={page_no!r} 不是整数页码，无法定位原件")
    if not 0 <= idx < doc.page_count:
        return unusable(f"provenance.page={page_no} 超出本 PDF 页数（共 {doc.page_count} 页）"
                        f"→ 该条目的比对未执行，不算通过")

    page = doc[idx]
    pr = [float(v) for v in page.rect]
    try:
        raw_region = [float(v) for v in rect]
    except (TypeError, ValueError):
        return unusable(f"provenance.rect={rect!r} 不是四个数，无法定位原件区域")
    # 必须先钳制：越界 rect 在 fitz 里静默返回空文本，于是整条转写都会被报成"编造"，
    # 而真正写坏的是那个 rect——错误会指向完全相反的方向。
    region = [max(pr[0], raw_region[0]), max(pr[1], raw_region[1]),
              min(pr[2], raw_region[2]), min(pr[3], raw_region[3])]
    if region[2] - region[0] < 1.0 or region[3] - region[1] < 1.0:
        return unusable(f"provenance.rect={[round(v, 1) for v in raw_region]} "
                        f"与第 {page_no} 页页面矩形 {[round(v, 1) for v in pr]} 无有效交集"
                        f"→ 该条目的比对未执行，不算通过")

    s_tokens, src_orig = _token_stream(page, region)
    src_raw = "".join(c for c, _ in src_orig)
    src_boxes = token_boxes(src_orig)
    if not s_tokens:
        return unusable(f"第 {page_no} 页区域 {[round(v, 1) for v in region]} "
                        f"的文本层一个字符都读不出来。纯扫描卷请先做 OCR 再抄题；"
                        f"若这一题的题面本来就是图片，请在草稿里标 options_as_image"
                        f"→ 该条目的比对未执行，不算通过")

    t_tokens, markers = transcript_seq(item)
    t_seq = "".join(t_tokens)
    # 图形推理这类「选项本身就是图片」的条目：缺的文字本来就不在文本层，
    # 但必须已经带了 unparseable_reason，否则就是我们在给自己找借口。
    image_only = bool(item.get("options_as_image")) or "图片" in (item.get("unparseable_reason") or "")

    extra, missing = bank.align_token_lists(t_tokens, s_tokens, min_len)

    def exempt(frag: str, code: str) -> bool:
        k = allow_key(iid, code, frag)
        entry = allow.get(k)
        if entry is None:
            return False
        if used_allow is not None:
            used_allow.add(k)
        return True

    found: dict[str, list[str]] = {"transcription_extra": [], "transcription_missing": []}

    # —— 方向一：转写里有、原文区域内没有 → 疑似编造
    for run in extra:
        frag = run["text"]
        found["transcription_extra"].append(frag)
        if exempt(frag, "transcription_extra"):
            continue
        n = run["len"]
        ratio = n / max(1, len(t_tokens))
        sev = "error" if (n >= ABS_EXTRA or ratio >= RATIO_EXTRA) else "warn"
        vs.append(bank.validation(
            "transcription_extra", severity=sev, item_id=iid, printed_no=pn,
            detail=(f"转写独有 {n} 字 {frag!r}"
                    f"｜上下文 …{bank.context_window(t_seq, run['pos'], 12)}… "
                    f"在本条目所属区域（第 {page_no} 页）的文本层里找不到"),
            count=n, actual=n,
            expected={"scope": f"page {page_no} rect {[round(v, 1) for v in region]}"}))

    # —— 方向二：原文区域内有、转写里没有 → 疑似漏抄
    for run in missing:
        raw = render_source_evidence(run["pos"], run["len"], src_orig)
        found["transcription_missing"].append(run["text"])
        if exempt(run["text"], "transcription_missing"):
            continue
        n = run["len"]
        if image_only and not bank.has_cjk(raw):
            continue                       # 图片选项区的字母/数字本来就读不出来
        if run["pos"] == 0 and re.fullmatch(r"\d{1,3}\s*[.．、]?", raw):
            continue                       # 区域开头的卷面题号：printed_no 已结构化承载并
                                           # 由损失账目单独核账，逐字比对再报一遍只是噪声
        if re.match(r"^\s*\d{1,3}\s*[.．、]", raw):
            vs.append(bank.validation(
                "transcription_missing", severity="info", item_id=iid, printed_no=pn,
                detail=(f"原文该区域另有以题号开头的内容（{raw[:26]!r}），"
                        f"疑为区域边界覆盖到下一题，不计为本题漏抄"),
                count=n, actual=n))
            continue
        if _is_page_furniture(raw):
            vs.append(bank.validation(
                "transcription_missing", severity="info", item_id=iid, printed_no=pn,
                detail=f"原文该区域含页眉页脚（{raw[:26]!r}），非题目内容",
                count=n, actual=n))
            continue
        sev = ("error" if (n >= ABS_MISSING or n / max(1, len(s_tokens)) >= RATIO_MISSING)
               else "warn")
        before = render_source_evidence(max(0, run["pos"] - 10), 10, src_orig)
        after = render_source_evidence(run["pos"] + n, 10, src_orig)
        box = G.rect_union(src_boxes[run["pos"]:run["pos"] + n]) or region
        vs.append(bank.validation(
            "transcription_missing", severity=sev, item_id=iid, printed_no=pn,
            detail=(f"原文独有 {n} 字 {raw!r}｜上下文 …{before}⟨{raw}⟩{after}…"
                    f"｜纸上位置 第 {page_no} 页 {[round(v, 1) for v in box]}"),
            count=n, actual=n))

    # —— 公式存在性 + 选项字母防发明
    vs.extend(verify_formulas(page, item, region, iid, pn, src_raw))
    vs.extend(check_invented_letters(item, src_raw, markers=markers))
    return vs, {"checked": True, "page": page_no,
                "rect": [round(v, 1) for v in region],
                "transcript_tokens": len(t_tokens), "source_tokens": len(s_tokens),
                "option_markers_dropped": markers,
                "extra": sum(r["len"] for r in extra),
                "missing": sum(r["len"] for r in missing),
                "observed": found}


_FURNITURE = re.compile(r"(第\s*\d+\s*页|共\s*\d+\s*页|版权所有|咨询热线|翻版必究|"
                        r"圆您公职梦|绝密★启用前)")


def _is_page_furniture(raw: str) -> bool:
    """这段原文是否是页眉页脚/水印一类非题目内容。

    刻意保守：要么整段就是一个数字（页码），要么命中版面关键词。
    放宽会让真题干被当成噪声豁免掉——那正是我们要防的静默丢失。
    """
    s = raw.strip()
    if not s:
        return False
    if re.fullmatch(r"\d{1,3}", s):
        return True
    return bool(_FURNITURE.search(s))


def formula_groups(item: dict) -> list[tuple[str, str]]:
    """收集条目里所有 $...$ 组：[(所在字段, 公式体)]。"""
    out: list[tuple[str, str]] = []

    def scan(field: str, value: str) -> None:
        for m in _DOLLAR.finditer(value or ""):
            body = m.group(1).strip()
            if body:
                out.append((field, body))

    scan("stem", item.get("stem") or "")
    for letter, v in sorted((item.get("options") or {}).items()):
        scan(f"option.{letter}", v)
    for i, v in enumerate(item.get("subs") or []):
        scan(f"sub.{i + 1}", v)
    for i, v in enumerate(item.get("appendices") or []):
        scan(f"appendix.{i + 1}", v)
    return out


def literal_probe(formula: str) -> str:
    """公式体 → 若它被原样打印在纸上，文本层应当读得到的令牌序列。"""
    body = _LATEX_CMD.sub("", formula).replace("\\", "")
    return bank.normalize_for_compare(body, keep_math=True)


def anchor_candidates(formula: str) -> list[str]:
    """公式体 → 可能在文本层留下的字形线索（原始字符，不是令牌）。

    MathType 会把结构丢掉只剩操作数：`\\sqrt{7}` 在文本层就是 '7'，
    `(1+5i)i` 在文本层是 '1+5i' 与 'i'。所以取公式里的字母数字前缀作锚点。
    """
    body = _LATEX_CMD.sub(" ", formula)
    tokens = re.findall(r"[0-9A-Za-z]+", body)
    out: list[str] = []
    for size in (6, 4, 3, 2, 1):
        for t in tokens:
            if len(t) >= size and t[:size] not in out:
                out.append(t[:size])
    return out


def verify_formulas(page: "fitz.Page", item: dict, region: list[float],
                    iid: str | None, pn: int | None, src_raw: str) -> list[dict]:
    """每个 $...$ 组必须满足下列三条之一，否则判定为「该公式无物证」：

      A. 公式体去掉 LaTeX 命令后可在区域文本层逐字符读到（现代 LaTeX 导出，结构本就在文本里）；
      B. 锚点邻域内的矢量线条罩住了可读字形（MathType 根号横钩穿过操作数——√7 的 '7' 落在勾线框内）；
      C. 锚点旁紧贴着私用区结构字形（大括号、竖线这类不压线的符号，如 (1+5i)i 的那对括号）。

    三条都不满足 = 既读不到、也看不到它印在纸上的痕迹 → error。
    这正是旧核验器的盲区：它先剥掉 $...$ 再只比汉字，公式压根不在检查范围内，
    `1+5i i`→`(1+5i)i`（括号丢）与 `7 倍`→`√7 倍`（根号丢）都无从暴露。
    """
    groups = formula_groups(item)
    if not groups:
        return []
    out: list[dict] = []
    for field, formula in groups:
        probe = literal_probe(formula)
        if len(probe) >= 2 and probe in bank.normalize_for_compare(src_raw, keep_math=True):
            continue                                        # A
        anchors = anchor_candidates(formula)
        matched: list[list[float]] = []
        used_anchor = ""
        for cand in anchors:
            matched = G.glyph_rects_for_text(page, cand, region)
            if matched:
                used_anchor = cand
                break
        if matched:
            ratio, biggest, band = G.formula_support_ratio(page, matched, region)
            if ratio >= FORMULA_INK_RATIO and biggest and _covers_glyphs(biggest, matched):
                continue                                    # B
            puas = G.formula_glyph_rects(page, G.expand_rect(matched[0], 10.0, clip=region))
            if puas and _anchors_inside(matched, puas):
                continue                                    # C
            out.append(bank.validation(
                "formula_unverified", severity="error", item_id=iid, printed_no=pn,
                detail=(f"{field} 的 ${formula}$：锚点 {used_anchor!r} 邻域内"
                        f"非文本层墨迹占比 {ratio:.3f}（通过线 {FORMULA_INK_RATIO}，"
                        f"搜索窗口 {[round(v, 1) for v in band]}），"
                        f"最大墨迹块 {[round(v, 1) for v in biggest] if biggest else '无'}"
                        f"→ 该墨迹既未罩住该公式的可读字形，旁边也没有私用区结构字形，"
                        f"即该公式的结构（根号/分数线/括号）在原件此处找不到物证")))
        else:
            out.append(bank.validation(
                "formula_unverified", severity="error", item_id=iid, printed_no=pn,
                detail=(f"{field} 的 ${formula}$ 在本条目区域内既读不到原文、"
                        f"也定位不到任何锚点（候选 {anchors[:4]}）→ 该公式无物证")))
    return out


def _covers_glyphs(ink: list[float], glyph_rects: list[list[float]]) -> bool:
    """墨迹包围盒是否罩住至少一个可读字形——MathType 结构线穿过操作数的特征。"""
    for g in glyph_rects:
        if (ink[0] <= g[0] + 0.8 and ink[1] <= g[1] + 0.8
                and ink[2] >= g[2] - 0.8 and ink[3] >= g[3] - 0.8
                and G.rect_area(ink) >= G.rect_area(g) * 0.8):
            return True
    return False


def _anchors_inside(glyph_rects: list[list[float]], marks: list[list[float]]) -> bool:
    """私用区结构字形是否紧贴操作数：同一行高度带内、水平间隙小于约一个字宽。"""
    for g in glyph_rects:
        h = max(1e-6, g[3] - g[1])
        for m in marks:
            if abs((m[1] + m[3]) / 2 - (g[1] + g[3]) / 2) <= h * 0.8:
                gap = max(m[0] - g[2], g[0] - m[2])
                if gap <= h * 1.0:
                    return True
    return False


def check_invented_letters(item: dict, src_raw: str, *,
                           markers: list[str]) -> list[dict]:
    """选项字母防发明：转写里有某个字母标号，而该条目区域内连这个字母都没出现过 → error。

    protocol.md 明令禁止发明选项字母；这条检查把它变成可机器判定的约束。
    注意只在「区域文本层完全没有该字母」时才报，避免与 transcript_seq 的宽松处理重复计数。
    """
    out: list[dict] = []
    up = src_raw.upper()
    for c in markers:
        if c.upper() not in up:
            out.append(bank.validation(
                "transcription_extra", severity="error", item_id=item.get("id"),
                printed_no=item.get("printed_no"),
                detail=(f"转写了选项 {c}. 但该条目区域内从未出现字母 {c}"
                        f"→ 违反「绝不发明选项字母」，必须改为 unparseable_reason"),
                expected=c, actual=c))
    return out


# ---------------------------------------------------------------------------
# 整库验收
# ---------------------------------------------------------------------------
def verify_draft(draft: dict, pdf_path: str | Path, *,
                 min_len: int = MIN_LEN_DEFAULT,
                 allow_entries: list[dict] | None = None) -> dict:
    """验收草稿，返回一份**新的** v2 题库（不修改入参）。

    结论规则：只有零 error 记录且 meta.loss 账目平，才会 verified=True；
    而 verified 最终还要靠外部独立核验置位，这里只保证「不被虚假点亮」。
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise VerifyError(f"源 PDF 不存在，无法验收：{pdf_path}")
    allow_entries = allow_entries or []
    bank.require_v2(draft, where="待验收草稿")

    doc = G.open_pdf(str(pdf_path))
    try:
        items = draft.get("items") or []
        validations: list[dict] = []
        used: set[str] = set()
        observed: dict[tuple[Any, str], list[str]] = {}
        allow = {}
        for e in allow_entries:
            allow[allow_key(e.get("item_id"), e.get("code", ""), e["expected_diff"])] = e
        checked = 0
        for it in items:
            try:
                vs, summary = verify_item(doc, it, min_len=min_len,
                                          allow=allow, used_allow=used)
            except Exception as exc:            # 单条崩掉不能带走整卷结果
                vs = [bank.validation(
                    "verify_crashed", severity="error", item_id=it.get("id"),
                    printed_no=it.get("printed_no"),
                    detail=(f"验收器处理该条目时抛出 {type(exc).__name__}: {exc}"
                            f"→ 该条目的比对未完成，必须修数据或修验收器"))]
                summary = {"checked": False}
            validations.extend(vs)
            for code, frags in (summary.get("observed") or {}).items():
                observed.setdefault((it.get("id"), code), []).extend(frags)
            if summary.get("checked"):
                checked += 1
            if it.get("provenance") and it.get("images") == [] and it.get("crop_required"):
                validations.append(bank.validation("crop_missing", severity="warn",
                                                   item_id=it.get("id"),
                                                   printed_no=it.get("printed_no")))
        # 白名单里没被用到的登记 → 逐条报错
        leftovers = check_allow_list(allow_entries, used=used, observed=observed)
        for e in allow_entries:
            k = allow_key(e.get("item_id"), e.get("code", ""), e["expected_diff"])
            if k in used:
                validations.append(bank.validation(
                    "source_defect", severity="info", item_id=e.get("item_id"),
                    printed_no=e.get("printed_no"),
                    detail=(f"已核实的原件缺陷，差异内容与登记吻合故豁免："
                            f"diff={e['expected_diff']!r}｜依据={e['evidence']}")))
        validations.extend(leftovers)

        # 结构性检查：缺损题必须降级而非消失
        loss = dict(draft.get("meta", {}).get("loss") or {})
        if not loss:
            raise VerifyError("草稿缺少 meta.loss：抄题端点必须交回损失账目，"
                              "否则无法判断有没有题被静默删除")
        seen = loss.get("seen_list")
        if seen is None:
            raise VerifyError("草稿 meta.loss.seen_list 缺失：只有计数无法逐号审计")
        kept_pns = {int(i["printed_no"]) for i in items if i.get("printed_no") is not None}
        dropped_pns = {int(d["printed_no"]) for d in (loss.get("dropped") or [])}
        never = sorted({int(n) for n in seen} - kept_pns - dropped_pns)
        if never:
            validations.append(bank.validation(
                "never_parsed", severity="error", ids=never,
                detail="卷面出现过这些题号，但草稿既没有条目也没有 drop 记录"))
        for d in loss.get("dropped") or []:
            validations.append(bank.validation(
                "draft_dropped", severity="warn", printed_no=int(d["printed_no"]),
                detail=f"草稿自报已丢弃：{d['reason']}"))

        # 报告里每一句「第 N 题」都必须真的指向第 N 题：先稽核身份再渲染，
        # 否则验收器自己就成了那类"描述与数据不符"的来源。
        validations.extend(bank.audit_validations(items, validations))

        result = {
            "meta": {**draft.get("meta", {}),
                     "source": pdf_path.name,
                     "pages": doc.page_count,
                     "verification": {
                         "verifier": "src/extract/verify.py",
                         "verified_at": bank._dt.datetime.now(bank._dt.timezone.utc)
                                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "items_with_provenance_checked": checked,
                         "total_items": len(items),
                         "allow_list_size": len(allow_entries),
                         "allow_list_used": len(used),
                         "thresholds": {"min_len": min_len,
                                        "formula_ink_ratio": FORMULA_INK_RATIO},
                     }},
            "sections": draft.get("sections") or [],
            "passages": draft.get("passages") or [],
            "items": [dict(i) for i in items],
            "validations": validations,
        }
        result["report"] = bank.render_bank_report_lines(result)
        bank.apply_verified(result)
        return result
    finally:
        doc.close()


def summarize(result: dict) -> dict[str, Any]:
    """给上层用的紧凑摘要（不含题目内容，可安全打印）。"""
    vs = result["validations"]
    by_code: dict[str, int] = {}
    for v in vs:
        by_code[v["code"]] = by_code.get(v["code"], 0) + 1
    return {
        "verified": result["meta"].get("verified"),
        "blockers": result["meta"].get("verified_blockers") or [],
        "errors": sum(1 for v in vs if v["severity"] == "error"),
        "warns": sum(1 for v in vs if v["severity"] == "warn"),
        "by_code": by_code,
        "checked": result["meta"]["verification"]["items_with_provenance_checked"],
    }
