"""题库信封（bank envelope）的唯一构造器：v2 schema、结构化校验记录、损失账目与体检报告。

为什么要单独有这一层
--------------------
历史上每份试卷各写一个解析脚本，于是同一件事被实现了四遍且互相漂移：
  · 信封组装（meta/sections/passages/items/validations）
  · 题号正则（{1,2} / {1,3} / 坐标定位三种）、小数守卫（701.6 不是第 701 题）、MAX_QNO
  · 选项字母切分（[A-G] 与 [A-D]×N、OPT_SPLIT/OPT_LOOSE）
  · printed_no/no/id/seq_note 的卷内连续重编号（两份脚本逐字重复）
  · 分节归桶（两份脚本逐字重复）
  · 水印/图表页判定（同尺寸图片出现在 >80% 页 → 水印）
  · 体检报告打印（四套 print("=" * 78)）

本模块只做一次实现，四个解析脚本退化为「版式专用抽取 + 调用这里」的薄壳。

诚实性约束在数据层的落点
------------------------
1. Validation 是**结构化记录**，ids 永不截断；人类可读的中文报告由记录渲染而来，
   反向（只有一句没有记录支撑的话）在本层不可能发生。
2. meta.loss 是**必填**字段：卷面出现过的题号、实际保留的题号、丢失原因全部入账。
   loss.dropped 非空的题库不能被判为已核验。
3. 缺损的题**降级而不是删除**：complete_item() 只会把 answerable 置 False 并给出原因，
   绝不丢弃条目——静默丢题正是旧解析器最大的问题。
4. bank_version=2；读到 v1（或无版本号）的旧题库时抛 BankVersionError，
   要求重新生成，而不是猜测字段含义后继续用。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent

BANK_VERSION = 2
SEVERITIES = ("error", "warn", "info")

# 校验严重度会直接影响题库能否被标记为已核验，因此集中定义而非散落各处
SEVERITY_LABEL = {"error": "错误", "warn": "告警", "info": "说明"}


class BankVersionError(RuntimeError):
    """题库版本不被当前代码支持。刻意不猜字段，避免把 v1 误读成 v2。"""


# ---------------------------------------------------------------------------
# 文本归一化与比对基元
# ---------------------------------------------------------------------------
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_MATH_SPAN = re.compile(r"\$[^$]*\$")
_WS = re.compile(r"[ \t\u3000]+")


def cjk_only(text: str) -> str:
    """只保留中日韩汉字。用于「文字层」比对：公式、字母、标点一律剔除。"""
    return "".join(_CJK.findall(text))


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def classify_char(ch: str) -> str | None:
    r"""把字符归一为比对令牌。

    规则：**汉字保留本身**，其它一切符号塌成两类记号——拉丁/希腊等字母与数字记 '@'，
    其余可见符号记 '#'。为什么这样切：
      · 这些试卷的文本层里，印在纸上的全角「，」常被编码成半角 ','、「、」有时整个消失，
        MathType 还把 A、1、x 这类字形拆成独立片段；逐字符严格比对会产生几百条假告警，
        旧实现因此走到另一个极端（只留汉字、其余全丢），公式连「有没有、多长、什么形状」
        都不在检查范围内。
      · 塌成 '@'/'#' 之后被恢复的是**形状**：公式的数量、长度、结构（分数线、根号、括号
        的相对位置）可比；而 `x+1` 与 `y+2`、`\sqrt{2}` 与 `\sqrt{3}`、`\sin` 与 `\cos`
        归一化后逐字符相同——**公式内容仍然看不见**。这一点由 tests/test_bank.py 钉成
        显式限度，别再当成"公式已纳入自动校验"。
      · 汉字在这批 PDF 里解码可靠、且是题目内容的本体，所以它必须逐字可比——
        「疾病」被写成「疾并」要能抓到，编造一句中文更要能抓到。
        （注：一字之差在 align_runs 的 min_len=2 下仍会漏，见该函数文档。）
      · 私用区（U+E000–U+F8FF）是 MathType 的内部结构标记，读不出内容，直接丢弃；
        它的物证价值由 extract/geometry.py 当作「非文本层墨迹」单独使用。
    """
    if ch.isspace() or unicodedata.category(ch).startswith(("Z", "C")):
        return None
    o = ord(ch)
    if 0xE000 <= o <= 0xF8FF:
        return None
    if _CJK.match(ch):
        return ch
    cat = unicodedata.category(ch)
    if cat.startswith(("L", "N")):
        return "@"
    return "#"


def normalize_for_compare(text: str, keep_math: bool = True) -> str:
    """比对用的令牌序列。keep_math=False 时先剥掉 $...$ 段——
    公式的存在性由 verify.formula_unverified 用墨迹证据单独负责，两类检查不互相稀释。"""
    t = text if keep_math else _MATH_SPAN.sub(" ", text)
    return "".join(tok for tok in (classify_char(ch) for ch in t) if tok)


def normalized_chars(text: str, keep_math: bool = True) -> list[str]:
    """归一化后拆成令牌列表（已无空白）。"""
    return list(normalize_for_compare(text, keep_math))


def longest_common_substring(a: str, b: str) -> int:
    """最长公共子串长度（滚动数组，O(len(a)*len(b))）。用于给 diff 找上下文锚点。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def context_window(seq: str, pos: int, radius: int = 14) -> str:
    """取 seq 中 pos 附近的一段作为人可读证据（带省略号）。"""
    lo = max(0, pos - radius)
    hi = min(len(seq), pos + radius)
    return ("…" if lo else "") + seq[lo:hi] + ("…" if hi < len(seq) else "")


def align_token_lists(t: list[str], s: list[str],
                      min_len: int = 2) -> tuple[list[dict], list[dict]]:
    """在**已归一化的令牌列表**上做贪心双向对齐。align_runs 的核。

    单独暴露这个入口是给验收器用的：它手里是「逐字符令牌 + 逐字符包围盒」两个
    平行数组，位置就是证据的一部分。若让它改走 align_runs，令牌会被再归一化一次
    （'@' 落到 '#'），字母数字与符号从此无从区分，而 pos 又要用来回指纸面，
    等于一边降级判据一边假装位置可信。

    返回的每个片段 {"text","pos","len"} 里的 pos/len 都是**入参列表的下标**，
    调用方可直接用 pos 取到自己那份平行数组。
    """
    t_free = bytearray(b"\x01") * len(t)
    s_free = bytearray(b"\x01") * len(s)

    # 外层走原文、内层用 pos_t 直接取「转写里同令牌的位置」，于是每轮开销由
    # **较小的一侧**界定（转写总是比原文短），而不是 O(len(t)*len(s))。
    #   · 不能像旧实现那样"跳过相邻等值令牌"：等值段中间仍可能是唯一可用的对齐起点
    #     （归一化把成串符号塌成同一个 '#'，公式密集处极常见），跳过去就把整段
    #     对得上的转写报成"原文里没有"。
    #   · 表每轮重建、只收仍可用的位置：静态表会把已配对的候选一律遍历再拒绝，
    #     轮数一多就是十几倍的空转；重建只花 O(len(t))，且结果逐字节相同。
    # 配对判据不变：最长公共子串低于 min_len 即停，宁多报不静默。
    def free_positions(seq: list[str], free: bytearray) -> dict[str, list[int]]:
        d: dict[str, list[int]] = {}
        for k, tok in enumerate(seq):
            if free[k]:
                d.setdefault(tok, []).append(k)
        return d

    pos_t = free_positions(t, t_free)

    def longest() -> tuple[int, int, int] | None:
        best = None
        for j in range(len(s)):
            if not s_free[j]:
                continue
            for i in pos_t.get(s[j], ()):
                L = 0
                while (i + L < len(t) and j + L < len(s)
                       and t_free[i + L] and s_free[j + L] and t[i + L] == s[j + L]):
                    L += 1
                if best is None or L > best[0]:
                    best = (L, i, j)
                    if L >= 40:
                        return best
        return best

    while True:
        found = longest()
        if not found or found[0] < min_len:
            break
        L, i, j = found
        for k in range(i, i + L):
            t_free[k] = 0
        for k in range(j, j + L):
            s_free[k] = 0
        pos_t = free_positions(t, t_free)

    def runs(buf: bytearray, seq: list[str]) -> list[dict]:
        out: list[dict] = []
        k = 0
        while k < len(seq):
            if buf[k]:
                e = k
                while e < len(seq) and buf[e]:
                    e += 1
                out.append({"text": "".join(seq[k:e]), "pos": k, "len": e - k})
                k = e
            else:
                k += 1
        return [r for r in out if r["len"] >= min_len]

    return runs(t_free, t), runs(s_free, s)


def align_runs(transcript: str, source: str, keep_math: bool = True,
               min_len: int = 2) -> tuple[list[dict], list[dict]]:
    """双向局部对齐：返回 (转写独有片段, 原文独有片段)。入参是**原始文本**。

    做法是把两侧各自归一化成令牌序列，反复取出**双侧都还存在的最长公共子串**并标记为已配对，
    直到找不到长度 >= min_len 的公共子串；剩下的就是各自的独有片段。
    这样同时检查「转写里有、原文里没有」（疑似编造）与
    「原文里有、转写里没有」（疑似漏抄）——旧实现只做前者，且做在全卷范围上，
    于是只要字符在任意一页出现过就算通过，locality 全被忽略。

    三个已实测确认的盲区（tests/test_bank.py 把它们钉成显式限度，别当"已覆盖"）：
      · **只比内容不比顺序**：配对不看位置，两个大段落互换位置仍算全对齐。
        它保证"没有编造、没有漏抄"，不保证"抄对了顺序"。
      · **小于 min_len 的差异被当噪声滤掉**：默认 min_len=2 ⇒ 一字之差（疾病→疾并）看不见，
        所以已知原件缺陷必须走人工台账，不能指望这里。
      · **公式内容看不见**：字母与数字都塌成 '@'，`\\sqrt{2}` 与 `\\sqrt{3}` 归一化后相同。
        公式的存在性由 extract/geometry.py 用非文本层墨迹单独负责。

    复杂度：外层走原文、内层用 pos_t 取候选，单轮由较小一侧界定；主循环轮数 = 配对数，
    每轮重建 pos_t（O(len(t))）。实测本机：8k 令牌的现实题面 48.8ms（旧跳转表 106.1ms）、
    1.6k 令牌的全异字符病态输入 9.4ms（旧跳转表在 800 令牌上就要 218ms）。
    设计上是**每题一次**的局部比对，别拿全卷文本喂它。
    """
    return align_token_lists(normalized_chars(transcript, keep_math),
                             normalized_chars(source, keep_math), min_len)


# ---------------------------------------------------------------------------
# 题号 / 选项 / 小数守卫 / MAX_QNO —— 四处实现的统一版
# ---------------------------------------------------------------------------
Q_NUM_TAIL_TEMPLATE = re.compile(r"^\s*(\d{1,DIGITS})\s*[.．、]\s*(.*)$")
DECIMAL_GUARD_TEMPLATE = re.compile(r"^\s*\d{1,DIGITS}\s*[.．]\s*\d")
LETTER_OPTION = re.compile(r"(?:(?<=^)|(?<=[\s。；、]))([A-G])\s*[.．、)]\s*")
LETTER_BARE = re.compile(r"^\s*([A-G])\s*[.．、)]?\s*$")
LETTER_LOOSE = re.compile(r"^\s*([A-G])\s+(\S.*)$")


def question_number_re(max_digits: int = 3) -> re.Pattern[str]:
    """题号行正则。旧代码里英语用 {1,2}、行测/银行用 {1,3}，语义相同只是位数不同。"""
    return re.compile(Q_NUM_TAIL_TEMPLATE.pattern.replace("{1,DIGITS}", "{1,%d}" % max_digits))


def looks_like_decimal_prefix(text: str, max_digits: int = 3) -> bool:
    """'701.6 万吨' 不是第 701 题：行首数字后紧跟小数点再跟数字的一律排除。

    三个脚本各写了一份这个守卫（parse_xingce_pdf.py:48 / parse_bank_pdf.py:114 / :307），
    这里合并成一个，并与题号正则共用位数参数。
    """
    return bool(re.compile(DECIMAL_GUARD_TEMPLATE.pattern.replace("{1,DIGITS}", "{1,%d}" % max_digits)).match(text))


def extract_question_number(text: str, *, max_digits: int = 3, max_qno: int = 200,
                            started: bool = True) -> int | None:
    """统一的「这一行是不是开了一道新题」判定：题号位数、小数守卫、卷面题数上限、开考标志。

    started=False 时一律不认题号——卷首「注意事项」也是编号的，遇到正式小节才算开考。
    """
    if not started or looks_like_decimal_prefix(text, max_digits):
        return None
    m = question_number_re(max_digits).match(text)
    if not m:
        return None
    no = int(m.group(1))
    return no if 1 <= no <= max_qno else None


def option_letters(letters: Iterable[str] = "ABCD") -> str:
    """该卷允许的选项字母集合（英语七选五是 A–G，其余多为 A–D）。"""
    return "".join(sorted(set(letters)))


def split_inline_options(line: str, allowed: str = "ABCD",
                         require_separator: bool = True) -> tuple[str, dict[str, str]]:
    """一行里可能塞了多个选项：'A.6B.8' / 'A.xx B.yy C.zz D.ww'。

    返回 (选项之前的正文, {字母: 内容})。require_separator=True 时，除行首外
    只有紧跟空白或行首位置才认字母，避免把英文单词首字母（'D 项'）当选项。
    """
    allowed_set = set(allowed)
    pat = LETTER_OPTION if require_separator else re.compile(
        r"(?:^|[\s。；、])([A-G])\s*[.．、)]\s*")
    hits = [m for m in pat.finditer(line) if m.group(1) in allowed_set]
    if not hits:
        return line.strip(), {}
    opts: dict[str, str] = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(line)
        opts.setdefault(m.group(1), line[m.end():end].strip())
    return line[:hits[0].start()].strip(), opts


def match_option_line(line: str, allowed: str = "ABCD") -> tuple[str, str] | None:
    """独立成行的选项：'A. 内容' / 'A、内容' / 'A) 内容'。"""
    m = re.match(r"^\s*([A-G])\s*[.．、)]\s*(.*)$", line)
    if not m or m.group(1) not in set(allowed):
        return None
    return m.group(1), m.group(2).strip()


def match_loose_option(line: str, have: dict[str, str], allowed: str = "ABCD") -> tuple[str, str] | None:
    """无标点写法 'B stronger'：仅当它是已有选项的**下一个**字母时才认。

    首个选项必须写作 'A.'，否则任何以大写单字母开头的正文都会被误判为选项。
    """
    m = LETTER_LOOSE.match(line)
    if not m or m.group(1) not in set(allowed):
        return None
    letter = m.group(1)
    if not have:
        return None
    if ord(letter) != ord(list(have)[-1]) + 1:
        return None
    return letter, m.group(2).strip()


def bare_letter(line: str, allowed: str = "ABCD") -> str | None:
    """整行只有一个选项字母（'A'）——篇章标签或图形推理被抽成图片后剩下的残迹。"""
    m = LETTER_BARE.match(line)
    if not m or m.group(1) not in set(allowed):
        return None
    return m.group(1)


def trailing_answer_letter(stem: str, allowed: str = "ABCD") -> tuple[str, str] | None:
    """题干尾部孤零零挂着一个答案字母：'…呈现一定的规律性。A'。

    这类行的含义是「印刷体的正确答案被吸进了题干行」，而 A/B/C/D 四个选项本身是图片。
    剥离它可以让题面干净，但它同时是「本题选项未被抽出」的强信号，故返回给调用方记账。
    """
    m = re.search(r"([\u4e00-\u9fff。！？])[.．]?\s*([A-G])\s*$", stem)
    if not m or m.group(2) not in set(allowed):
        return None
    head = stem[:m.start(2)].rstrip()
    head = re.sub(r"[.．]$", "", head).rstrip()
    if len(head) < 6:
        return None
    return head, m.group(2)


MIN_OPTIONS_DEFAULT = 3


def missing_letters(letters: Iterable[str], allowed: str = "ABCD") -> list[str]:
    """按期望字母表列出缺失项，供 unparseable_reason 使用。"""
    have = set(letters)
    return [c for c in allowed if c not in have]


# ---------------------------------------------------------------------------
# 页面几何：块流、水印/图表页、题目区域
# ---------------------------------------------------------------------------
def page_blocks(page, join_lines: bool = True) -> list[dict]:
    """把一页压成「块流」：文本块带 lines/text/bbox，图像块带 kind=image。

    保留阅读顺序（PyMuPDF 原始 block 序）而不是按 y 重排：多栏与图文混排时
    原始顺序更接近真实阅读顺序，也更接近旧 load_pages 的行序。
    """
    out: list[dict] = []
    for blk in page.get_text("dict")["blocks"]:
        bbox = fitz_rect_list(blk["bbox"])
        if blk.get("type") == 1:
            out.append({"kind": "image", "bbox": bbox, "lines": [], "text": ""})
            continue
        lines: list[str] = []
        for ln in blk.get("lines", []):
            t = re.sub(r"\s+", " ", "".join(sp["text"] for sp in ln["spans"])).strip()
            if t:
                lines.append(t)
        text = "\n".join(lines) if join_lines else " ".join(lines)
        if not lines:
            continue
        out.append({"kind": "text", "bbox": bbox, "lines": lines, "text": text})
    return out


def fitz_rect_list(bbox) -> list[float]:
    return [float(v) for v in bbox]


def rect_union(rects: list[list[float]]) -> list[float] | None:
    if not rects:
        return None
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[2] for r in rects)
    y1 = max(r[3] for r in rects)
    return [x0, y0, x1, y1]


def expand_rect(rect: list[float], pad: float = 2.5, clip: list[float] | None = None) -> list[float]:
    """向四周扩 pad 磅。MathType 的结构线（根号、分数线、括号）常略宽于字形包围盒，
    紧贴字形会把它们排除在外，公式存在性检查就失去意义。"""
    x0, y0, x1, y1 = rect
    if clip:
        cx0, cy0, cx1, cy1 = clip
        return [max(cx0, x0 - pad), max(cy0, y0 - pad), min(cx1, x1 + pad), min(cy1, y1 + pad)]
    return [x0 - pad, y0 - pad, x1 + pad, y1 + pad]


def span_rects(page, clip: list[float] | None = None) -> list[list[float]]:
    """页面上所有文字字形包围盒（可按矩形裁剪），用于从「墨迹」中扣除「文本」。"""
    out: list[list[float]] = []
    d = page.get_text("rawdict", clip=clip) if clip else page.get_text("rawdict")
    for blk in d["blocks"]:
        if blk.get("type") == 1 or "lines" not in blk:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    r = fitz_rect_list(ch["bbox"])
                    if r[2] - r[0] > 0.05 and r[3] - r[1] > 0.05:
                        out.append(r)
    return out


def watermark_sizes(doc, ratio: float = 0.8) -> set[tuple[int, int]]:
    """在超过 ratio 比例页面上出现的同尺寸图片 = 页脚水印，不是内容图表。"""
    total = doc.page_count
    hits: dict[tuple[int, int], int] = {}
    for page in doc:
        for s in {(im[2], im[3]) for im in page.get_images(full=True)}:
            hits[s] = hits.get(s, 0) + 1
    return {s for s, n in hits.items() if n > total * ratio}


def figure_pages(doc, ratio: float = 0.8) -> list[int]:
    """含真实图表的页码（1 起）。

    优先用块流里的 image 块（版面级证据）；若该 PDF 的块流不含图像块
    （部分导出器会把图片留成 XObject），回落到 page.get_images() 并排除水印尺寸。
    """
    wm = watermark_sizes(doc)
    out: list[int] = []
    for idx in range(doc.page_count):
        blocks = page_blocks(doc[idx])
        if any(b["kind"] == "image" for b in blocks):
            out.append(idx + 1)
            continue
        sizes = {(im[2], im[3]) for im in doc[idx].get_images(full=True)}
        if any(s not in wm for s in sizes):
            out.append(idx + 1)
    return sorted(out)


def locate_question_spans(doc, *, lo: int = 1, hi: int = 999,
                          max_left_x: float = 130.0,
                          pattern: re.Pattern[str] | None = None) -> dict[int, list[tuple[int, float]]]:
    """在文本层里按坐标定位题号（'12.' 独占一个 span 且靠左）。

    这是数学卷的做法：它的题号不在行首（公式把行打散了），只能用坐标找。
    规整版式的卷子应该用 question_number_re 扫行，不必走这里。
    """
    pat = pattern or re.compile(r"(\d{1,3})\s*[.．]")
    found: dict[int, list[tuple[int, float]]] = {}
    for pno in range(doc.page_count):
        for blk in doc[pno].get_text("dict")["blocks"]:
            if blk.get("type") == 1:
                continue
            for line in blk.get("lines", []):
                for sp in line["spans"]:
                    t = sp["text"].strip()
                    m = re.fullmatch(pat.pattern, t)
                    if m and sp["bbox"][0] < max_left_x:
                        no = int(m.group(1))
                        if lo <= no <= hi:
                            found.setdefault(no, []).append((pno, sp["bbox"][1]))
    return found


# ---------------------------------------------------------------------------
# 句子切分与篇章构造（原先困在 parse_pdf.py 里，无人复用）
# ---------------------------------------------------------------------------
ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "u.s", "u.k", "no", "fig"}
_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\u201c(])'
                         r'|(?<=[.!?][\u201d"\u2019)])\s+(?=[A-Z"\u201c(])')
_BLANK_MARK = re.compile(r"\[(\d{1,3})\]_{2,}")


def normalize_blanks(text: str) -> str:
    """把 "…\n41\n________ …" 归一为 "… [41]____ …"。必须在合并折行之前做。"""
    text = re.sub(r"\s*(\d{1,3})\s*\n\s*_{2,}", r" [\1]____ ", text)
    return re.sub(r"[ \t]+", " ", text)


def join_lines_into_paragraphs(text: str, blank_only=None) -> list[str]:
    """折行还原为段落；空行（或纯题空碎片行）视为段落分隔。"""
    blank = blank_only or re.compile(r"^[\s_，、]*$")
    paras: list[str] = []
    buf: list[str] = []
    for s in text.splitlines():
        if blank.match(s):
            if buf:
                paras.append(" ".join(buf))
                buf = []
            continue
        buf.append(s.strip())
    if buf:
        paras.append(" ".join(buf))
    return [re.sub(r"\s+", " ", p).strip() for p in paras if p.strip()]


def split_sentences(text: str) -> list[str]:
    """切句：容忍句末标点后紧跟引号/括号；常见缩写处不误切；纯题空碎片并入后句。"""
    parts = _SENT_SPLIT.split(text)
    merged: list[str] = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if merged:
            tail = merged[-1].lower().rstrip().rstrip(".")
            if not tail.endswith(".") and tail.split() and tail.split()[-1] in ABBREV:
                merged[-1] = merged[-1] + " " + p
                continue
        merged.append(p)
    out: list[str] = []
    pending = ""
    for p in merged:
        if re.fullmatch(r"(\[\d{1,3}\]_{2,}\s*)+", p):
            pending += p + " "
            continue
        out.append((pending + p).strip())
        pending = ""
    if pending:
        if out:
            out[-1] += " " + pending.strip()
        else:
            out.append(pending.strip())
    return out


def make_passage(pid: str, label: str | None, lines: list[tuple[str, int]],
                 figures: set[int] | None = None, detect_title: bool = False) -> dict:
    """由 (行, 页码) 序列构造篇章：段落、逐句编号、题空号、所在页、是否含图表。"""
    figures = figures or set()
    title = ""
    if detect_title and lines:
        head = lines[0][0]
        # 标题行特征：短，且不以句末标点结束
        if len(head) <= 70 and not re.search(r"[.!?\u201d\u2019]$", head):
            title = head
            lines = lines[1:]
    raw = "\n".join(s for s, _ in lines)
    paras = join_lines_into_paragraphs(normalize_blanks(raw))
    full = " ".join(paras)
    pages = sorted({p for _, p in lines})
    fig = sorted(p for p in pages if p in figures)
    return {
        "id": pid, "label": label, "title": title,
        "paragraphs": paras,
        "sentences": [{"no": f"S{i + 1}", "text": t} for i, t in enumerate(split_sentences(full))],
        "blanks": sorted({int(x) for x in _BLANK_MARK.findall(full)}),
        "pages": pages, "has_figure": bool(fig), "figure_pages": fig,
        "chars": len(full),
    }


# ---------------------------------------------------------------------------
# 结构化校验记录
# ---------------------------------------------------------------------------
def validation(code: str, *, severity: str = "warn", item_id: str | None = None,
               printed_no: int | None = None, detail: str = "",
               ids: list[int] | None = None, count: int | None = None,
               expected: Any = None, actual: Any = None) -> dict:
    """构造一条 Validation。ids **永不截断**。

    已知 code（新增需在此登记，保证报告的中文渲染有条可依）：
      options_incomplete          选项数不足（降级为不可作答，不删题）
      options_are_images          选项在原件中是图片，文本层未抽出（图形推理）
      options_merged_in_stem      选项与题干挤在同一行且未逐个识别
      stem_suspicious             题干异常（空、过短、被截断迹象）
      no_standard_answer          未取到标准答案（不影响作答，仅无法对照评分）
      duplicate_printed_no        卷面题号重复出现
      identity_mismatch           校验记录指向的条目与它自称的卷面号不一致（编号顺序被写坏）
      never_parsed                卷面出现过该题号但一道条目也没产出
      draft_dropped               草稿自报该卷面号已被丢弃（含丢弃理由）
      transcription_extra         转写中有、原文区域内没有（疑似编造）
      transcription_unmatched     转写的中文在原件文本层找不到（只比中文，旧式全卷比对）
      transcription_missing       原文区域内有、转写中没有（疑似漏抄）
      formula_unverified          $...$ 公式组附近找不到对应非文本层墨迹
      provenance_missing          条目缺页码/区域来源信息，无法核对
      provenance_unusable         页码/区域指向原件读不出东西的地方 → 比对未执行（不等于通过）
      probe_missing               抄题端点没交回定位锚点（probe）
      probe_not_found             probe 在声称那一页的文本层里找不到 → 抄错或页码报错
      probe_duplicate             两条目的 probe 落在同一行同一列 → 至少一条定位不可信
      probe_unpinned              该条目最终没钉住纸面位置，比对已跳过（降级，不是通过）
      text_layer_missing          条目所在页无文本层（扫描卷）：锚点定位与原文比对均不可用
      verify_crashed              验收器处理该条目时抛出异常 → 比对未完成
      crop_missing                未能按题裁出原图
      section_count_mismatch      小节题数与卷面应有数不符
      blank_count_mismatch        篇章解析出的题空数与卷面不符
      passage_no_sentences        篇章未解析出任何句子
      source_defect               原件本身的缺陷（文本层编码错等），已核实并解释
    """
    if severity not in SEVERITIES:
        raise ValueError(f"未知 severity: {severity}")
    idlist = [int(x) for x in (ids or [])]
    return {
        "code": code,
        "severity": severity,
        "item_id": item_id,
        "printed_no": int(printed_no) if printed_no is not None else None,
        "detail": detail,
        "ids": idlist,
        "count": len(idlist) if count is None else int(count),
        "expected": expected,
        "actual": actual,
    }


def render_validation(v: dict) -> str:
    """由结构化记录渲染一句中文。**只有记录，才有句子**——
    反过来不成立：报告里不会出现无记录支撑的自由文本。"""
    code = v["code"]
    dn = v.get("printed_no")
    who = f"第 {dn} 题" if dn is not None else ("条目 " + v["item_id"] if v.get("item_id") else "全卷")
    ids = v.get("ids") or []
    idtxt = ("：" + "、".join(str(i) for i in ids)) if ids else ""
    table = {
        "options_incomplete": f"{who} 选项只解析到 {v.get('actual')} 个（应不少于 {v.get('expected')}）→ 降级为不可作答{''}",
        "options_are_images": f"{who} 选项在原件中是图片，文本层未抽出任何选项字母 → 不作答、不猜{''}",
        "options_merged_in_stem": f"{who} 选项与题干挤在同一行且未逐个识别（得到 {v.get('actual')} 项）→ 降级为不可作答",
        "stem_suspicious": f"{who} 题干异常：{v.get('detail') or '无可读内容'}",
        # 「全卷：未取到标准答案」是误导——没答案的只是点名的那几十道，且题仍可作答，
        # 缺的只是对照评分用的官方答案。整卷式记录（无 printed_no/item_id）不带前缀。
        "no_standard_answer": (
            f"{who} 未取到标准答案（不影响作答，只是没有可对照评分的官方答案）"
            if v.get("printed_no") is not None or v.get("item_id") else
            f"共 {v.get('count')} 道未取到标准答案（不影响作答，只是没有可对照评分的官方答案）"),
        "duplicate_printed_no": f"卷面题号重复出现，共 {v.get('count')} 个{''}",
        "identity_mismatch": f"校验记录身份不一致：{v.get('detail')}",
        "never_parsed": f"{who}：卷面出现过但未产出任何条目，共 {v.get('count')} 道{''}",
        "draft_dropped": f"{who} 卷面有此题号，草稿自报未收录：{v.get('detail')}",
        "transcription_extra": f"{who} 转写中出现原文该区域没有的内容（疑似编造）：{v.get('detail')}{''}",
        "transcription_unmatched": f"{who} 转写的中文在原件文本层找不到：{v.get('detail')}{''}",
        "transcription_missing": f"{who} 原文该区域有内容未进入转写（疑似漏抄）：{v.get('detail')}{''}",
        "formula_unverified": f"{who} 公式无物证：{v.get('detail')}",
        "provenance_missing": f"{who} 缺少页码/区域来源，无法与原文比对",
        "provenance_unusable": f"{who} 来源区域无法核对：{v.get('detail')}",
        "probe_missing": f"{who} 没有定位锚点（probe），无法在原件上钉住位置：{v.get('detail')}",
        "probe_not_found": f"{who} 的定位锚点在原件该页文本层找不到：{v.get('detail')}",
        "probe_duplicate": f"{who} 与其它条目定位到同一处：{v.get('detail')}",
        "probe_unpinned": f"{who} 未钉住纸面位置，与原文的比对已跳过：{v.get('detail')}",
        "text_layer_missing": f"扫描件无文本层，锚点定位与原文比对不可用{''}",
        "verify_crashed": f"{who} 验收过程中断：{v.get('detail')}",
        "crop_missing": f"{who} 未裁出原图（题号坐标未定位到）",
        "section_count_mismatch": (f"小节 {v.get('detail') or v.get('item_id')} 解析出 {v.get('actual')} 题"
                                   f"（卷面应为 {v.get('expected')} 题）"),
        "blank_count_mismatch": (f"篇章 {v.get('detail')} 解析出 {v.get('actual')} 个题空"
                                 f"（卷面应为 {v.get('expected')} 个）"),
        "passage_no_sentences": f"篇章 {v.get('detail')} 未解析出任何句子",
        "source_defect": f"{who}：原件缺陷（非转写问题）——{v.get('detail')}",
    }
    if code not in table:
        raise KeyError(f"validation code 未在 render_validation 登记：{code}")
    suffix = ""
    if ids and v.get("count", len(ids)) > len(ids):
        suffix = f"（完整名单见 validations，共 {v['count']} 条，此处仅列前 {len(ids)} 条）"
    if v.get("severity") == "info" and code != "source_defect":
        return "ℹ " + table[code] + suffix
    return table[code] + suffix


def render_validations(vs: Iterable[dict]) -> list[str]:
    out = []
    for v in vs:
        s = render_validation(v)
        if s not in out:
            out.append(s)
    return out


def errors_of(validations: Iterable[dict]) -> list[dict]:
    return [v for v in validations if v["severity"] == "error"]


# ---------------------------------------------------------------------------
# 损失账目
# ---------------------------------------------------------------------------
def build_loss(seen_printed_nos: Iterable[int], items: list[dict],
               dropped: list[dict] | None = None,
               number_map: list[list[int]] | None = None) -> dict:
    """meta.loss：卷面见过的题号 vs 实际保留的条目，差额必须逐条有原因。

    seen_printed_nos 必须是**扫描原始文本层**得到的集合，不能从 items 反推——
    否则「丢了哪些」这个问题本身就依赖丢失的结果，审计失去意义。
    """
    seen = sorted({int(n) for n in seen_printed_nos})
    kept_pns = sorted({int(it["printed_no"]) for it in items if it.get("printed_no") is not None})
    kept_set = set(kept_pns)
    dropped_list = [{"printed_no": int(d["printed_no"]), "reason": str(d["reason"])}
                    for d in (dropped or []) if d.get("printed_no") is not None]
    dropped_pns = {d["printed_no"] for d in dropped_list}
    never_parsed = [n for n in seen if n not in kept_set and n not in dropped_pns]
    mapping = [[int(a), int(b)] for a, b in (number_map or
              [[it["printed_no"], it["no"]] for it in items if it.get("printed_no") is not None])]
    return {
        "seen_printed_nos": len(seen),
        "seen_list": seen,
        "kept": len(items),
        "kept_list": kept_pns,
        "dropped": dropped_list,
        "never_parsed": never_parsed,
        "number_map": mapping,
    }


def loss_is_clean(loss: dict) -> bool:
    """账目平：见过的每个卷面号都留下了条目或被显式 drop 并给了原因。"""
    return not loss.get("dropped") and not loss.get("never_parsed")


# ---------------------------------------------------------------------------
# 条目完整性（defect 1 的修复点）
# ---------------------------------------------------------------------------
def complete_item(item: dict, *, allowed: str = "ABCD", min_options: int = MIN_OPTIONS_DEFAULT,
                  expects_options: bool = True, answer_kind: str | None = None,
                  reason_override: str | None = None,
                  domain_reason: str | None = None) -> tuple[dict, list[dict]]:
    """给条目补 printed_no/no/id/seq_note/kind/material/passage_id，并判定可作答性。

    关键：**任何情况下都不丢弃条目**。信息不全只会让 answerable=False 并附原因，
    同时产出一条 Validation 记录。旧实现把 len(options)>=3 当成 append 的前置条件，
    结果缺损题直接消失，answerable 恒为真、notice 成为死代码。

    domain_reason 与 reason_override 是两回事，别混用：
      reason_override  = 解析缺损（这一题没能抄全）→ 计入 meta.loss.dropped
      domain_reason    = 本卷性质决定不可作答（无听力原文 / JEV 不产文本）
                         → 只写 notice，不是缺损，不占损失账目；结构性发现仍以
                           Validation 记录的形式留在 validations 里
    """
    it = dict(item)
    pn = it.get("printed_no", it.get("no"))
    it["printed_no"] = int(pn) if pn is not None else None
    validations: list[dict] = []
    opts = it.get("options") or {}
    n_opt = len(opts)
    it["options"] = opts

    # 判断题天然只有 正确/错误 两项：选项数下限与字母表都按 kind 收窄，
    # 否则两选项的判断题会被「少于 3」「缺 C/D」双重误杀（抄题链路首跑真抓到的）。
    kind = answer_kind or it.get("kind")
    if min_options == MIN_OPTIONS_DEFAULT and kind == "judge":
        min_options = 2
    if kind == "judge" and allowed == "ABCD":
        allowed = "AB"

    reason = reason_override
    if expects_options:
        if n_opt == 0:
            reason = reason or "选项未被文本层抽出（原件中很可能是图片），题面缺损，不作答"
            validations.append(validation(
                "options_are_images" if it.get("options_as_image") else "options_incomplete",
                severity="error", item_id=it.get("id"), printed_no=it["printed_no"],
                detail=reason, expected=min_options, actual=n_opt))
        elif n_opt < min_options:
            code = "options_merged_in_stem" if it.get("options_as_image") else "options_incomplete"
            reason = reason or f"选项只解析到 {n_opt} 个（少于 {min_options}），题面可能有缺损，不作答"
            validations.append(validation(
                code, severity="error", item_id=it.get("id"), printed_no=it["printed_no"],
                detail=reason, expected=min_options, actual=n_opt))
        elif missing_letters(opts.keys(), allowed):
            miss = missing_letters(opts.keys(), allowed)
            reason = reason or f"选项字母不齐，缺 {'/'.join(miss)}，不作答"
            validations.append(validation(
                "options_incomplete", severity="warn", item_id=it.get("id"),
                printed_no=it["printed_no"], detail=reason,
                ids=[ord(c) - 64 for c in miss], expected=list(allowed), actual=sorted(opts)))

    stem = (it.get("stem") or "").strip()
    if not stem:
        reason = reason or "题干为空，题面缺损，不作答"
        validations.append(validation("stem_suspicious", severity="error",
                                      item_id=it.get("id"), printed_no=it["printed_no"],
                                      detail=reason))
    elif len(stem) < 8 and has_cjk(stem):
        validations.append(validation("stem_suspicious", severity="warn",
                                      item_id=it.get("id"), printed_no=it["printed_no"],
                                      detail=f"题干仅 {len(stem)} 字，可能被截断：{stem}"))

    it["unparseable_reason"] = reason or None
    it["answerable"] = reason is None and (not expects_options or n_opt >= min_options)
    it["notice"] = "" if it["answerable"] else (reason or "")
    if domain_reason:
        it["answerable"] = False
        it["notice"] = domain_reason
        it["unparseable_reason"] = None
    if answer_kind:
        it["kind"] = it.get("kind") or answer_kind
    it.setdefault("material", "")
    it.setdefault("passage_id", None)
    return it, validations


def renumber(items: list[dict], *, id_prefix: str,
             notes: dict[int, str] | None = None) -> tuple[list[dict], list[dict]]:
    """卷内连续重编号：printed_no 保留卷面号，no/id 用连续序号，错位时写 seq_note。

    这段逻辑曾在 parse_xingce_pdf.py:149-153 与 parse_bank_pdf.py:229-233 逐字重复。
    返回 (重排后的条目, 因卷面号重复而产生的 Validation 列表)。
    """
    out: list[dict] = []
    validations: list[dict] = []
    dup_seen: dict[int, int] = {}
    dup_ids: set[int] = set()
    for k, item in enumerate(items, 1):
        it = dict(item)
        pn = it.get("printed_no")
        if pn is None:
            pn = it.get("no")
        it["printed_no"] = int(pn) if pn is not None else None
        it["no"] = k
        it["id"] = f"{id_prefix}{k}"
        note = ""
        if it["printed_no"] is not None:
            dup_seen[it["printed_no"]] = dup_seen.get(it["printed_no"], 0) + 1
            if dup_seen[it["printed_no"]] > 1:
                dup_ids.add(it["printed_no"])
                note = f"卷面编号 {it['printed_no']} 在第 {dup_seen[it['printed_no']]} 次出现"
            elif it["printed_no"] != k:
                note = f"卷面编号 {it['printed_no']}"
            extra = (notes or {}).get(it["printed_no"])
            if extra:
                note = (note + "；" if note else "") + extra
        it["seq_note"] = note
        it["id_prefix"] = id_prefix
        out.append(it)
    if dup_ids:
        ordered = sorted(dup_ids)
        validations.append(validation(
            "duplicate_printed_no", severity="warn", ids=ordered,
            detail="同一卷面号多次出现（多套题拼卷或跨页重绘），已按出现次序分别保留"))
    return out, validations


def attach_sections(items: list[dict], *, id_prefix: str = "S") -> tuple[list[dict], list[dict]]:
    """按 (part, section_name) 归桶并回填 section_id。曾有两份逐字重复的实现。"""
    secs: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    for it in items:
        key = (it.get("part") or "", it.get("section_name") or "")
        hit = index.get(key)
        if hit is None:
            hit = {"id": f"{id_prefix}{len(secs) + 1}", "part": key[0],
                   "name": key[1] or key[0] or "未分节",
                   "no_from": it["no"], "no_to": it["no"], "count": 0}
            secs.append(hit)
            index[key] = hit
        hit["no_to"] = it["no"]
        hit["count"] += 1
        it["section_id"] = hit["id"]
        it["section_name"] = hit["name"]
    return secs, items


# ---------------------------------------------------------------------------
# 信封组装与读写
# ---------------------------------------------------------------------------
def audit_validations(items: list[dict], validations: list[dict]) -> list[dict]:
    """校验记录的身份必须与它指向的条目一致。

    为什么在这里查而不是写在解析脚本里：`printed_no` 是**卷面号**、`no` 是内部序号，
    只有 renumber() 之后两者才各就各位。如果哪个解析器（尤其是以后 LLM 生成的那些）
    把 complete_item 放在 renumber 前面，它盖进记录的就是序号，于是体检报告会指着
    一道不存在的题号说话——而这份报告正是这个项目对外承诺"可复核"的唯一凭据。
    顺序是约定，约定会漂；这里把它变成数据层面的硬检查。
    """
    by_id = {it.get("id"): it for it in items if it.get("id") is not None}
    real_pn = {it.get("printed_no") for it in items if it.get("printed_no") is not None}
    out: list[dict] = []
    seen: set[tuple[str, str, int | None]] = set()
    for v in validations:
        iid = v.get("item_id")
        pn = v.get("printed_no")
        if iid is None:
            # 只有卷面号的记录（已知缺陷台账就是在 renumber 之前写的）同样得落地：
            # 否则报告里会出现一句指着不存在的题号说的话，而报告是本项目的对外凭据。
            if pn is not None and pn not in real_pn:
                key = ("orphan_printed_no", "", int(pn))
                if key in seen:
                    continue
                seen.add(key)
                out.append(validation("identity_mismatch", severity="warn",
                                      printed_no=pn,
                                      detail=(f"记录自称卷面 {pn} 号，但没有任何条目的 printed_no 是它——"
                                              f"报告里会出现一句指着不存在的题号说的话"
                                              f"（若这是针对已被丢弃题目的台账登记则属正常）")))
            continue
        it = by_id.get(iid)
        if it is None:
            key = ("missing_item", str(iid), None)
            if key in seen:
                continue
            seen.add(key)
            out.append(validation("identity_mismatch", severity="error", item_id=iid,
                                  detail=f"{iid} 在 items 里不存在"))
            continue
        if pn is not None and pn != it.get("printed_no"):
            key = ("printed_no", str(iid), int(pn))
            if key in seen:
                continue
            seen.add(key)
            out.append(validation("identity_mismatch", severity="error", item_id=iid,
                                  printed_no=pn, detail=f"{iid}（实际卷面 {it.get('printed_no')}）"))
    return out


def build_bank(*, subject: str, source: str, items: list[dict], sections: list[dict],
               passages: list[dict] | None = None, loss: dict,
               validations: Iterable[dict] | None = None, meta: dict | None = None,
               generated_by: str = "", method: str = "") -> dict:
    """唯一的题库信封构造器。validations 传结构化记录，中文报告在这里渲染。

    meta.verified 只能由外部核验流程显式置位，且 loss 不平会被强制取消。
    """
    vs = [dict(v) for v in (validations or [])]
    vs += audit_validations(items, vs)
    meta = dict(meta or {})
    if loss.get("never_parsed"):
        ordered = loss["never_parsed"]
        vs.append(validation("never_parsed", severity="error", ids=ordered,
                             detail="这些卷面题号在文本层出现过，但没有产出任何条目"))
    # 自报丢弃同样必须留在记录里：账目平了不等于「丢弃」没有发生。放在 audit 之后——
    # 被丢弃的题号本来就不在 items 里，不该再给它配发一条 identity_mismatch。
    for d in loss.get("dropped") or []:
        vs.append(validation("draft_dropped", severity="warn",
                             printed_no=int(d["printed_no"]),
                             detail=str(d.get("reason") or "草稿自报未收录")))
    for it in items:
        if it.get("unparseable_reason") and not any(
                v["item_id"] == it.get("id") and v["severity"] == "error" for v in vs):
            vs.append(validation("stem_suspicious", severity="error", item_id=it.get("id"),
                                 printed_no=it.get("printed_no"),
                                 detail=it["unparseable_reason"]))
    bank = {
        "meta": {
            "bank_version": BANK_VERSION,
            "subject": subject,
            "source": source,
            "title": meta.pop("title", source),
            "total_items": len(items),
            "answered": sum(1 for i in items if i.get("standard_answer")),
            "answerable": sum(1 for i in items if i.get("answerable")),
            "generated_by": generated_by,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method": method,
            "loss": loss,
            **meta,
        },
        "sections": sections,
        "passages": passages or [],
        "items": items,
        "validations": vs,
    }
    bank["report"] = render_bank_report_lines(bank)
    apply_verified(bank)
    return bank


def apply_verified(bank: dict) -> dict:
    """重算 meta.verified：任何 error 级记录、或 loss.dropped/never_parsed 非空 → 取消。"""
    m = bank["meta"]
    blockers = errors_of(bank["validations"])
    reasons = []
    if not loss_is_clean(m.get("loss") or {}):
        reasons.append("meta.loss 存在未入账的题目损失")
    if blockers:
        reasons.append(f"{len(blockers)} 条 error 级校验记录")
    if not m.get("verification"):
        reasons.append("尚未经过独立核验（extract/verify.py）")
    m["verified"] = not reasons
    m["verified_blockers"] = reasons
    return bank


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def sha256_obj(obj: Any) -> str:
    return hashlib.sha256(dump_json(obj).encode("utf-8")).hexdigest()


def read_bank(path: str | Path, *, allow_v1: bool = False) -> dict:
    """读取题库。v1（或无版本号）默认拒绝，不做字段猜测式兼容。"""
    bank = json.loads(Path(path).read_text(encoding="utf-8"))
    version = bank.get("meta", {}).get("bank_version")
    if version is None:
        version = 1
    if version != BANK_VERSION and not allow_v1:
        raise BankVersionError(
            f"{path} 的 bank_version={version}，当前代码只支持 v{BANK_VERSION}。"
            f"请用对应解析脚本重新生成题库（不要手工改版本号）。")
    return bank


def require_v2(bank: dict, *, where: str = "") -> dict:
    version = bank.get("meta", {}).get("bank_version")
    if version != BANK_VERSION:
        raise BankVersionError(
            f"{where or '题库'} 的 bank_version={version}，需要 v{BANK_VERSION}；"
            f"旧版题库的 validations 是无结构字符串、且缺损题已被静默删除，不可复用。")
    return bank


# ---------------------------------------------------------------------------
# 体检报告（唯一一份打印机）
# ---------------------------------------------------------------------------
def section_state(section: dict, items: list[dict]) -> tuple[int, str]:
    """共享的一行：期望题数与状态（OK / 缺 N）。曾重复于 parse_pdf.py:397 与 parse_math_pdf.py:338。"""
    exp = int(section.get("no_to", 0)) - int(section.get("no_from", 0)) + 1
    if section.get("expected_count") is not None:
        exp = int(section["expected_count"])
    cnt = int(section.get("count", 0))
    return exp, ("OK" if cnt == exp else f"缺 {exp - cnt}")


WIDTH = 78


def render_bank_report_lines(bank: dict) -> list[str]:
    """把题库渲染成体检报告行。**所有行都由结构化字段驱动**，不接受自由文本。"""
    m = bank["meta"]
    items = bank["items"]
    lines: list[str] = ["=" * WIDTH, f"{m.get('subject', '')}试卷解析体检报告", "=" * WIDTH]
    lines.append(f"bank_version=v{m.get('bank_version')}   来源: {m.get('source')}"
                 f"   页数: {m.get('pages', '-')}   条目 {m.get('total_items')} 道")
    if m.get("method"):
        lines.append(f"方法: {m['method']}")
    if m.get("paper_note"):
        lines.append(f"原件说明: {m['paper_note']}")
    lines.append("")

    if bank.get("sections"):
        lines.append(f"{'小节':<22}{'题号':<10}{'应有':<6}{'已解':<6}{'选项':<8}{'状态'}")
        lines.append("-" * WIDTH)
        for s in bank["sections"]:
            exp, state = section_state(s, items)
            opts = {len(i.get("options") or {}) for i in items if i.get("section_id") == s["id"]}
            opt_s = "/".join(str(o) for o in sorted(opts)) if opts else "-"
            rng = f"{s.get('no_from')}-{s.get('no_to')}"
            name = (s.get("name") or s.get("part") or "")[:20]
            lines.append(f"{name:<22}{rng:<10}{exp:<6}{s.get('count', 0):<6}{opt_s:<8}{state}")
        lines.append("")

    loss = m.get("loss") or {}
    lines.append("损失账目（卷面题号 → 条目）：")
    lines.append(f"  卷面出现过的题号 {loss.get('seen_printed_nos', 0)} 个   "
                 f"保留条目 {loss.get('kept', 0)} 道   "
                 f"降级保留 {len(loss.get('dropped') or [])} 道   "
                 f"未产出任何条目 {len(loss.get('never_parsed') or [])} 道")
    for d in loss.get("dropped") or []:
        lines.append(f"  · 卷面第 {d['printed_no']} 题 已保留但降级为不可作答：{d['reason']}")
    if loss.get("never_parsed"):
        lines.append(f"  ✗ 下列卷面题号完全没有条目（解析器缺陷，必须修）："
                     f"{loss['never_parsed']}")
    moved = [p for p, s in (loss.get("number_map") or []) if p != s]
    if moved:
        lines.append(f"  卷面号与内部序号错位 {len(moved)} 处（多套题拼卷或题号重复），"
                     f"详见 items[].seq_note")
    lines.append("")

    unans = [i for i in items if not i.get("answerable")]
    lines.append(f"可自动作答 {len(items) - len(unans)} 题 / 共 {len(items)} 题；不可作答 {len(unans)} 题")
    kinds: dict[str, int] = {}
    for i in items:
        kinds[i.get("kind") or "?"] = kinds.get(i.get("kind") or "?", 0) + 1
    lines.append(f"题型分布 {kinds}")
    if unans:
        lines.append("不可作答题目（逐条给原因，不合并、不省略）：")
        for i in unans:
            lines.append(f"  - {i.get('id')}（卷面 {i.get('printed_no')}）：{i.get('notice') or i.get('unparseable_reason')}")
    lines.append("")

    vs = bank["validations"]
    if vs:
        lines.append("校验记录（结构化，报告行由此渲染）：")
        for v in vs:
            lines.append(f"  [{SEVERITY_LABEL[v['severity']]}] {render_validation(v)}")
    else:
        lines.append("校验记录：无")
    lines.append("")

    defects = m.get("known_text_layer_defects") or []
    if defects:
        lines.append("已核实的原件缺陷（非转写问题，逐条列出）：")
        for d in defects:
            lines.append(f"  - {d}")
        lines.append("")

    verdict = "已核验" if m.get("verified") else "未核验"
    lines.append(f"结论：{verdict}")
    if not m.get("verified"):
        for r in m.get("verified_blockers") or []:
            lines.append(f"  阻碍项：{r}")
    return lines


def print_bank_report(bank: dict) -> None:
    for line in bank.get("report") or render_bank_report_lines(bank):
        print(line)


def write_bank(bank: dict, path: str | Path, *, mkdir: bool = True) -> Path:
    p = Path(path)
    if mkdir:
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_json(bank), encoding="utf-8")
    return p
