"""上传管线：把用户按模板填写的题库 JSON 变成信封（bank envelope），并做结构验收。

分工与诚实性
------------
- 本模块只做**结构**验收：字段齐不齐、引用在不在、spec 认不认领、编号连不连续。
  它不核验内容——「题面是否与原件一致」在没有原件物证的情况下机器无从判断，
  所以上传题库的 meta.verified 恒为 False（bank.apply_verified 如实写明阻碍项）。
- 任何 error 级记录都会让上传被拒：工作台宁可没有题库，也不要一份会悄悄算错卷的题库。
- 归一化只做「补默认值与重编号」，不改写用户内容；缺损题降级
  （answerable=False + 原因），永不删除条目。

用户文件的形状见 templates/README.md；本模块是那份文档的执行者，两边要一起改。
"""
from __future__ import annotations

import re
from typing import Any

import bank

# 判定层认识的 kind。上传时给未知 kind 只是告警（spec 的 kind_default 会兜底），
# 但前端题型显示与「不可作答」原因表都以这份清单为准。
KNOWN_KINDS = {
    "mcq", "cloze", "seven", "gapfill", "listening", "writing", "judge",
    "math_single", "math_multi", "math_gapfill", "math_solution",
}

# 这些 kind 本身就超出「选择/判断」的产品边界：整题按性质判不可作答，
# 原因写进 notice（domain_reason），不算题面缺损、不占损失账目。
DOMAIN_UNANSWERABLE = {
    "listening": "听力题需音频材料，本工作台不可作答",
    "writing": "写作题需生成文本，本工作台不可作答",
    "gapfill": "语法填空需生成词形，本工作台不可作答",
    "math_gapfill": "填空题需生成数值/表达式，本工作台不可作答",
    "math_solution": "解答题需生成完整解题过程，本工作台不可作答",
}

# 天然没有选项的题型：不按「缺选项」的缺损判定（其余题型缺选项仍是 error）。
NO_OPTIONS_KINDS = {"gapfill", "writing", "math_gapfill", "math_solution"}

_LETTERS = "ABCDEFG"
_MAX_FILENAME = 80


# ---------------------------------------------------------------------------
# 文件名 → 试卷 id
# ---------------------------------------------------------------------------
def paper_id_from_filename(name: str) -> str:
    """题库文件名（去扩展名）即试卷 id。路径分隔符等危险字符一律替换成下划线。"""
    stem = re.sub(r"\.json$", "", str(name or "").strip(), flags=re.I)
    stem = stem.replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip().strip(".") or "bank"
    return stem[:_MAX_FILENAME]


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def _norm_meta(raw: Any, errs: list[dict]) -> dict:
    meta_raw = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    meta: dict[str, Any] = {}
    for field in ("title", "subject"):
        v = str(meta_raw.get(field) or "").strip()
        if not v:
            errs.append(bank.validation(
                "upload_item_invalid", severity="error",
                detail=f"meta.{field} 必填（非空字符串）"))
        meta[field] = v or "（未命名）"
    specs = meta_raw.get("specs")
    if (not isinstance(specs, list) or not specs
            or not all(isinstance(s, str) and s.strip() for s in specs)):
        errs.append(bank.validation(
            "upload_item_invalid", severity="error",
            detail="meta.specs 必填：非空的 spec 名数组，如 [\"english\"] 或 [\"math\", \"math_multi\"]"))
        specs = []
    meta["specs"] = [str(s).strip() for s in specs]
    meta["has_latex"] = bool(meta_raw.get("has_latex"))
    # id_prefix：轨迹文件名前缀（{前缀}{序号}.json）。默认 Q；从旧版信封迁来的题库
    # 带上原前缀（M/X…），既有轨迹就能继续被认领，而不是整卷"未求解"。
    prefix = str(meta_raw.get("id_prefix") or "Q").strip().upper()
    if len(prefix) != 1 or not ("A" <= prefix <= "Z"):
        errs.append(bank.validation(
            "upload_item_invalid", severity="error",
            detail=f"meta.id_prefix 需为单个字母（A–Z），实为 {prefix!r}"))
        prefix = "Q"
    meta["id_prefix"] = prefix
    for field in ("paper_note", "source_note"):
        if meta_raw.get(field):
            meta[field] = str(meta_raw[field])
    # 原件缺陷台账（可选）：逐条展示在体检报告里，不用结构化记录表达
    defects = meta_raw.get("known_text_layer_defects")
    if isinstance(defects, list) and defects:
        meta["known_text_layer_defects"] = [str(x) for x in defects]
    return meta


def _norm_passages(raw: Any, errs: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen_ids: set[str] = set()
    for i, p in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(p, dict):
            errs.append(bank.validation(
                "upload_item_invalid", severity="error",
                detail=f"passages[{i}] 需为对象（id + paragraphs）"))
            continue
        pid = str(p.get("id") or "").strip()
        if not pid:
            errs.append(bank.validation(
                "upload_item_invalid", severity="error",
                detail=f"passages[{i}] 缺 id"))
            continue
        if pid in seen_ids:
            errs.append(bank.validation(
                "upload_item_invalid", severity="error",
                detail=f"passages[{i}] 的 id={pid} 与前面的篇章重复"))
            continue
        seen_ids.add(pid)
        paragraphs = p.get("paragraphs")
        if paragraphs is None and p.get("text"):
            paragraphs = re.split(r"\n\s*\n", str(p["text"]))
        if not isinstance(paragraphs, list):
            paragraphs = []
        passage = bank.make_passage(
            pid, str(p.get("label") or "") or None,
            [str(x) for x in paragraphs], title=str(p.get("title") or ""))
        if not passage["sentences"]:
            errs.append(bank.validation(
                "passage_no_sentences", severity="warn",
                detail=f"passages[{i}]（{pid}）没有可切分的句子——证据定位在该篇章上不可用"))
        out.append(passage)
    return out


def _norm_option_letter(k: Any) -> str | None:
    s = str(k or "").strip().upper()
    return s if len(s) == 1 and s in _LETTERS else None


def _norm_item(idx: int, raw: Any, passage_ids: set[str], errs: list[dict]) -> dict | None:
    if not isinstance(raw, dict):
        errs.append(bank.validation(
            "upload_item_invalid", severity="error",
            detail=f"items[{idx}] 需为对象"))
        return None
    it = dict(raw)

    no = raw.get("no")
    if no is not None and (isinstance(no, bool) or not isinstance(no, int)):
        errs.append(bank.validation(
            "upload_item_invalid", severity="error",
            detail=f"items[{idx}] 的 no 需为整数，实为 {no!r}"))
        return None

    stem = raw.get("stem")
    if stem is None or not isinstance(stem, str):
        errs.append(bank.validation(
            "upload_item_invalid", severity="error",
            detail=f"items[{idx}] 的 stem 必填（字符串题干）"))
        return None

    opts_raw = raw.get("options") or {}
    if not isinstance(opts_raw, dict):
        errs.append(bank.validation(
            "upload_item_invalid", severity="error",
            detail=f"items[{idx}] 的 options 需为对象（如 {{\"A\": \"…\", \"B\": \"…\"}}）"))
        return None
    options: dict[str, str] = {}
    bad_letters = []
    for k, v in opts_raw.items():
        L = _norm_option_letter(k)
        if L is None:
            bad_letters.append(str(k))
            continue
        options[L] = str(v)
    if bad_letters:
        errs.append(bank.validation(
            "options_letters_invalid", severity="error",
            detail=f"items[{idx}] 的选项字母不是 A–G 的单个字母：{'、'.join(bad_letters)}"))

    passage_ref = raw.get("passage") or raw.get("passage_id")
    passage_id = None
    if passage_ref:
        passage_id = str(passage_ref).strip()
        if passage_id not in passage_ids:
            errs.append(bank.validation(
                "passage_unknown", severity="error",
                detail=f"items[{idx}]（no={no}）引用的篇章 id={passage_id} 不在 passages 里"))

    kind = str(raw.get("kind") or "").strip() or None
    if kind and kind not in KNOWN_KINDS:
        errs.append(bank.validation(
            "kind_unknown", severity="warn",
            detail=f"items[{idx}] 的 kind={kind}；已知：{', '.join(sorted(KNOWN_KINDS))}"))

    # 标准答案：answer / standard_answer 均可；只保留本卷存在的选项字母
    ans_raw = str(raw.get("standard_answer") or raw.get("answer") or "").upper()
    if ans_raw:
        letters = [c for c in ans_raw if c in options]
        dropped = [c for c in ans_raw if c.isalpha() and c not in options]
        if dropped:
            errs.append(bank.validation(
                "standard_answer_invalid", severity="warn", printed_no=no,
                detail=f"items[{idx}] 答案里的 {'、'.join(dropped)} 不是本题选项"
                       f"（本题选项：{'、'.join(sorted(options)) or '无'}）"))
        it["standard_answer"] = "".join(sorted(set(letters))) or None
    else:
        it["standard_answer"] = None

    it["no"] = no
    it["printed_no"] = no
    it["stem"] = stem
    it["options"] = options
    it["kind"] = kind
    it["material"] = str(raw.get("material") or "")
    it["passage_id"] = passage_id
    # section / section_name 两种写法都认：模板用 section，旧版信封存的是 section_name
    section = raw.get("section") or raw.get("section_name")
    if section:
        it["section_name"] = str(section)
    if raw.get("part"):
        it["part"] = str(raw["part"])
    # 上传方可显式声明某题不可作答（如选项在原件中是图片）：如实保留原因，
    # 不再按"缺选项"判缺损——声明是显式的，体检报告逐条登记
    if raw.get("answerable") is False:
        it["_declared_unanswerable"] = True
        it["_declared_notice"] = str(raw.get("notice") or raw.get("unparseable_reason") or "").strip()
    # 表格、小问、附注、图示说明等可选字段原样透传（判定 spec 会按 state_from 取用）
    for f in ("score", "subs", "appendices", "table", "crit_table", "figures", "images"):
        if raw.get(f) is not None:
            it[f] = raw[f]
    return it


def _norm_sections(raw: Any, errs: list[dict]) -> list[dict]:
    out: list[dict] = []
    for i, s in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(s, dict) or not str(s.get("name") or "").strip():
            errs.append(bank.validation(
                "upload_item_invalid", severity="error",
                detail=f"sections[{i}] 需为带 name 的对象"))
            continue
        entry = {"name": str(s["name"]).strip()}
        if s.get("part"):
            entry["part"] = str(s["part"])
        for f in ("per", "full"):
            if s.get(f) is not None and isinstance(s[f], (int, float)) \
                    and not isinstance(s[f], bool):
                entry[f] = s[f]
        if s.get("count") is not None and isinstance(s["count"], int) \
                and not isinstance(s["count"], bool):
            entry["expected_count"] = s["count"]
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# 信封组装与结构验收
# ---------------------------------------------------------------------------
def _check_specs(spec_names: list[str], items: list[dict], errs: list[dict]) -> None:
    """spec 名必须存在；全部 spec 都声明 covers_kinds 时，**可作答条目**的 kind
    必须被认领。不可作答的条目不参与（求解时根本不会为它们选 spec——
    数学卷的 math_gapfill/math_solution 就是这种情况：条目要在，但不判）。"""
    from judge import spec as spec_mod

    specs = []
    for name in spec_names:
        try:
            specs.append(spec_mod.get(name))
        except spec_mod.SpecError:
            errs.append(bank.validation(
                "spec_unknown", severity="error",
                detail=f"meta.specs 里的 {name!r} 不是 src/judge/specs/ 下的 spec 名"))
    if len(specs) != len(spec_names) or not specs:
        return
    if any(not s.covers_kinds for s in specs):
        return          # 存在默认 spec：任何 kind（含空）都由它兜底
    for it in items:
        if not it.get("answerable"):
            continue
        try:
            spec_mod.resolve(tuple(spec_names), it)
        except spec_mod.SpecError as e:
            errs.append(bank.validation(
                "kind_unclaimed", severity="error",
                printed_no=it.get("printed_no"),
                detail=f"条目（no={it.get('no')}，kind={it.get('kind') or '（空）'}）：{e}"))


def ingest(raw: Any, *, filename: str = "uploaded.json") -> tuple[dict | None, list[dict]]:
    """把用户的原始 JSON 变成 v2 题库信封。

    返回 (bank, errors)。errors 非空时 bank 为 None——**error 即拒绝**，
    warn/info 随题库入库并出现在体检报告里。每个 error 附带渲染好的 message，
    供界面直接展示。
    """
    errs: list[dict] = []
    if not isinstance(raw, dict):
        errs.append(bank.validation("upload_item_invalid", severity="error",
                                    detail="顶层必须是 JSON 对象"))
        return None, _out(errs)

    meta = _norm_meta(raw, errs)
    passages = _norm_passages(raw.get("passages"), errs)
    sections_declared = _norm_sections(raw.get("sections"), errs)

    items_raw = raw.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        errs.append(bank.validation(
            "upload_item_invalid", severity="error",
            detail="items 必填：非空的题目数组"))
        return None, _out(errs)

    passage_ids = {p["id"] for p in passages}
    items: list[dict] = []
    next_no = 0
    for idx, raw_item in enumerate(items_raw):
        it = _norm_item(idx, raw_item, passage_ids, errs)
        if it is None:
            continue
        if it["no"] is None:
            it["no"] = 0          # 占位；renumber 前统一补号
            it["printed_no"] = 0
            errs.append(bank.validation(
                "item_no_missing", severity="info",
                detail=f"items[{idx}] 未给卷面号，将按出现顺序编号"))
        next_no = max(next_no, int(it["no"]))
        items.append(it)
    if not items:
        return None, _out(errs)

    # 缺号补齐：从现有最大号之后顺序续编（不改用户已给的号）
    for it in items:
        if it["no"] == 0:
            next_no += 1
            it["no"] = next_no
            it["printed_no"] = next_no

    items, dup_vs = bank.renumber(items, id_prefix=meta["id_prefix"])
    errs.extend(dup_vs)

    # 小节：条目自带的小节名归桶，与用户声明的 sections 对账
    secs, items = bank.attach_sections(items)
    used: set[str] = set()
    ordered: list[dict] = []
    for s in sections_declared:
        key_part = s.get("part", "")
        hit = next((b for b in secs if b["name"] == s["name"]
                    and (b.get("part") or "") == key_part), None)
        if hit is None:
            # 声明里没写 part（条目却有）：同名桶唯一时按名字认领，而不是判成
            # "零条目"再把 per/full 丢掉。同名多个则宁可告警也不猜。
            same_name = [b for b in secs if b["name"] == s["name"] and b["id"] not in used]
            if len(same_name) == 1:
                hit = same_name[0]
        if hit is None:
            errs.append(bank.validation(
                "section_count_mismatch", severity="warn",
                detail=s["name"], expected=s.get("expected_count"), actual=0))
            continue
        used.add(hit["id"])
        if s.get("expected_count") is not None and s["expected_count"] != hit["count"]:
            errs.append(bank.validation(
                "section_count_mismatch", severity="warn",
                detail=s["name"], expected=s["expected_count"], actual=hit["count"]))
        merged = {**hit, **{k: v for k, v in s.items() if k not in ("name", "part")}}
        ordered.append(merged)
    for b in secs:
        if b["id"] not in used:
            errs.append(bank.validation(
                "section_undeclared", severity="warn", detail=b["name"]))
            ordered.append(b)
    secs = ordered

    # 可作答性判定（缺损降级，不删题）+ 领域性不可作答 + 上传方显式声明
    finished: list[dict] = []
    for it in items:
        declared = it.pop("_declared_unanswerable", False)
        declared_notice = str(it.pop("_declared_notice", "") or "")
        allowed = _ABCDEFG(it["options"])
        if declared:
            done, vs = bank.complete_item(
                it, allowed=allowed, expects_options=False,
                domain_reason=declared_notice or "题库显式声明该题不可作答（未给原因）")
            errs.extend(vs)
            errs.append(bank.validation(
                "declared_unanswerable", severity="info",
                item_id=done.get("id"), printed_no=done.get("printed_no"),
                detail=done["notice"]))
        else:
            done, vs = bank.complete_item(
                it, allowed=allowed,
                expects_options=it.get("kind") not in NO_OPTIONS_KINDS,
                domain_reason=DOMAIN_UNANSWERABLE.get(it.get("kind") or ""))
            errs.extend(vs)
        finished.append(done)
    items = finished

    if not any(i.get("answerable") for i in items):
        errs.append(bank.validation("no_answerable", severity="warn"))

    _check_specs(meta["specs"], items, errs)

    # 此刻仍为 error → 拒绝上传
    if any(e["severity"] == "error" for e in errs):
        return None, _out(errs)

    loss = bank.build_loss([i["printed_no"] for i in items if i.get("printed_no") is not None],
                           items)
    bank_obj = bank.build_bank(
        subject=meta["subject"], source=filename, items=items, sections=secs,
        passages=passages, loss=loss, validations=errs,
        meta={"title": meta["title"], "specs": meta["specs"], "has_latex": meta["has_latex"],
              "id_prefix": meta["id_prefix"],
              **{k: meta[k] for k in
                 ("paper_note", "source_note", "known_text_layer_defects") if k in meta}},
        generated_by="upload.py（用户按模板上传）", method="按模板上传")
    return bank_obj, []


def _ABCDEFG(options: dict) -> str:
    """条目自己的字母表：A 一直取到最大字母（{A,B,D} → ABCD，缺 C 会被点名）。"""
    if not options:
        return _LETTERS
    top = max(ord(str(k)) for k in options if str(k).isalpha() and str(k) in _LETTERS)
    return _LETTERS[: max(1, top - ord("A") + 1)]


def _out(errs: list[dict]) -> list[dict]:
    """对外错误格式：结构化记录 + 渲染句。渲染失败不让上传流程崩掉。"""
    out = []
    for v in errs:
        row = dict(v)
        try:
            row["message"] = bank.render_validation(v)
        except Exception:  # noqa: BLE001 — 未登记 code 不该挡住错误汇报本身
            row["message"] = str(v.get("detail") or v.get("code"))
        out.append(row)
    return out
