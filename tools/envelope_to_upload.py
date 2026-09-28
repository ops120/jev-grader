"""旧版 v2 信封题库 → 上传模板形态 的迁移转换器。

背景：本仓库早期版本从 PDF 提取题库，产出的是 ``bank.py`` 的 v2 信封
（meta/sections/passages/items + validations/loss/report）。工作台现在只接受
「按 templates/README.md 模板填写」的文件；这个工具把旧信封翻译成那个形态，
让已有题库能走上传管线重新入库，而不必照模板重做一遍。

转换 = **字段改名 + 补 meta 声明**，不动题目内容：

  meta.specs / meta.id_prefix / meta.has_latex   ← 旧信封没有，按内置对照表或参数给出
  items[].no          ← printed_no（卷面号就是模板里的 no；内部序号由管线重编）
  items[].section     ← section_name
  items[].passage     ← passage_id
  items[].answer      ← standard_answer
  answerable=False    ← 原样保留（显式声明不可作答 + 原因），管线的
                        declared_unanswerable 分支接住，逐条登记在体检报告里
  passages            ← 只留 id/label/title/paragraphs（句子与题空号由管线重建）

**不迁移**旧的 validations/loss/report：那是当时提取过程的记录，新库由上传管线
重新生成自己的记录。被放弃的记录数会如实打印——迁移不等于把历史抹掉。

用法::

    python tools/envelope_to_upload.py <旧信封.json> [...] --out-dir data/upload_ready
    # 未知卷需显式声明判定方式：
    python tools/envelope_to_upload.py new.json --specs xingce --id-prefix X

轨迹：把旧版轨迹目录复制成 ``data/traces_<新id>/``（id 即输出文件名），上传后
历史判定会直接显示——轨迹按载荷指纹认领，只要题库内容一致就不会错认。
数学卷的题图目录（``data/math_items/``）一并复制到新仓库根下同名位置。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 已知旧卷的对照表：文件名 → (id, id_prefix, specs, has_latex)。
# 与 src/papers.py 的 LEGACY_PAPERS 保持一致（那边是"原路径直接可用"，这边是"转成上传件"）。
KNOWN = {
    "exam.json": ("english", "Q", ["english"], False),
    "exam_math.json": ("math", "M", ["math", "math_multi"], True),
    "exam_xc_副省级.json": ("xc_fs", "X", ["xingce"], False),
    "exam_xc_地市类.json": ("xc_ds", "X", ["xingce"], False),
    "exam_xc_行政执法类.json": ("xc_zf", "X", ["xingce"], False),
}


def convert(env: dict, *, specs: list[str], id_prefix: str, has_latex: bool) -> dict:
    meta = env.get("meta") or {}
    items_out = []
    for it in env.get("items") or []:
        row = {
            "no": it.get("printed_no", it.get("no")),
            "stem": it.get("stem") or "",
            "options": it.get("options") or {},
        }
        for src, dst in (("kind", "kind"), ("passage_id", "passage"),
                         ("section_name", "section"), ("part", "part"),
                         ("material", "material"), ("score", "score"),
                         ("subs", "subs"), ("appendices", "appendices"),
                         ("table", "table"), ("crit_table", "crit_table"),
                         ("figures", "figures"), ("images", "images"),
                         ("explanation", "explanation")):
            if it.get(src) is not None:
                row[dst] = it[src]
        ans = it.get("standard_answer")
        if ans:
            row["answer"] = ans
        # 显式声明不可作答的条目：原因原样保留（notice 优先，其次 unparseable_reason）
        if it.get("answerable") is False:
            row["answerable"] = False
            notice = str(it.get("notice") or it.get("unparseable_reason") or "").strip()
            if notice:
                row["notice"] = notice
        items_out.append(row)

    passages_out = []
    for p in env.get("passages") or []:
        row = {"id": p.get("id"), "paragraphs": p.get("paragraphs") or []}
        if p.get("label"):
            row["label"] = p["label"]
        if p.get("title"):
            row["title"] = p["title"]
        passages_out.append(row)

    # 旧信封里小节可能没有 part（条目却有）：从条目回填，让转换产物自洽
    part_by_section: dict[str, str] = {}
    for it in env.get("items") or []:
        if it.get("section_name") and it.get("part"):
            part_by_section.setdefault(str(it["section_name"]), str(it["part"]))
    sections_out = []
    for s in env.get("sections") or []:
        row = {"name": s.get("name") or s.get("part") or "未分节"}
        part = s.get("part") or part_by_section.get(str(row["name"]))
        if part:
            row["part"] = part
        for f in ("per", "full"):
            if s.get(f) is not None:
                row[f] = s[f]
        if s.get("count") is not None:
            row["count"] = s["count"]
        sections_out.append(row)

    out_meta = {
        "title": meta.get("title") or meta.get("subject") or "未命名题库",
        "subject": meta.get("subject") or "未命名",
        "specs": specs,
        "has_latex": has_latex,
        "id_prefix": id_prefix,
    }
    for f in ("paper_note", "known_text_layer_defects"):
        if meta.get(f):
            out_meta[f] = meta[f]
    return {"meta": out_meta, "sections": sections_out,
            "passages": passages_out, "items": items_out}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("inputs", nargs="+", help="旧 v2 信封题库路径")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "upload_ready"))
    ap.add_argument("--specs", nargs="+", help="判定 spec（未知卷必填），如 english / xingce / math math_multi")
    ap.add_argument("--id-prefix", help="轨迹 id 前缀（默认按对照表或 Q）")
    ap.add_argument("--has-latex", action="store_true", help="题干含 $…$ 行内公式")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = 0
    for src in args.inputs:
        p = Path(src)
        if not p.exists():
            print(f"FAIL  {p} 不存在")
            rc = 1
            continue
        env = json.loads(p.read_text(encoding="utf-8"))
        version = (env.get("meta") or {}).get("bank_version")
        if version != 2:
            print(f"FAIL  {p.name}: bank_version={version}，本工具只迁 v2 信封")
            rc = 1
            continue
        known = KNOWN.get(p.name)
        specs = args.specs or (known[2] if known else None)
        id_prefix = (args.id_prefix or (known[1] if known else "Q")).upper()
        has_latex = args.has_latex or bool(known[3]) if known else args.has_latex
        if not specs:
            print(f"FAIL  {p.name}: 未知卷，请用 --specs 指定判定 spec（可选：english / xingce / math math_multi）")
            rc = 1
            continue
        name = f"{known[0]}.json" if known else f"{p.stem}.json"
        out = convert(env, specs=specs, id_prefix=id_prefix, has_latex=has_latex)
        dst = out_dir / name
        dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

        # 如实打印：转了什么、放弃了什么
        vals = env.get("validations") or []
        sev = Counter(v.get("severity") for v in vals)
        ans = sum(1 for i in out["items"] if "answer" in i)
        una = sum(1 for i in out["items"] if i.get("answerable") is False)
        print(f"OK    {p.name} -> {dst}")
        print(f"      {len(out['items'])} 题（带答案 {ans}，显式不可作答 {una}）"
              f" · {len(out['passages'])} 篇章 · {len(out['sections'])} 小节"
              f" · specs={specs} id_prefix={id_prefix} has_latex={has_latex}")
        if vals:
            print(f"      旧库 {len(vals)} 条校验记录未迁移"
                  f"（{dict(sev)}）——那是提取过程的记录，新库由上传管线重新生成")
    if not rc:
        print(f"\n完成。到工作台顶栏「上传题库」逐个选择 {out_dir} 下的文件即可。")
        print("想让历史判定直接显示：把旧轨迹目录复制成 data/traces_<文件名>/，"
              "题图目录 data/math_items/ 也一并复制。")
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
