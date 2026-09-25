"""抄题端点：PDF → 逐页多模态请求 → 带 probe 的草稿 → 定位切区 → v2 草稿题库。

这里只做三件事：**问模型、把模型的话钉在纸上的位置、交给验收器**。
盖章不在这里——`verify.verify_draft` 是纯代码，模型无权给自己的输出背书。
这条分工是整个抽取层存在的理由：JEV 契约只返回类型化判定（choice/noul/score + 概率），
它不产文本，所以"抄题"和"判定"是两个不可互替的模型角色。

配置（环境变量或仓库根 .env，任一即可）：
    TRANSCRIBE_BASE_URL   OpenAI 兼容端点，如 https://api.example.com/v1
    TRANSCRIBE_MODEL      多模态模型名
    TRANSCRIBE_API_KEY    密钥
三项缺一即报错退出，**绝不退回"用上次那份草稿"**——静默复用旧草稿是这一层最坏的死法。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bank                                             # noqa: E402
from . import geometry as G                             # noqa: E402

PROTOCOL_TEXT = (HERE / "protocol.md").read_text(encoding="utf-8")
DEFAULT_DPI = 150
MAX_TOKENS = 8000
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
# 页面文本层少于该字符数即视为扫描页：真文本页几百字起步，扫描页往往只有
# 水印/页眉的零星字符（如「XX教育微信公众号…」二三十字）。阈值取中间量。
SCANNED_PAGE_CHARS = 150
ENV_NAMES = {
    "base_url": ("TRANSCRIBE_BASE_URL", "OPENAI_BASE_URL"),
    "model": ("TRANSCRIBE_MODEL", "OPENAI_MODEL"),
    "api_key": ("TRANSCRIBE_API_KEY", "OPENAI_API_KEY"),
}


class TranscribeError(RuntimeError):
    pass


class ConfigError(TranscribeError):
    pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _dotenv() -> dict[str, str]:
    env: dict[str, str] = {}
    for p in (Path.cwd() / ".env", HERE.parent.parent / ".env"):
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def load_config(base_url: str = "", model: str = "",
                api_key: str = "") -> dict[str, str]:
    """取抄题端点配置：命令行参数 > 环境变量 > .env。缺失项**逐个点名**。"""
    cache = _dotenv()
    cli = {"base_url": base_url, "model": model, "api_key": api_key}
    out: dict[str, str] = {}
    missing: list[str] = []
    for field, names in ENV_NAMES.items():
        val = (cli.get(field) or "").strip()
        if not val:
            for n in names:
                val = (os.environ.get(n) or cache.get(n) or "").strip()
                if val:
                    break
        if not val:
            missing.append(" / ".join(names))
        out[field] = val
    if missing:
        raise ConfigError(
            "抄题端点配置不完整，缺：" + "、".join(missing) + "。"
            "可在环境变量或仓库根 .env 里补，或用 --base-url/--model/--api-key 传入。"
            "本工具**不会**改用上一次留下的草稿：静默复用旧草稿会让"
            "「这份题库来自哪份 PDF」无从回答。")
    if not out["base_url"].startswith(("http://", "https://")):
        raise ConfigError(f"base_url 必须是 http(s) 端点，实得 {out['base_url']!r}")
    return out


# ---------------------------------------------------------------------------
# 与模型对话
# ---------------------------------------------------------------------------
def _data_url(png_b64: str) -> str:
    return f"data:image/png;base64,{png_b64}"


def _extract_json(text: str) -> dict:
    """从模型回复里取出那个 JSON 对象。容错只到「剥掉 ```json 围栏与前后废话」为止。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    start = t.find("{")
    if start < 0:
        raise TranscribeError(f"模型没有返回 JSON 对象，前 200 字：{t[:200]!r}")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except json.JSONDecodeError as e:
                    raise TranscribeError(f"模型返回的 JSON 解析失败：{e}") from e
    raise TranscribeError(f"模型返回的 JSON 括号不配对，前 200 字：{t[:200]!r}")


def ask_page(cfg: dict[str, str], png_b64: str, page_no: int, *,
             timeout: float = 300.0, attempts: int = 3) -> tuple[dict, dict]:
    """请求一页，返回 (解析后的草稿块, usage)。网络错误重试，语义错误不重试。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {
        "model": cfg["model"],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": PROTOCOL_TEXT},
            {"role": "user", "content": [
                {"type": "text",
                 "text": (f"这是第 {page_no} 页的整页扫描图。按协议只输出一个 JSON 对象，"
                          f"不要任何解释文字。本层所有条目的 page 字段都填 {page_no}。")},
                {"type": "image_url", "image_url": {"url": _data_url(png_b64)}},
            ]},
        ],
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}",
               "Content-Type": "application/json"}
    last: Any = None
    for try_no in range(1, attempts + 1):
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
            if try_no < attempts:
                time.sleep(2.0 * try_no)
                continue
            raise TranscribeError(f"第 {page_no} 页请求失败：{last}") from e
        if r.status_code in RETRY_STATUS and try_no < attempts:
            time.sleep(2.0 * try_no)
            continue
        if r.status_code >= 400:
            raise TranscribeError(
                f"端点返回 {r.status_code}（第 {page_no} 页，第 {try_no} 次）："
                f"{r.text[:400]}")
        data = r.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise TranscribeError(
                f"响应不是 OpenAI 兼容的 choices[0].message.content 形状：{e}；"
                f"键={list(data) if isinstance(data, dict) else type(data).__name__}") from e
        return _extract_json(content), (data.get("usage") or {})
    raise TranscribeError(f"第 {page_no} 页重试 {attempts} 次仍失败：{last}")


# ---------------------------------------------------------------------------
# 草稿 → 定位 → v2 信封
# ---------------------------------------------------------------------------
def scanned_printed_nos(doc, *, max_no: int = 300) -> list[int]:
    """卷面出现过的题号，**只从原始文本层**数出来。

    不能从条目反推：那样「丢了哪些题」这个问题就依赖丢失的结果本身，审计失去意义。

    必须走 `bank.question_number_re()`：`bank.Q_NUM_TAIL_TEMPLATE` 是模板，
    pattern 里含字面量 `{1,DIGITS}`，拿它去 match 任何一行都返回 None。
    误用它会让 seen 恒为空集，于是 never_parsed 检查永真通过——
    整条"防静默丢题"的链路会被一个正则用法安静地废掉。
    """
    qre = bank.question_number_re(3)
    nos: set[int] = set()
    for page in doc:
        for line in page.get_text().splitlines():
            m = qre.match(line)
            if m and not bank.looks_like_decimal_prefix(line):
                n = int(m.group(1))
                if 1 <= n <= max_no:
                    nos.add(n)
    return sorted(nos)


def pin_probes(entries: list[dict], doc) -> tuple[list[dict], list[dict], dict]:
    """把模型交回的 probe 钉到纸面坐标，据此切出每条目自己的区域。

    产出带 provenance 的条目，以及三类必留痕的记录：
      probe_missing    模型没交锚点 → 该条目无法定位，区域只能靠猜
      probe_not_found  锚点在该页文本层里找不到 → 要么抄错了要么这页不是那页
      probe_duplicate  两条目定位到同一处 → 至少有一条在蒙
    """
    streams: dict[int, tuple[str, list[list[float]]]] = {}
    per_page: dict[int, list[tuple[int, list[float]]]] = {}
    scanned_pages: set[int] = set()
    vs: list[dict] = []
    staged: list[dict] = []

    for e in entries:
        page_no = e.get("page")
        pn = e.get("printed_no")
        probe = "".join(str(e.get("probe") or "").split())
        if page_no is None:
            vs.append(bank.validation("probe_missing", severity="error", printed_no=pn,
                                      detail="条目没有 page 字段，无法定位原件"))
            staged.append(dict(e, _page=None, _anchor=None))
            continue
        if page_no not in streams:
            if not 1 <= int(page_no) <= doc.page_count:
                # 这里绝不能 continue 了事：直接丢条目等于替模型把题「吃掉」，
                # 损失账目之外又开了一条静默丢失的暗道。
                vs.append(bank.validation(
                    "probe_not_found", severity="error", printed_no=pn,
                    detail=f"声称在第 {page_no} 页，但本 PDF 只有 {doc.page_count} 页"))
                staged.append(dict(e, _page=page_no, _anchor=None))
                continue
            streams[page_no] = G.page_char_stream(doc[page_no - 1])
            if len(streams[page_no][0]) < SCANNED_PAGE_CHARS:
                scanned_pages.add(page_no)
        if page_no in scanned_pages:
            # 扫描页没有文本层：probe 无从在原文上定位，但条目绝不能因此被吃掉
            # 或降级——抄题本身（看图）不受影响，只是验收比对不可用，留痕即可。
            staged.append(dict(e, _page=page_no, _anchor=None, _scanned=True))
            continue
        if not probe:
            vs.append(bank.validation(
                "probe_missing", severity="error", printed_no=pn,
                detail=(f"第 {page_no} 页的该条目没有 probe（定位锚点）。"
                        f"协议要求交回该题原文里最长的连续汉字段")))
            staged.append(dict(e, _page=page_no, _anchor=None))
            continue
        stream, boxes = streams[page_no]
        idx = G.locate_needle(stream, probe)
        if idx < 0:
            vs.append(bank.validation(
                "probe_not_found", severity="error", printed_no=pn,
                detail=(f"probe {probe!r} 在第 {page_no} 页的文本层里找不到"
                        f"→ 要么这条不是原文，要么页码报错了")))
            staged.append(dict(e, _page=page_no, _anchor=None))
            continue
        anchor = G.rect_union(boxes[idx:idx + len(probe)]) or boxes[idx]
        per_page.setdefault(page_no, []).append((pn, anchor))
        staged.append(dict(e, _page=page_no, _anchor=anchor))

    rects: dict[int, dict[Any, list[float]]] = {}
    dup_failed: set[Any] = set()
    for page_no, anchors in per_page.items():
        r, failed = G.slice_page_rects(doc[page_no - 1], anchors)
        rects[page_no] = r
        for pn in failed:
            dup_failed.add(pn)
            vs.append(bank.validation(
                "probe_duplicate", severity="error", printed_no=pn,
                detail=(f"第 {page_no} 页上有两条目的锚点落在同一行同一列"
                        f"→ 至少有一条定位不可信，其区域比对已跳过")))

    items: list[dict] = []
    for e in staged:
        page_no, anchor = e.pop("_page"), e.pop("_anchor")
        pn = e.get("printed_no")
        if e.pop("_scanned", False):
            # 扫描页条目：不设 _unpinned（不降级），由 to_draft 统一留 text_layer_missing
            e["_scanned"] = True
        else:
            rect = (rects.get(page_no) or {}).get(pn)
            if anchor is None or rect is None or pn in dup_failed:
                e["_unpinned"] = True
            else:
                e["provenance"] = {"page": page_no, "rect": [round(v, 2) for v in rect],
                                   "rect_derived": True,
                                   "probe": "".join(str(e.get("probe") or "").split())}
        items.append(e)
    return items, vs, {"pages_used": sorted(streams), "pinned": sum(
        1 for i in items if i.get("provenance"))}


KIND_MAP = {"single": "single_choice", "multi": "multi_choice", "judge": "judge",
            "gapfill": "gapfill", "solution": "solution", "listening": "listening"}


def to_draft(items: list[dict], dropped: list[dict], doc, *,
             subject: str, source: str, model: str) -> dict:
    """条目 → v2 草稿信封（未验收）。"""
    raw: list[dict] = []
    vs: list[dict] = []
    unpinned = {e.get("printed_no") for e in items if e.get("_unpinned")}
    scanned_pns = sorted({e.get("printed_no") for e in items if e.get("_scanned")}
                         - {None})
    # 产品定位：只自动作答选择与判断题。需生成文本/数值或依赖音频的题型
    # 一律以「本卷性质」标注不可作答（不占损失账目），而不是假装能改。
    DOMAIN_NA = {
        "solution": "解答/作文/翻译题需生成文本，本系统只自动作答选择与判断题",
        "gapfill": "填空题需生成数值或词形，本系统不自动作答",
        "listening": "听力题需音频材料，本系统不自动作答",
    }
    for i, e in enumerate(items, 1):
        kind = KIND_MAP.get(str(e.get("kind") or "single"), "single_choice")
        raw.append({
            "printed_no": e.get("printed_no"), "seq": i, "kind": kind,
            "section_name": e.get("section_name") or "",
            "stem": e.get("stem") or "", "options": e.get("options") or {},
            "subs": e.get("sub_questions") or [], "table": e.get("table"),
            "standard_answer": e.get("standard_answer"),
            "provenance": e.get("provenance"),
            "unparseable_reason": ("选项为图形，无法从文本层解析"
                                   if e.get("options_as_image") else None),
            "options_as_image": bool(e.get("options_as_image")),
            "figure": bool(e.get("figure")),
        })
    items2, dup_vs = bank.renumber(raw, id_prefix="T")
    vs += dup_vs
    done: list[dict] = []
    for it in items2:
        # 判据只有一处来源：unpinned 集合。早先这里从 renumber 后的条目上取
        # _unpinned，而那个键在构造 raw 时就没复制过去，条件恒假 —— 降级永不触发。
        lost = it.get("printed_no") in unpinned
        d, cvs = bank.complete_item(
            it, expects_options=it["kind"] in ("single_choice", "multi_choice", "judge"),
            reason_override=("无定位锚点：该条目在原件上钉不住位置" if lost else None),
            domain_reason=DOMAIN_NA.get(it["kind"]))
        done.append(d)
        vs += cvs
        if lost:
            vs.append(bank.validation(
                "probe_unpinned", severity="warn", printed_no=it.get("printed_no"),
                item_id=it.get("id"),
                detail="该条目未能钉住纸面位置，验收器已跳过它与原文的比对"))
    if scanned_pns:
        vs.append(bank.validation(
            "text_layer_missing", severity="warn", ids=scanned_pns, count=len(scanned_pns),
            detail="这些条目所在页没有文本层（扫描卷）：锚点定位与原文比对均不可用，"
                   "条目照常入库但未经验收"))
    seen = scanned_printed_nos(doc)
    loss = bank.build_loss(seen, done, dropped=[
        {"printed_no": d.get("printed_no"),
         "reason": str(d.get("reason") or "抄题端点自报未收录")}
        for d in dropped if d.get("printed_no") is not None])
    # 分节必须走 bank.attach_sections：它同时给条目回填 section_id。
    # 早先这里用自造的 sections_from 只造 sections 不回填，左栏导航直接渲染为空。
    sections, done = bank.attach_sections(done)
    return bank.build_bank(subject=subject, source=source, items=done,
                           sections=sections, loss=loss, validations=vs,
                           meta={"model": model, "stage": "draft"},
                           generated_by="src/extract/transcribe.py",
                           method=(f"多模态抄题（{model}）→ 锚点定位切区 → "
                                   f"纯代码验收器比对；公式内容不与文本层比对"))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def transcribe(pdf: Path, *, cfg: dict[str, str], pages: list[int] | None = None,
               dpi: int = DEFAULT_DPI, verbose: bool = True) -> tuple[dict, dict]:
    """整份 PDF → (未验收草稿, 统计)。"""
    doc = G.open_pdf(str(pdf))
    try:
        todo = pages or list(range(1, doc.page_count + 1))
        entries: list[dict] = []
        dropped: list[dict] = []
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        for pno in todo:
            if not 1 <= pno <= doc.page_count:
                raise TranscribeError(f"请求了第 {pno} 页，本 PDF 只有 {doc.page_count} 页")
            png, w, h = G.render_page_base64(doc, pno - 1, dpi)
            block, usage = ask_page(cfg, png, pno)
            for k in usage_total:
                if isinstance(usage.get(k), int):
                    usage_total[k] += usage[k]
            page_items = block.get("items") or []
            if not isinstance(page_items, list) or not isinstance(block.get("dropped") or [], list):
                raise TranscribeError(f"第 {pno} 页返回的 items/dropped 不是数组，不予采信")
            for e in page_items:
                e["page"] = int(e.get("page") or pno)
            entries.extend(page_items)
            dropped.extend(block.get("dropped") or [])
            if verbose:
                print(f"  第 {pno:>3}/{doc.page_count} 页：条目 {len(page_items)}，"
                      f"自报丢弃 {len(block.get('dropped') or [])}，"
                      f"tokens {usage.get('prompt_tokens')}+{usage.get('completion_tokens')}")
        items, pin_vs, stats = pin_probes(entries, doc)
        draft = to_draft(items, dropped, doc, subject="draft",
                         source=pdf.name, model=cfg["model"])
        draft["validations"] = list(draft.get("validations") or []) + pin_vs
        draft["report"] = bank.render_bank_report_lines(draft)
        stats.update(usage=usage_total, entries=len(entries), dropped=len(dropped))
        return draft, stats
    finally:
        doc.close()


def parse_pages(spec: str) -> list[int]:
    """'1-3,7,10-' → 页码列表。空串表示全卷。"""
    spec = (spec or "").strip()
    if not spec:
        return []
    out: set[int] = set()
    for part in re.split(r"[,，\s]+", spec):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)", part)
        if m:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else 10 ** 6
            out.update(range(a, b + 1))
        else:
            out.add(int(part))
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="PDF → 多模态抄题 → 锚点定位 → 纯代码验收 → v2 题库")
    ap.add_argument("--pdf", required=True, help="源 PDF 路径")
    ap.add_argument("--out", required=True, help="输出题库 JSON 路径")
    ap.add_argument("--pages", default="", help="只处理这些页，如 1-3,7（默认整卷）")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="页图渲染精度")
    ap.add_argument("--base-url", default="", help="OpenAI 兼容端点")
    ap.add_argument("--model", default="", help="多模态模型名")
    ap.add_argument("--api-key", default="", help="密钥（建议走环境变量，别写进命令行历史）")
    ap.add_argument("--no-verify", action="store_true",
                    help="只出草稿、不跑验收器（调试用；产物不得标 verified）")
    ap.add_argument("--allow", default="", help="已知原件缺陷台账 JSON 路径")
    args = ap.parse_args(argv)

    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"[停止] 源 PDF 不存在：{pdf}", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.base_url, args.model, args.api_key)
    except ConfigError as e:
        print(f"[停止] {e}", file=sys.stderr)
        return 2

    print(f"抄题端点：{cfg['base_url']} 模型：{cfg['model']} 源：{pdf.name}")
    draft, stats = transcribe(pdf, cfg=cfg, pages=parse_pages(args.pages), dpi=args.dpi)
    out = Path(args.out)

    if args.no_verify:
        write(out, draft)
        print(f"[未验收] 草稿已写出 {out}——它没有经过验收器，不得标 verified")
        return 0

    from . import verify                                     # 延后导入：只有验收时才要
    allow_entries = []
    if args.allow:
        allow_entries = json.loads(Path(args.allow).read_text(encoding="utf-8"))
    result = verify.verify_draft(draft, pdf, allow_entries=allow_entries)
    write(out, result)
    s = verify.summarize(result)
    print(f"验收：error={s['errors']} warn={s['warns']} "
          f"比对完成 {s['checked']}/{s['by_code'].get('provenance_missing', 0) + s['checked']} 条")
    for b in s["blockers"]:
        print(f"  阻碍 verified：{b}")
    print(f"verified={s['verified']}  → {out}")
    return 0 if not s["errors"] else 1


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
