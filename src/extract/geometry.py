"""PDF 几何与墨迹证据：给抄题端点（渲染页面图）和验收器（公式存在性检查）共用。

为什么单独一个模块
------------------
「这一道小题在原文里到底占了哪些地方」是抽取层唯一真正的物证来源：
  · 文本层字符包围盒 —— 能读出来的部分
  · 矢量绘图指令     —— MathType 的根号、分数线、括号
  · 版面图像块       —— 图形推理的选项、统计图表
只有把这三类证据摆在一起，才能判断「转写里的 $...$ 是不是真的对应一团非文本层墨迹」，
也才能解释旧核验器为什么看不见 √7 被抽成 7。
"""
from __future__ import annotations

import base64
from typing import Any

import fitz


def rect_intersection(a: list[float], b: list[float]) -> float:
    x = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return x * y


def rect_area(r: list[float]) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def rect_union(rects: list[list[float]]) -> list[float] | None:
    if not rects:
        return None
    return [min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects)]


def expand_rect(r: list[float], pad: float, clip: list[float] | None = None) -> list[float]:
    x0, y0, x1, y1 = r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad
    if clip:
        return [max(clip[0], x0), max(clip[1], y0), min(clip[2], x1), min(clip[3], y1)]
    return [x0, y0, x1, y1]


# ---------------------------------------------------------------------------
# 三类物证
# ---------------------------------------------------------------------------
def text_char_rects(page: "fitz.Page", clip: list[float] | None = None) -> list[list[float]]:
    """页面上每个可见字形字符的包围盒（可按区域裁剪）。这是「文本层能读出的部分」。"""
    out: list[list[float]] = []
    d = page.get_text("rawdict", clip=clip) if clip else page.get_text("rawdict")
    for blk in d["blocks"]:
        if blk.get("type") == 1 or "lines" not in blk:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    bb = [float(v) for v in ch["bbox"]]
                    if bb[2] - bb[0] > 0.05 and bb[3] - bb[1] > 0.05:
                        out.append(bb)
    return out


def formula_glyph_rects(page: "fitz.Page", clip: list[float] | None = None) -> list[list[float]]:
    """私用区（U+E000–U+F8FF）字形包围盒。

    MathType 把「文本层读不出来但确实印在纸上」的结构符——大括号、根号横钩、分数线——
    编码成符号字体（本机这份卷是 PMExtra,Bold）的私用区码位。它们既不是可读文本，
    也不是矢量绘图指令，却是公式存在的直接物证。旧核验器把所有非汉字字符一律剥掉，
    于是这批证据被主动扔掉，`(1+5i)i` 少对括号、`√7` 少根号都无从发现。
    """
    out: list[list[float]] = []
    d = page.get_text("rawdict", clip=clip) if clip else page.get_text("rawdict")
    for blk in d["blocks"]:
        if blk.get("type") == 1 or "lines" not in blk:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    c = ch.get("c") or ""
                    if len(c) == 1 and 0xE000 <= ord(c) <= 0xF8FF:
                        bb = [float(v) for v in ch["bbox"]]
                        if bb[2] - bb[0] > 0.05 and bb[3] - bb[1] > 0.05:
                            out.append(bb)
    return out


def vector_drawings(page: "fitz.Page", clip: list[float] | None = None) -> list[dict]:
    """矢量绘图指令。MathType 的根号/分数线/括号就是若干条线段或填充小矩形。

    整页背景矩形（面积占页面 60% 以上的单矩形）会被剔掉——它是刷白底，不是内容。
    """
    pr = [float(v) for v in page.rect]
    out: list[dict] = []
    for dr in page.get_drawings():
        rr = [float(v) for v in dr["rect"]]
        area = rect_area(rr)
        if area <= 0:
            continue
        if clip and rect_intersection(rr, clip) <= 0:
            continue
        items = dr.get("items") or []
        kinds = {it[0] for it in items}
        if len(items) == 1 and "re" in kinds and area > 0.6 * rect_area(pr):
            continue                      # 页面底色
        out.append({"rect": rr, "type": dr.get("type"), "n_items": len(items),
                    "kinds": sorted(kinds)})
    return out


def image_blocks(page: "fitz.Page") -> list[list[float]]:
    """版面里的图像块包围盒（图形推理的选项、统计图都长在这里）。"""
    return [[float(v) for v in b["bbox"]]
            for b in page.get_text("dict")["blocks"] if b.get("type") == 1]


# ---------------------------------------------------------------------------
# 公式存在性判定
# ---------------------------------------------------------------------------
MIN_INK_AREA = 1.5      # 平方磅：小于此面积的碎片不足以构成一条结构线
TEXT_COVERED = 0.85     # 被字形覆盖 85% 以上的绘图视为「就是这段文字本身」，不算额外墨迹


def unexplained_ink(page: "fitz.Page", rect: list[float]) -> list[list[float]]:
    """区域内**无法用文本层解释**的墨迹包围盒列表。

    判据：矢量绘图片段若与任一字形包围盒重叠 85% 以上，视为「就是那段字的载体」而剔除；
    剩下的绘图 + 私用区结构字形 + 版面图像块，就是「文本层读不出、但确实印在纸上」的东西，
    正是 MathType 结构符（根号横钩、分数线、大括号）的特征。

    一个必须知道的细节：`text_char_rects` 覆盖所有字形，**包含私用区码位**（它们不可读但
    是字形）。所以一条正好压在私用区字形上的矢量线会被判为"已解释"而剔除；同一批私用区
    字形随后由 formula_glyph_rects 直接计入结果，总账不受影响——但别把这里读成
    "只有可读文本才会参与覆盖判定"。
    """
    chars = text_char_rects(page, rect)
    out: list[list[float]] = []
    for d in vector_drawings(page, rect):
        rr = d["rect"]
        a = rect_area(rr)
        if a <= MIN_INK_AREA:
            continue
        covered = sum(min(a, rect_intersection(rr, c)) for c in chars)
        if covered / a >= TEXT_COVERED:
            continue
        out.append(rr)
    out.extend(formula_glyph_rects(page, rect))
    for r in image_blocks(page):
        if rect_intersection(r, rect) > 0:
            out.append([float(v) for v in r])
    return out


def formula_support_ratio(page: "fitz.Page", anchor_rects: list[list[float]],
                          region: list[float], pad_v: float = 13.0,
                          pad_h: float = 26.0) -> tuple[float, list[float] | None, list[float]]:
    """锚点邻域内「非文本层墨迹」面积 ÷ 锚点自身面积。

    锚点是转写中一段公式对应的文本层字形包围盒（例如 √7 在文本层只剩 '7'）。
    MathType 的结构线**不长在字形包围盒里面**：根号的横钩在数字上方、分数线穿过
    上下两行之间、大括号是一条竖线贴着分子分母左侧。因此必须在锚点四周的带状区域里找，
    只量锚点自身会把所有真公式都判成 0——旧核验器正是因为压根不看墨迹才漏掉了它们。

    返回 (比值, 最大墨迹块, 搜索窗口)。没有锚点时比值记为 -1.0，表示无从判断，
    调用方必须记为未核验而不是放过。
    """
    if not anchor_rects:
        return -1.0, None, list(region)
    anchor = rect_union(anchor_rects) or list(region)
    h = max(1e-6, anchor[3] - anchor[1])
    w = max(1e-6, anchor[2] - anchor[0])
    # 带高至少覆盖一行字高，否则单个小操作数（如 '$9$'）的窗口会退化成一条缝
    band = expand_rect(anchor, 0.0)
    band[1] -= max(pad_v, h * 0.9)
    band[3] += max(pad_v, h * 0.9)
    band[0] -= min(pad_h, w * 1.5)
    band[2] += min(pad_h, w * 1.5)
    band = expand_rect(band, 0.0, clip=region)
    ink = unexplained_ink(page, band)
    if not ink:
        return 0.0, None, band
    total = sum(rect_area(r) for r in ink)
    biggest = max(ink, key=rect_area)
    return total / (h * w), biggest, band


def glyph_rects_for_text(page: "fitz.Page", needle: str, region: list[float]) -> list[list[float]]:
    """在区域内找出连续文本 needle 对应的字形包围盒（按 rawdict 字符流拼接匹配）。

    needle 为空返回 []。找不到也返回 []——由调用方决定是告警还是降级为宽松检查。
    """
    needle = "".join(needle.split())
    if not needle:
        return []
    d = page.get_text("rawdict", clip=region)
    seq: list[tuple[str, list[float]]] = []
    for blk in d["blocks"]:
        if blk.get("type") == 1 or "lines" not in blk:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    t = ch.get("c") or ""
                    if t.strip():
                        seq.append((t, [float(v) for v in ch["bbox"]]))
    joined = "".join(t for t, _ in seq)
    idx = joined.find(needle)
    if idx < 0:
        return []
    return [seq[i][1] for i in range(idx, idx + len(needle))]


# ---------------------------------------------------------------------------
# probe 锚点定位：protocol.md 承诺的机制就在这里兑现
# ---------------------------------------------------------------------------
def page_char_stream(page: "fitz.Page") -> tuple[str, list[list[float]]]:
    """整页文本层字符流（丢弃空白与私用区）+ 逐字包围盒，两者逐位对齐。

    丢弃规则与 verify._token_stream 一致：空白和 MathType 私用区码位不算字符。
    否则「已知向量 a 且」这种被公式打断的句子，在这两处会被切成两个不同的字符串，
    一边定位得到、一边比对看不到——同一份卷的两套真相。
    """
    chars: list[str] = []
    boxes: list[list[float]] = []
    d = page.get_text("rawdict")
    for blk in d["blocks"]:
        if blk.get("type") == 1 or "lines" not in blk:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    t = ch.get("c") or ""
                    if not t.strip():
                        continue
                    o = ord(t)
                    if 0xE000 <= o <= 0xF8FF:
                        continue
                    chars.append(t)
                    boxes.append([float(v) for v in ch["bbox"]])
    return "".join(chars), boxes


def locate_needle(stream: str, needle: str) -> int:
    """在字符流里找连续片段，返回起始下标；找不到返回 -1。空白一律不算。"""
    needle = "".join(needle.split())
    if not needle:
        return -1
    return stream.find(needle)


def same_column(a: list[float], b: list[float]) -> bool:
    """两个包围盒是否同列：水平重叠超过较窄者的半数。"""
    overlap = min(a[2], b[2]) - max(a[0], b[0])
    narrower = min(a[2] - a[0], b[2] - b[0])
    return narrower > 0 and overlap >= narrower * 0.5


LINE_GAP = 24.0   # 同行文字生长允许的最大空隙（磅）：须大于行内词距、小于栏间沟


def grow_line_box(chars: list[list[float]], box: list[float]) -> list[float]:
    """把锚点盒沿它所在的文本行生长到连续文字的尽头（只横向生长，纵向不动）。

    锚点往往只是一句话的头几个字，直接拿它当「列」的判据，同一句的后续文字
    （在锚点右侧、与锚点水平不重叠）会被判成邻列切出区域外——于是漏抄比对把
    自家题干的后半句和右侧选项全部报成「疑似编造」。生长规则：与锚点同一行
    （纵向有交集）且与当前盒横向空隙 ≤ LINE_GAP 的字逐步并入，直到不再扩大；
    栏间沟远宽于行内词距，生长到沟边自然停住。
    """
    x0, x1 = box[0], box[2]
    grew = True
    while grew:
        grew = False
        for c in chars:
            if c[3] <= box[1] + 0.5 or c[1] >= box[3] - 0.5:
                continue                        # 与锚点行无纵向交集：别行的字
            if c[2] < x0 and x0 - c[2] <= LINE_GAP:
                x0 = c[0]
                grew = True
            elif c[0] > x1 and c[0] - x1 <= LINE_GAP:
                x1 = c[2]
                grew = True
    return [x0, box[1], x1, box[3]]


def slice_page_rects(page: "fitz.Page", anchors: list[tuple[Any, list[float]]],
                     *, pad: float = 4.0) -> tuple[dict[Any, list[float]], list[Any]]:
    """按锚点把一页纵向切成一道道题的区域。

    anchors = [(键, 锚点包围盒)]，键由调用方决定（一般是 printed_no）。
    每个键的区域 = 从它的锚点顶边（含上一行）到**同列**下一个锚点的顶边。

    为什么要 `same_column` 这一层：双栏卷子上「下一个锚点」如果在另一栏，
    纵向直接切就会把整栏连同隔壁栏一起框进来，于是漏抄比对会把邻栏的正文
    全报成"本题漏抄"，而编造比对又因为框大而变得宽松——两头同时失真。

    返回 ({键: rect}, 定位失败的键列表)。锚点缺 y 序或同列同高的重复锚点，
    只保留第一个并把它列进失败名单——宁可让调用方知道"这题没切开"。
    """
    pr = [float(v) for v in page.rect]
    kept: list[tuple[Any, list[float]]] = []
    failed: list[Any] = []
    for key, box in sorted(anchors, key=lambda kb: (kb[1][1], kb[1][0])):
        if any(abs(box[1] - b[1]) < 1.0 and same_column(box, b) for _, b in kept):
            failed.append(key)
            continue
        kept.append((key, box))

    all_chars = text_char_rects(page)
    rects: dict[Any, list[float]] = {}
    for i, (key, box) in enumerate(kept):
        y0 = max(pr[1], box[1] - pad)
        y1 = pr[3]
        for _, b2 in kept[i + 1:]:
            if same_column(box, b2) and b2[1] > box[1] + 1.0:
                y1 = b2[1] - 1.0
                break
        # 横向不收就变成"整页宽 + 本题高度带"，双栏卷子上会把邻栏正文一起框进来：
        # 于是漏抄比对把邻栏的字全报成本题缺，而编造比对又因框大而变松——两头同时失真。
        # 列判据用「生长到整行」的锚点而不是锚点本身：锚点常只有一句的头几个字，
        # 同一行右侧的后续文字若按锚点判列会被切出区域外，自家题干反被报成编造。
        # 在锚点行生长之外还要沿纵向跨度继续生长：题干行可以比选项行窄，
        # 若只拿题干行宽当边界，行尾选项会被切出区域，模型照抄也被报成"发明选项字母"。
        # 生长规则与 grow_line_box 一致（空隙 ≤ LINE_GAP 即并入，逐轮扩张到稳定）：
        # 能越过词距与选项间距；栏间沟远宽于 LINE_GAP，两侧互不并入，跨栏仍被拦住。
        col = grow_line_box(all_chars, box)
        span_chars = [c for c in all_chars
                      if y0 <= (c[1] + c[3]) / 2 <= y1]
        while True:
            x0, x1 = col[0], col[2]
            grown = False
            for c in span_chars:
                if c[2] < x0 and x0 - c[2] <= LINE_GAP:
                    x0 = c[0]
                    grown = True
                elif c[0] > x1 and c[0] - x1 <= LINE_GAP:
                    x1 = c[2]
                    grown = True
            if not grown:
                break
            col = [x0, col[1], x1, col[3]]
        band = [c for c in span_chars if same_column(col, c)]
        u = rect_union(band or [box])
        rects[key] = [max(pr[0], u[0] - pad), y0, min(pr[2], u[2] + pad), y1]
    return rects, failed


# ---------------------------------------------------------------------------
# 渲染（抄题端点用；验收器不调用任何网络，只用几何）
# ---------------------------------------------------------------------------
def render_page_base64(doc: "fitz.Page", page_no: int, dpi: int) -> tuple[str, int, int]:
    """渲染整页为 PNG，返回 (base64, 宽, 高)。"""
    pix = doc[page_no].get_pixmap(dpi=dpi)
    return (base64.b64encode(pix.tobytes("png")).decode("ascii"), pix.width, pix.height)


def open_pdf(path: str) -> "fitz.Document":
    doc = fitz.open(path)
    if doc.needs_pass:
        raise RuntimeError(f"PDF 需要密码，无法解析：{path}")
    return doc
