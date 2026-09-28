"""门禁共用的题库目标装载：动态发现的试卷 + 随仓库分发的模板卷。

题库不再随仓库分发，所以门禁必须区分「有没有目标」与「目标坏没坏」：
- 本机 ``data/banks/``（及旧版迁移路径）里发现的题库逐个列出；读取失败的
  把原因带出来，**不静默略过**；
- 模板卷 ``templates/bank_template.json``（原创演示题）永远存在，走
  ``upload.ingest`` 得到信封，因此 golden_compose 与 test_cache_identity
  在没有任何本地题库的机器上也有真实目标，而不是集体 SKIP；
- golden_replay 与 test_solver_replay 需要历史轨迹，没有就是 SKIP（如实说明）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import bank  # noqa: E402
import papers  # noqa: E402
import upload  # noqa: E402

TEMPLATE = ROOT / "templates" / "bank_template.json"


def template_envelope() -> dict:
    """模板卷走一遍生产上传管线（模板与管线必须始终对得上）。"""
    raw = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    env, errors = upload.ingest(raw, filename="bank_template.json")
    if env is None:
        raise RuntimeError("模板卷没通过上传管线："
                           + "；".join(e.get("message", "") for e in errors))
    return env


def template_target() -> dict:
    env = template_envelope()
    return {"id": "__template__", "specs": tuple(env["meta"]["specs"]), "envelope": env,
            "traces": None, "local": False, "error": None}


def discovered_targets() -> list[dict]:
    """本机题库：[{id, specs, envelope, traces, local, error}]，error 非空即读不出。"""
    out: list[dict] = []
    for pid, cfg in sorted(papers.discover().items()):
        entry = {"id": pid, "specs": cfg["specs"], "envelope": None,
                 "traces": cfg["traces"], "local": True, "error": cfg["load_error"]}
        if entry["error"] is None and not cfg["specs"]:
            entry["error"] = "meta.specs 缺失"
        if entry["error"] is None:
            try:
                entry["envelope"] = bank.read_bank(cfg["exam"])
            except Exception as e:  # noqa: BLE001 — 坏题库要带原因出现在门禁里
                entry["error"] = f"{type(e).__name__}: {e}"
        out.append(entry)
    return out


def all_targets(*, include_template: bool = True) -> list[dict]:
    targets = discovered_targets()
    if include_template:
        targets.append(template_target())
    return targets
