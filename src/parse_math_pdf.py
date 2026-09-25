"""数学试卷解析：把 2025 高考数学（新Ⅰ卷）PDF 解析成 v2 题库。

为什么不能只用文本提取
----------------------
该卷用 MathType 排版，公式被拆成大量独立字形片段，且**分数线、根号、括号等结构信息不在文本层里**。
纯文本提取的结果是错的，例如第 1 题：
    文本层得到 "1+5i i 的虚部为"
    真实题目是 (1+5i)i 的虚部为     —— 括号丢失
第 3 题的 √7 会被提取成 "7"（根号丢失），第 4 题的 π/3 会变成 "π 3"（分数线丢失）。

本脚本实际做到的事（以及做不到的事）
------------------------------------
1. 结构通道：PyMuPDF 文本层 —— 中文与题号可靠（公式不可靠）
2. 公式通道：**逐题人工**把 LaTeX 写进下面的 TRANSCRIPTION（19 条字面量），
   写的时候对着按题号裁剪、200 DPI 渲染的片段图核对。
   注意：这不是"整页 220 DPI 转写"，公式的正确性**依赖人工核对**，机器不复核。
3. 自动校验：只做一个方向的、只比中文的检查——把转写里的中文词与整卷文本层的中文比对，
   报"转写里有、文本层里没有"的片段。**它查不出公式写错**（公式在比对前就被剥掉），
   **也查不出抄题人编造中文**（只有中文进文本层就通过）。
   因此本题库的 meta.verified 由 extract/verify.py 的独立验收盖章，不由本脚本自证。
4. 保真：按题号坐标裁剪每题原图（dpi=200），界面可直接展示原始排版，避免二次排版失真。

信封、编号、完整性判定、损失账目与体检报告一律走 bank.py。

用法: python src/parse_math_pdf.py [试卷.pdf] [--out data/exam_math.json]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bank  # noqa: E402

PDF = ROOT / "2025年普通高等学校招生全国统一考试 数字.pdf"
OUT = ROOT / "data" / "exam_math.json"
FIGDIR = ROOT / "data" / "math_items"

# ---------------------------------------------------------------------------
# 转写内容：**人工**逐题对着 crop_items 裁出的片段图（200 DPI）写进来。$...$ 内为 LaTeX。
# stem/options 使用 LaTeX；zh 字段供"转写里的中文能否在文本层找到"这一单向检查用。
# 公式写错了这里查不出来——只能靠原图与下面的 known_text_layer_defects 台账。
# ---------------------------------------------------------------------------
TRANSCRIPTION: list[dict] = [
    # ---- 一、选择题（单选，每题 5 分）----
    dict(no=1, sec="S1", type="single", score=5,
         stem=r"$(1+5i)i$ 的虚部为",
         opts={"A": r"$-1$", "B": r"$0$", "C": r"$1$", "D": r"$6$"},
         zh=["的虚部为"]),
    dict(no=2, sec="S1", type="single", score=5,
         stem=r"设全集 $U=\{x\mid x$ 是小于 $9$ 的正整数$\}$，集合 $A=\{1,3,5\}$，则 $\complement_U A$ 中元素个数为",
         opts={"A": r"$2$", "B": r"$3$", "C": r"$5$", "D": r"$8$"},
         zh=["设全集", "是小于", "的正整数", "集合", "则", "中元素个数为"]),
    dict(no=3, sec="S1", type="single", score=5,
         stem=r"若双曲线 $C$ 的虚轴长为实轴长的 $\sqrt{7}$ 倍，则 $C$ 的离心率为",
         opts={"A": r"$\sqrt{2}$", "B": r"$2$", "C": r"$\sqrt{7}$", "D": r"$2\sqrt{2}$"},
         zh=["若双曲线", "的虚轴长为实轴长的", "倍，则", "的离心率为"]),
    dict(no=4, sec="S1", type="single", score=5,
         stem=r"若点 $(a,0)(a>0)$ 是函数 $y=2\tan\left(x-\dfrac{\pi}{3}\right)$ 的图象的一个对称中心，则 $a$ 的最小值为",
         opts={"A": r"$\dfrac{\pi}{4}$", "B": r"$\dfrac{\pi}{2}$", "C": r"$\dfrac{\pi}{3}$", "D": r"$\dfrac{4\pi}{3}$"},
         zh=["若点", "是函数", "的图象的一个对称中心，则", "的最小值为"]),
    dict(no=5, sec="S1", type="single", score=5,
         stem=r"设 $f(x)$ 是定义在 $\mathbf{R}$ 上且周期为 $2$ 的偶函数，当 $2\leqslant x\leqslant 3$ 时，"
              r"$f(x)=5-2x$，则 $f\left(-\dfrac{3}{4}\right)=$",
         opts={"A": r"$-\dfrac{1}{2}$", "B": r"$-\dfrac{1}{4}$", "C": r"$\dfrac{1}{4}$", "D": r"$\dfrac{1}{2}$"},
         zh=["设", "是定义在", "上且周期为", "的偶函数，当", "时，", "则"]),
    dict(no=6, sec="S1", type="single", score=5,
         stem=r"帆船比赛中，运动员可借助风力计测定风速的大小和方向，测出的结果在航海学中称为视风风速。"
              r"视风风速对应的向量，是真风风速对应的向量与船行风速对应的向量之和，其中船行风速对应的向量与船速对应的向量"
              r"大小相等，方向相反。图 1 给出了部分风力等级、名称与风速大小的对应关系。已知某帆船运动员在某时刻测得的"
              r"视风风速对应的向量与船速对应的向量如图 2（风速的大小和向量的大小相同，单位 m/s），则真风为",
         opts={"A": "轻风", "B": "微风", "C": "和风", "D": "劲风"},
         zh=["帆船比赛中，运动员可借助风力计测定风速的大小和方向，测出的结果在航海学中称为视风风速。",
             "视风风速对应的向量，是真风风速对应的向量与船行风速对应的向量之和，其中船行风速对应的向量与船速对应的向量",
             "大小相等，方向相反。图", "给出了部分风力等级、名称与风速大小的对应关系。已知某帆船运动员在某时刻测得的",
             "视风风速对应的向量与船速对应的向量如图", "风速的大小和向量的大小相同，单位", "），则真风为"],
         table=dict(header=["等级", "风速大小 m/s", "名称"],
                    rows=[["2", "1.1~3.3", "轻风"], ["3", "3.4~5.4", "微风"],
                          ["4", "5.5~7.9", "和风"], ["5", "8.0~10.1", "劲风"]]),
         figures=["图1 风力等级对照表", "图2 视风风速与船速的向量图（坐标系，含 O、1、2、3 刻度与 y、x 轴）"]),
    dict(no=7, sec="S1", type="single", score=5,
         stem=r"若圆 $x^2+(y+2)^2=r^2\ (r>0)$ 上到直线 $y=\sqrt{3}x+2$ 的距离为 $1$ 的点有且仅有 $2$ 个，"
              r"则 $r$ 的取值范围是",
         opts={"A": r"$(0,1)$", "B": r"$(1,3)$", "C": r"$(3,+\infty)$", "D": r"$(0,+\infty)$"},
         zh=["若圆", "上到直线", "的距离为", "的点有且仅有", "个，则", "的取值范围是"]),
    dict(no=8, sec="S1", type="single", score=5,
         stem=r"若实数 $x,y,z$ 满足 $2+\log_2 x=3+\log_3 y=5+\log_5 z$，则 $x,y,z$ 的大小关系不可能是",
         opts={"A": r"$x>y>z$", "B": r"$x>z>y$", "C": r"$y>x>z$", "D": r"$y>z>x$"},
         zh=["若实数", "满足", "的大小关系不可能是"]),
    # ---- 二、选择题（多选，每题 6 分）----
    dict(no=9, sec="S2", type="multi", score=6,
         stem=r"在正三棱柱 $ABC-A_1B_1C_1$ 中，$D$ 为 $BC$ 中点，则",
         opts={"A": r"$AD\perp A_1C$", "B": r"$BC\perp$ 平面 $AA_1D$",
               "C": r"$CC_1\parallel$ 平面 $AA_1D$", "D": r"$AD\parallel A_1B_1$"},
         zh=["在正三棱柱", "中，", "为", "中点，则"]),
    dict(no=10, sec="S2", type="multi", score=6,
         stem=r"设抛物线 $C:y^2=6x$ 的焦点为 $F$，过 $F$ 的直线交 $C$ 于 $A$、$B$，过 $F$ 且垂直于 $AB$ 的直线交准线 "
              r"$l:x=-\dfrac{3}{2}$ 于 $E$，过点 $A$ 作准线 $l$ 的垂线，垂足为 $D$，则",
         opts={"A": r"$|AD|=|AF|$", "B": r"$|AE|=|AB|$", "C": r"$|AB|\geqslant 6$",
               "D": r"$|AE|\cdot|BE|\geqslant 18$"},
         zh=["设抛物线", "的焦点为", "，过", "的直线交", "于", "，过", "且垂直于", "的直线交准线",
             "于", "，过点", "作准线", "的垂线，垂足为", "，则"]),
    dict(no=11, sec="S2", type="multi", score=6,
         stem=r"已知 $\triangle ABC$ 的面积为 $\dfrac{1}{4}$，若 $\cos 2A+\cos 2B+2\sin C=2$，"
              r"$\cos A\cos B\sin C=\dfrac{1}{4}$，则",
         opts={"A": r"$\sin C=\sin^2 A+\sin^2 B$", "B": r"$AB=\sqrt{2}$",
               "C": r"$\sin A+\sin B=\dfrac{\sqrt{6}}{2}$", "D": r"$AC^2+BC^2=3$"},
         zh=["已知", "的面积为", "，若", "，", "，则"]),
    # ---- 三、填空题（每题 5 分）----
    dict(no=12, sec="S3", type="gapfill", score=5,
         stem=r"若直线 $y=2x+5$ 是曲线 $y=e^x+x+a$ 的切线，则 $a=$ $\underline{\qquad}$",
         opts={}, zh=["若直线", "是曲线", "的切线，则"]),
    dict(no=13, sec="S3", type="gapfill", score=5,
         stem=r"若一个正项等比数列的前 $4$ 项和为 $4$，前 $8$ 项和为 $68$，则该等比数列的公比为 $\underline{\qquad}$",
         opts={}, zh=["若一个正项等比数列的前", "项和为", "，前", "项和为", "，则该等比数列的公比为"]),
    dict(no=14, sec="S3", type="gapfill", score=5,
         stem=r"一个箱子里有 $5$ 个相同的球，分别以 $1\sim 5$ 标号，从中有放回地取三次，记至少取出一次的球的个数 $X$，"
              r"则数学期望 $E(X)=$ $\underline{\qquad}$",
         opts={}, zh=["一个箱子里有", "个相同的球，分别以", "标号，从中有放回地取三次，记至少取出一次的球的个数",
                      "，则数学期望"]),
    # ---- 四、解答题 ----
    dict(no=15, sec="S4", type="solution", score=13,
         stem=r"为研究某疾病与超声波检查结果的关系，从做过超声波检查的人群中随机调查了 $1000$ 人，得到如下列联表：",
         opts={},
         subs=[r"(1) 记超声波检查结果不正常者患该疾病的概率为 $P$，求 $P$ 的估计值；",
               r"(2) 根据小概率值 $\alpha=0.001$ 的独立性检验，分析超声波检查结果是否与患该疾病有关。"],
         appendices=[r"附：$\chi^2=\dfrac{n(ad-bc)^2}{(a+b)(c+d)(a+c)(b+d)}$"],
         table=dict(header=["组别＼超声波检查结果", "正常", "不正常", "合计"],
                    rows=[["患该疾病", "20", "180", "200"],
                          ["未患该疾病", "780", "20", "800"],
                          ["合计", "800", "200", "1000"]]),
         crit_table=dict(header=["$P(\\chi^2\\geqslant k)$", "0.005", "0.010", "0.001"],
                         rows=[["$k$", "3.841", "6.635", "10.828"]]),
         zh=["为研究某疾病与超声波检查结果的关系，从做过超声波检查的人群中随机调查了", "人，得到如下列联表：",
             "记超声波检查结果不正常者患该疾病的概率为", "，求", "的估计值；",
             "根据小概率值", "的独立性检验，分析超声波检查结果是否与患该疾病有关。",
             "附："]),
    dict(no=16, sec="S4", type="solution", score=15,
         stem=r"设数列 $\{a_n\}$ 满足 $a_1=3$，$\dfrac{a_{n+1}}{n}=\dfrac{a_n}{n+1}+\dfrac{1}{n(n+1)}$。",
         opts={},
         subs=[r"(1) 证明：$\{n a_n\}$ 为等差数列；",
               r"(2) 设 $f(x)=a_1x+a_2x^2+\cdots+a_nx^n$，求 $f'(-2)$。"],
         zh=["设数列", "满足", "证明：", "为等差数列；", "设", "，求"]),
    dict(no=17, sec="S4", type="solution", score=15,
         stem=r"如图所示的四棱锥 $P-ABCD$ 中，$PA\perp$ 平面 $ABCD$，$BC\parallel AD$，$AB\perp AD$。",
         opts={},
         subs=[r"(1) 证明：平面 $PAB\perp$ 平面 $PAD$；",
               r"(2) 若 $PA=AB=\sqrt{2}$，$AD=\sqrt{3}+1$，$BC=2$，$P,B,C,D$ 在同一个球面上，设该球面的球心为 $O$。",
               r"(i) 证明：$O$ 在平面 $ABCD$ 上；",
               r"(ii) 求直线 $AC$ 与直线 $PO$ 所成角的余弦值。"],
         figures=["四棱锥 P-ABCD 示意图（PA⊥平面ABCD，含虚线 PA 与实线棱）"],
         zh=["如图所示的四棱锥", "中，", "平面", "，", "，", "证明：平面", "平面", "；",
             "若", "在同一个球面上，设该球面的球心为", "证明：", "在平面", "上；",
             "求直线", "与直线", "所成角的余弦值。"]),
    dict(no=18, sec="S4", type="solution", score=17,
         stem=r"设椭圆 $C:\dfrac{x^2}{a^2}+\dfrac{y^2}{b^2}=1\ (a>b>0)$ 的离心率为 $\dfrac{2\sqrt{2}}{3}$，"
              r"下顶点为 $A$，右顶点为 $B$，$|AB|=\sqrt{10}$。",
         opts={},
         subs=[r"(1) 求椭圆的标准方程；",
               r"(2) 已知动点 $P$ 不在 $y$ 轴上，点 $R$ 在射线 $AP$ 上，且满足 $|AR|\cdot|AP|=3$。",
               r"(i) 设 $P(m,n)$，求点 $R$ 的坐标（用 $m,n$ 表示）；",
               r"(ii) 设 $O$ 为坐标原点，$Q$ 是 $C$ 上的动点，直线 $OR$ 的斜率为直线 $OP$ 的斜率的 $3$ 倍，求 $|PQ|$ 的最大值。"],
         zh=["设椭圆", "的离心率为", "，下顶点为", "，右顶点为", "，", "求椭圆的标准方程；",
             "已知动点", "不在", "轴上，点", "在射线", "上，且满足", "设", "，求点", "的坐标（用",
             "表示）；", "设", "为坐标原点，", "是", "上的动点，直线", "的斜率为直线", "的斜率的", "倍，求",
             "的最大值。"]),
    dict(no=19, sec="S4", type="solution", score=17,
         stem=r"设函数 $f(x)=5\cos x-\cos 5x$。",
         opts={},
         subs=[r"(1) 求 $f(x)$ 在 $\left[0,\dfrac{\pi}{4}\right]$ 的最大值；",
               r"(2) 给定 $\theta\in(0,\pi)$，设 $a$ 为实数，证明：存在 $y\in[a-\theta,a+\theta]$，使得 $\cos y\leqslant\cos\theta$；",
               r"(3) 若存在 $\varphi$ 使得对任意 $x$，都有 $5\cos x-\cos(5x+\varphi)\leqslant b$，求 $b$ 的最小值。"],
         zh=["设函数", "求", "在", "的最大值；", "给定", "，设", "为实数，证明：存在", "，使得",
             "若存在", "使得对任意", "，都有", "，求", "的最小值。"]),
]

# 已核实的文本层缺陷：渲染图正确、文本层 Unicode 错误。
# 这类差异不是转写问题，故单独记录并在报告中说明，而不是当作告警压下。
# 台账必须点名"哪道题、哪个片段、依据是什么"：少了片段名，就等于给整题开了一个
# 无条件吞告警的口子（旧实现是 known = no == 15，该题任何差异都被一并压掉）。
KNOWN_TEXT_LAYER_DEFECTS: list[dict] = [
    {"printed_no": 15, "tokens": ["疾病"],
     "evidence": "渲染图第 2 页第 15 题题干与列联表行标签均显示「疾病」，文本层同处却给出"
                 "「乘病」（PDF 字形-编码映射错误，以渲染图为准，转写保留「疾病」）"},
]

SECTIONS = [
    ("S1", "一、选择题", "单选", "single", 1, 8, 5, 40),
    ("S2", "二、选择题", "多选", "multi", 9, 11, 6, 18),
    ("S3", "三、填空题", "填空", "gapfill", 12, 14, 5, 15),
    ("S4", "四、解答题", "解答", "solution", 15, 19, None, 77),
]

# 比对时只留汉字：公式与标点一律剔除。不能保留标点——该 PDF 的文本层把全角「，」
# 编码成半角「,」，带标点比对会产生大量假告警。比对基元用 bank.cjk_only。


def find_question_spans(doc) -> dict[int, list[tuple[int, float]]]:
    """在文本层中定位每个题号的 (页码, y 坐标)，用于按题裁剪原图。"""
    found: dict[int, list[tuple[int, float]]] = {}
    for pno in range(doc.page_count):
        for blk in doc[pno].get_text("dict")["blocks"]:
            for line in blk.get("lines", []):
                for sp in line["spans"]:
                    t = sp["text"].strip()
                    m = re.fullmatch(r"(\d{1,2})\s*[.．]", t)
                    if m and sp["bbox"][0] < 130:
                        no = int(m.group(1))
                        if 1 <= no <= 19:
                            found.setdefault(no, []).append((pno, sp["bbox"][1]))
    return found


def crop_items(doc, spans: dict[int, list[tuple[int, float]]]) -> dict[int, list[str]]:
    """按题号区间裁剪原图，返回 {题号: [图片相对路径]}。"""
    FIGDIR.mkdir(parents=True, exist_ok=True)
    order = sorted(spans)
    out: dict[int, list[str]] = {}
    for i, no in enumerate(order):
        page_no, y0 = spans[no][0]
        page = doc[page_no]
        nxt = None
        if i + 1 < len(order):
            np_no, ny0 = spans[order[i + 1]][0]
            nxt = (np_no, ny0)
        rects = []
        if nxt and nxt[0] == page_no:
            rects.append((page_no, fitz.Rect(48, y0 - 6, page.rect.width - 40, nxt[1] - 6)))
        else:
            rects.append((page_no, fitz.Rect(48, y0 - 6, page.rect.width - 40,
                                             page.rect.height - 60)))
        paths = []
        for k, (pno, rect) in enumerate(rects):
            if rect.height < 8:
                continue
            pix = doc[pno].get_pixmap(dpi=200, clip=rect)
            name = f"q{no}{'' if k == 0 else f'_{k+1}'}.png"
            pix.save(FIGDIR / name)
            paths.append(f"data/math_items/{name}")
        out[no] = paths
    return out


# 本卷性质决定的不可作答，与解析缺损无关。文案逐字沿用旧实现：界面直接显示这句话。
DOMAIN_UNANSWERABLE = {
    "math_gapfill": "填空题需要求出具体值，而 JEV 只返回类型化判定、不生成数值，暂不可自动作答",
    "math_solution": "解答题需要完整推导与书写，JEV 不生成推导文本，暂不可自动作答",
}
FIGURE_NOTE = "本题含图形，图形信息不在文本层内，作答时需依赖题图"


def _zh_unmatched(q: dict, flat: str) -> list[str]:
    """转写里出现、整卷文本层却找不到的中文片段（只比汉字，公式与标点一律剔除）。"""
    out = []
    for z in q.get("zh", []):
        cjk = bank.cjk_only(z)
        if cjk and cjk not in flat:
            out.append(z)
    return out


def build(out_exam: Path, pdf: str | None = None) -> dict:
    src = Path(pdf) if pdf else PDF
    if not src.exists():
        sys.exit(f"未找到数学试卷 PDF: {src}")
    doc = fitz.open(str(src))
    spans = find_question_spans(doc)
    crops = crop_items(doc, spans)
    flat = bank.cjk_only("".join(doc[i].get_text() for i in range(doc.page_count)))

    raw_items: list[dict] = []
    validations: list[dict] = []
    explained: dict[int, set[str]] = {}

    for q in TRANSCRIPTION:
        no, kind = q["no"], f"math_{q['type']}"
        sec_name = next(s[1] for s in SECTIONS if s[0] == q["sec"])
        raw_items.append({
            "printed_no": no, "no": no, "section_id": q["sec"], "section_name": sec_name,
            "part": sec_name.split("、")[0], "kind": kind,
            "stem": q["stem"], "options": q["opts"], "score": q["score"],
            "images": crops.get(no, []),
            **{k: q[k] for k in ("subs", "table", "crit_table", "appendices", "figures")
               if q.get(k)},
        })
        if not crops.get(no):
            validations.append(bank.validation("crop_missing", severity="warn",
                                              item_id=f"M{no}", printed_no=no))

        unmatched = _zh_unmatched(q, flat)
        # 台账只对**它点名的那个片段**免告警，不再对整题一键吞掉
        entry = next((e for e in KNOWN_TEXT_LAYER_DEFECTS if e["printed_no"] == no), None)
        muted = [t for t in (entry or {}).get("tokens", []) if any(t in z for z in unmatched)]
        explained[no] = set(muted)
        rest = [z for z in unmatched if not any(t in z for t in (entry or {}).get("tokens", []))]
        if rest:
            validations.append(bank.validation(
                "transcription_unmatched", severity="warn", item_id=f"M{no}", printed_no=no,
                detail="、".join(rest), count=len(rest)))

    # 台账逐条留痕：仍触发的写成"原件缺陷"，不再触发的明说台账可能过期
    for e in KNOWN_TEXT_LAYER_DEFECTS:
        live = e["printed_no"] in explained and explained[e["printed_no"]]
        validations.append(bank.validation(
            "source_defect", severity="info", printed_no=e["printed_no"],
            detail=e["evidence"] + (
                f"｜当前仍触发（片段：{'、'.join(sorted(live)) if live else ''}）"
                if live else "｜本次解析未再触发该差异，请复核后从台账中删除")))

    items, dup_vs = bank.renumber(raw_items, id_prefix="M")
    validations += dup_vs
    for k, it in enumerate(items):
        kind = it["kind"]
        done, vs = bank.complete_item(
            it, allowed="ABCD", min_options=4,
            expects_options=kind in ("math_single", "math_multi"),
            domain_reason=DOMAIN_UNANSWERABLE.get(kind))
        if done.get("figures"):
            done["notice"] = "；".join(filter(None, [done["notice"], FIGURE_NOTE]))
        items[k] = done
        validations += vs

    sections = []
    for sid, name, short, kind, lo, hi, per, full in SECTIONS:
        cnt = sum(1 for it in items if it["section_id"] == sid)
        sections.append({"id": sid, "name": name, "short": short, "kind": kind,
                         "no_from": lo, "no_to": hi, "per": per, "full": full,
                         "count": cnt,
                         "desc": {"single": "四选一", "multi": "多选（全部选对得满分，部分选对得部分分，有错选得 0 分）",
                                  "gapfill": "填空题", "solution": "解答题，需写出文字说明、证明过程或演算步骤"}[kind]})

    exam = bank.build_bank(
        subject="数学", source=src.name, items=items, sections=sections,
        loss=bank.build_loss(spans.keys(), items),
        validations=validations,
        meta={"pages": doc.page_count, "total_score": 150, "figure_items": [i["id"] for i in items if i["images"]],
              "title": "2025 年普通高等学校招生全国统一考试（新Ⅰ卷）数学",
              "paper_note": "卷首自述为「回忆版，可能会有些许错误」，题目本身可能与正式卷有出入"},
        generated_by="src/parse_math_pdf.py",
        method="文本层取结构；公式为**人工**对着 200 DPI 按题裁图逐题写 LaTeX（机器不复核公式正确性）；"
               "自动检查只有一个方向：转写里的中文能否在文本层找到")
    bank.write_bank(exam, out_exam)
    return exam


def report(exam: dict) -> None:
    bank.print_bank_report(exam)
    print(f"满分: {exam['meta']['total_score']}   说明: {exam['meta']['paper_note']}")
    fig = exam["meta"]["figure_items"]
    print(f"已裁出原图 {len(fig)} 题: {fig}")
    print("不可作答原因: 填空题需生成数值、解答题需生成推导文本（JEV 只回类型化判定）")
    print("检查限度: transcription_unmatched 只比中文、且只看文本层——公式写错查不出，"
          "编造的中文只要文本层里有也查不出")


def main() -> None:
    argv = sys.argv[1:]
    out = OUT
    if "--out" in argv:
        i = argv.index("--out")
        out = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    pdf = next((a for a in argv if not a.startswith("-")), None)
    report(build(out, pdf))


if __name__ == "__main__":
    main()
