"""题库信封（bank envelope）的唯一构造器：v2 schema、结构化校验记录、损失账目与体检报告。

为什么要单独有这一层
--------------------
本项目不再从 PDF 提取试卷（提取功能已抽离出去）。题库的唯一来源是**用户按模板
制作并上传的 JSON**（见 ``templates/``），由 ``src/upload.py`` 归一化后送进这里
的构造器。无论题库从哪条路来，信封组装、校验记录、损失账目、体检报告都只有
这一份实现——「答案可被质疑和复核」的全部数据层承诺都落在这些函数上。

诚实性约束在数据层的落点
------------------------
1. Validation 是**结构化记录**，ids 永不截断；人类可读的中文报告由记录渲染而来，
   反向（只有一句没有记录支撑的话）在本层不可能发生。
2. meta.loss 是**必填**字段：卷面出现过的题号、实际保留的题号、丢失原因全部入账。
   loss.dropped 非空的题库不能被判为已核验。
3. 缺损的题**降级而不是删除**：complete_item() 只会把 answerable 置 False 并给出原因，
   绝不丢弃条目——静默丢题正是历史上解析器最大的问题。
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
# 文本工具：篇章构造（上传题库的 passages 需要逐句编号与填空标记）
# ---------------------------------------------------------------------------
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "u.s", "u.k", "no", "fig"}
_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\u201c(])'
                         r'|(?<=[.!?][\u201d"\u2019)])\s+(?=[A-Z"\u201c(])')
_BLANK_MARK = re.compile(r"\[(\d{1,3})\]_{2,}")


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def normalize_blanks(text: str) -> str:
    """把 "…\\n41\\n________ …" 归一为 "… [41]____ …"。必须在合并折行之前做。"""
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


def make_passage(pid: str, label: str | None, paragraphs: list[str],
                 title: str = "") -> dict:
    """由段落数组构造篇章：逐句编号、登记题空号、字数。

    上传题库的 passages 只需要给 ``id`` 与 ``paragraphs``（或整段 ``text``）；
    句子编号（S1、S2…）与题空号（``[n]____`` 标记）由这里统一产出，
    判定层的证据定位（candidates.source='sentences'）依赖这个形状。
    """
    paras = [re.sub(r"\s+", " ", normalize_blanks(p)).strip() for p in paragraphs]
    paras = [p for p in paras if p]
    full = " ".join(paras)
    return {
        "id": pid, "label": label, "title": title,
        "paragraphs": paras,
        "sentences": [{"no": f"S{i + 1}", "text": t} for i, t in enumerate(split_sentences(full))],
        "blanks": sorted({int(x) for x in _BLANK_MARK.findall(full)}),
        "chars": len(full),
    }


def missing_letters(letters: Iterable[str], allowed: str = "ABCD") -> list[str]:
    """按期望字母表列出缺失项，供 unparseable_reason 使用。"""
    have = set(letters)
    return [c for c in allowed if c not in have]


MIN_OPTIONS_DEFAULT = 3


# ---------------------------------------------------------------------------
# 条目完整性
# ---------------------------------------------------------------------------
def complete_item(item: dict, *, allowed: str = "ABCD", min_options: int = MIN_OPTIONS_DEFAULT,
                  expects_options: bool = True, answer_kind: str | None = None,
                  reason_override: str | None = None,
                  domain_reason: str | None = None) -> tuple[dict, list[dict]]:
    """给条目补 printed_no/no/id/seq_note/kind/material/passage_id，并判定可作答性。

    关键：**任何情况下都不丢弃条目**。信息不全只会让 answerable=False 并附原因，
    同时产出一条 Validation 记录。

    domain_reason 与 reason_override 是两回事，别混用：
      reason_override  = 题面缺损（这一题没能做全）→ 计入 meta.loss.dropped
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
    # 否则两选项的判断题会被「少于 3」「缺 C/D」双重误杀。
    kind = answer_kind or it.get("kind")
    if min_options == MIN_OPTIONS_DEFAULT and kind == "judge":
        min_options = 2
    if kind == "judge" and allowed == "ABCD":
        allowed = "AB"

    reason = reason_override
    if expects_options:
        if n_opt == 0:
            reason = reason or "选项未被提供，题面缺损，不作答"
            validations.append(validation(
                "options_are_images" if it.get("options_as_image") else "options_incomplete",
                severity="error", item_id=it.get("id"), printed_no=it["printed_no"],
                detail=reason, expected=min_options, actual=n_opt))
        elif n_opt < min_options:
            code = "options_merged_in_stem" if it.get("options_as_image") else "options_incomplete"
            reason = reason or f"选项只提供 {n_opt} 个（少于 {min_options}），题面可能有缺损，不作答"
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
    """按 (part, section_name) 归桶并回填 section_id。"""
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
# 结构化校验记录
# ---------------------------------------------------------------------------
def validation(code: str, *, severity: str = "warn", item_id: str | None = None,
               printed_no: int | None = None, detail: str = "",
               ids: list[int] | None = None, count: int | None = None,
               expected: Any = None, actual: Any = None) -> dict:
    """构造一条 Validation。ids **永不截断**。

    已知 code（新增需在此登记，保证报告的中文渲染有条可依）：
      options_incomplete          选项数不足（降级为不可作答，不删题）
      options_are_images          选项在原件中是图片，文本层未抽出（历史提取期的遗留形状）
      options_merged_in_stem      选项与题干挤在同一行且未逐个识别（历史提取期的遗留形状）
      stem_suspicious             题干异常（空、过短、被截断迹象）
      duplicate_printed_no        卷面题号重复出现
      identity_mismatch           校验记录指向的条目与它自称的卷面号不一致（编号顺序被写坏）
      never_parsed                卷面出现过该题号但一道条目也没产出（历史提取期的遗留形状）
      draft_dropped               草稿自报该卷面号已被丢弃（历史提取期的遗留形状）
      section_count_mismatch      小节题数与声明数不符
      blank_count_mismatch        篇章解析出的题空数与卷面不符
      passage_no_sentences        篇章未解析出任何句子
      source_defect               原件本身的缺陷（文本层编码错等），已核实并解释
      upload_item_invalid         上传条目缺关键字段或类型不对，无法入库
      options_letters_invalid     选项字母不是 A–G 的单个大写字母
      passage_unknown             条目引用的篇章 id 在 passages 里不存在
      spec_unknown                meta.specs 引用了不存在的判定 spec
      kind_unclaimed              条目的 kind 没有被本卷任何 spec 的 covers_kinds 认领
      standard_answer_invalid     标准答案含本卷选项之外的字母（该部分被忽略）
      kind_unknown                kind 不在已知清单，将按 spec 默认措辞处理
      section_undeclared          条目的小节不在 sections 清单里（已自动补建）
      no_answerable               全卷没有一道可作答题
      item_no_missing             条目缺卷面号 no，已按出现顺序自动编号
      declared_unanswerable       上传方显式声明该题不可作答（原因随条目展示）
      probe_missing               抄题端点没交回定位锚点（历史提取期的遗留形状）
      probe_not_found             probe 在声称那一页的文本层里找不到（历史提取期的遗留形状）
      probe_duplicate             两条目的 probe 落在同一行同一列（历史提取期的遗留形状）
      probe_unpinned              该条目最终没钉住纸面位置（历史提取期的遗留形状）
      text_layer_missing          扫描卷无文本层（历史提取期的遗留形状）
      verify_crashed              验收器处理该条目时抛出异常（历史提取期的遗留形状）
      crop_missing                未能按题裁出原图（历史提取期的遗留形状）
      transcription_extra         转写中有、原文区域内没有（历史提取期的遗留形状）
      transcription_unmatched     转写的中文在原件文本层找不到（历史提取期的遗留形状）
      transcription_missing       原文区域内有、转写中没有（历史提取期的遗留形状）
      formula_unverified          公式无物证（历史提取期的遗留形状）
      provenance_missing          条目缺页码/区域来源信息（历史提取期的遗留形状）
      provenance_unusable         页码/区域指向原件读不出东西的地方（历史提取期的遗留形状）
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
        "options_incomplete": f"{who} 选项只解析到 {v.get('actual')} 个（应不少于 {v.get('expected')}）→ 降级为不可作答",
        "options_are_images": f"{who} 选项在原件中是图片，文本层未抽出任何选项字母 → 不作答、不猜",
        "options_merged_in_stem": f"{who} 选项与题干挤在同一行且未逐个识别（得到 {v.get('actual')} 项）→ 降级为不可作答",
        "stem_suspicious": f"{who} 题干异常：{v.get('detail') or '无可读内容'}",
        "duplicate_printed_no": f"卷面题号重复出现，共 {v.get('count')} 个",
        "identity_mismatch": f"校验记录身份不一致：{v.get('detail')}",
        "never_parsed": f"{who}：卷面出现过但未产出任何条目，共 {v.get('count')} 道",
        "draft_dropped": f"{who} 卷面有此题号，草稿自报未收录：{v.get('detail')}",
        "section_count_mismatch": (f"小节 {v.get('detail') or v.get('item_id')} 解析出 {v.get('actual')} 题"
                                   f"（卷面应为 {v.get('expected')} 题）"),
        "blank_count_mismatch": (f"篇章 {v.get('detail')} 解析出 {v.get('actual')} 个题空"
                                 f"（卷面应为 {v.get('expected')} 个）"),
        "passage_no_sentences": f"篇章 {v.get('detail')} 未解析出任何句子",
        "source_defect": f"{who}：原件缺陷（非转写问题）——{v.get('detail')}",
        "upload_item_invalid": f"{who} 条目不合法，已拒绝入库：{v.get('detail')}",
        "options_letters_invalid": f"{who} 选项字母不合法：{v.get('detail')}",
        "passage_unknown": f"{who} 引用的篇章不存在：{v.get('detail')}",
        "spec_unknown": f"判定 spec 不存在：{v.get('detail')}",
        "kind_unclaimed": f"{who} 的 kind 没有被本卷任何 spec 认领：{v.get('detail')}",
        "standard_answer_invalid": f"{who} 标准答案含本卷不存在的选项字母：{v.get('detail')}（该部分已忽略）",
        "kind_unknown": f"{who} 的 kind 不在已知清单，将按 spec 默认措辞处理：{v.get('detail')}",
        "section_undeclared": f"小节「{v.get('detail')}」未在 sections 清单里声明，已按条目自动补建",
        "no_answerable": "全卷没有任何可作答题：工作台可以打开，但没有题可求解",
        "item_no_missing": f"{who} 未给卷面号，已按出现顺序自动编号",
        "declared_unanswerable": f"{who} 由题库显式声明不可作答：{v.get('detail')}",
        "probe_missing": f"{who} 没有定位锚点（probe），无法在原件上钉住位置：{v.get('detail')}",
        "probe_not_found": f"{who} 的定位锚点在原件该页文本层找不到：{v.get('detail')}",
        "probe_duplicate": f"{who} 与其它条目定位到同一处：{v.get('detail')}",
        "probe_unpinned": f"{who} 未钉住纸面位置，与原文的比对已跳过：{v.get('detail')}",
        "text_layer_missing": f"扫描件无文本层，锚点定位与原文比对不可用",
        "verify_crashed": f"{who} 验收过程中断：{v.get('detail')}",
        "crop_missing": f"{who} 未裁出原图（题号坐标未定位到）",
        "transcription_extra": f"{who} 转写中出现原文该区域没有的内容（疑似编造）：{v.get('detail')}",
        "transcription_unmatched": f"{who} 转写的中文在原件文本层找不到：{v.get('detail')}",
        "transcription_missing": f"{who} 原文该区域有内容未进入转写（疑似漏抄）：{v.get('detail')}",
        "formula_unverified": f"{who} 公式无物证：{v.get('detail')}",
        "provenance_missing": f"{who} 缺少页码/区域来源，无法与原文比对",
        "provenance_unusable": f"{who} 来源区域无法核对：{v.get('detail')}",
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

    seen_printed_nos 必须来自**原始清单**，不能从 items 反推——
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
# 信封组装与读写
# ---------------------------------------------------------------------------
def audit_validations(items: list[dict], validations: list[dict]) -> list[dict]:
    """校验记录的身份必须与它指向的条目一致。

    为什么在这里查而不是写在各处：`printed_no` 是**卷面号**、`no` 是内部序号，
    只有 renumber() 之后两者才各就各位。如果哪个环节把 complete_item 放在 renumber
    前面，它盖进记录的就是序号，于是体检报告会指着一道不存在的题号说话——而这份
    报告正是这个项目对外承诺"可复核"的唯一凭据。顺序是约定，约定会漂；
    这里把它变成数据层面的硬检查。
    """
    by_id = {it.get("id"): it for it in items if it.get("id") is not None}
    real_pn = {it.get("printed_no") for it in items if it.get("printed_no") is not None}
    out: list[dict] = []
    seen: set[tuple[str, str, int | None]] = set()
    for v in validations:
        iid = v.get("item_id")
        pn = v.get("printed_no")
        if iid is None:
            # 只有卷面号的记录（如台账登记）同样得落地：
            # 否则报告里会出现一句指着不存在的题号说的话。
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
                             detail="这些卷面题号在原始清单里出现过，但没有产出任何条目"))
    # 自报丢弃同样必须留在记录里：账目平了不等于「丢弃」没有发生。放在 audit 之后——
    # 被丢弃的题号本来就不在 items 里，不该再给它配发一条 identity_mismatch。
    for d in loss.get("dropped") or []:
        vs.append(validation("draft_dropped", severity="warn",
                             printed_no=int(d["printed_no"]),
                             detail=str(d.get("reason") or "自报未收录")))
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
    """重算 meta.verified：任何 error 级记录、或 loss.dropped/never_parsed 非空 → 取消。

    本项目不做提取，「内容与原件一致」的核验没有物证可查，因此上传题库的
    verified 恒为 False（阻碍项如实写明）。这不影响改卷，但界面会一直亮着。
    """
    m = bank["meta"]
    blockers = errors_of(bank["validations"])
    reasons = []
    if not loss_is_clean(m.get("loss") or {}):
        reasons.append("meta.loss 存在未入账的题目损失")
    if blockers:
        reasons.append(f"{len(blockers)} 条 error 级校验记录")
    if not m.get("verification"):
        reasons.append("题面内容未经独立核验（本题库由用户上传，内容正确性需自行负责）")
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
            f"请按 templates/ 的模板重新制作并上传（不要手工改版本号）。")
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
    """共享的一行：期望题数与状态（OK / 缺 N）。"""
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
    lines: list[str] = ["=" * WIDTH, f"{m.get('subject', '')}试卷题库体检报告", "=" * WIDTH]
    lines.append(f"bank_version=v{m.get('bank_version')}   来源: {m.get('source')}"
                 f"   条目 {m.get('total_items')} 道")
    if m.get("method"):
        lines.append(f"方法: {m['method']}")
    if m.get("paper_note"):
        lines.append(f"题库说明: {m['paper_note']}")
    lines.append("")

    if bank.get("sections"):
        lines.append(f"{'小节':<22}{'题号':<10}{'应有':<6}{'实有':<6}{'选项':<8}{'状态'}")
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
    lines.append(f"  清单出现过的题号 {loss.get('seen_printed_nos', 0)} 个   "
                 f"保留条目 {loss.get('kept', 0)} 道   "
                 f"降级保留 {len(loss.get('dropped') or [])} 道   "
                 f"未产出任何条目 {len(loss.get('never_parsed') or [])} 道")
    for d in loss.get("dropped") or []:
        lines.append(f"  · 卷面第 {d['printed_no']} 题 已保留但降级为不可作答：{d['reason']}")
    if loss.get("never_parsed"):
        lines.append(f"  ✗ 下列卷面题号完全没有条目（必须修）："
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
        lines.append("已核实的原件缺陷（非题库制作问题，逐条列出）：")
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
