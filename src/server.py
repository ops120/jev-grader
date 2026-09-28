"""FastAPI 服务：托管三栏前端，并提供题库上传、试卷、判定轨迹与求解接口。

题库不再由本仓库提取（提取功能已抽离）。用户按 ``templates/`` 的模板制作题库
JSON：通过 ``POST /api/upload`` 上传（结构验收不过就整体拒绝，逐条给出原因），
或直接放进 ``data/banks/``。密钥只在服务端使用，前端从不接触 API Key。
启动: python src/server.py   然后打开 http://127.0.0.1:8848
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import papers  # noqa: E402
import bank  # noqa: E402
import upload as upload_mod  # noqa: E402
from judge import spec as spec_mod  # noqa: E402
from jev_client import JevClient  # noqa: E402
from solver import load_exam, solve_one  # noqa: E402

WEB = ROOT / "web"
TEMPLATE = ROOT / "templates" / "bank_template.json"
# 默认只监听本机：这是个没有鉴权的本机工具（见 SECURITY.md）。端口可用环境变量覆盖。
HOST = os.environ.get("JEV_GRADER_HOST", "127.0.0.1")
PORT = int(os.environ.get("JEV_GRADER_PORT", "8848"))

app = FastAPI(title="JEV 智能改卷系统")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/api/template")
def api_template() -> FileResponse:
    """题库模板文件（随仓库分发，内容为原创示例）。界面的「下载模板」指向这里。"""
    if not TEMPLATE.exists():
        raise HTTPException(404, "模板文件缺失：templates/bank_template.json")
    return FileResponse(TEMPLATE, filename="bank_template.json",
                        media_type="application/json")


@app.post("/api/upload")
def api_upload(payload: dict) -> JSONResponse:
    """上传题库：结构验收 → 通过才落盘到 data/banks/。error 级问题整体拒绝。"""
    name = str(payload.get("name") or "uploaded.json")
    raw = payload.get("bank")
    if not isinstance(raw, dict):
        raise HTTPException(400, "请求体需为 {name, bank}，bank 是按模板填写的题库 JSON 对象")
    bank_obj, errors = upload_mod.ingest(raw, filename=name)
    if bank_obj is None:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)
    pid = upload_mod.paper_id_from_filename(name)
    path = papers.BANKS_DIR / f"{pid}.json"
    replaced = path.exists()
    bank.write_bank(bank_obj, path)
    m = bank_obj["meta"]
    return JSONResponse({
        "ok": True, "paper": pid, "replaced": replaced,
        "title": m["title"], "total_items": m["total_items"], "answerable": m["answerable"],
        "warnings": [bank.render_validation(v) for v in bank_obj["validations"]
                     if v["severity"] == "warn"],
    })


@app.get("/api/papers")
def api_papers() -> JSONResponse:
    return JSONResponse(papers.listing())


def _resolve_paper(paper: str | None) -> dict:
    try:
        cfg = papers.get(paper or None)
    except KeyError:
        if not papers.default_id():
            raise HTTPException(
                404, "还没有任何题库：请按 templates/ 模板制作题库并上传（顶栏「上传题库」），"
                     "或把题库 JSON 放进 data/banks/") from None
        raise HTTPException(404, f"没有这份试卷：{paper}") from None
    return cfg


def _check_specs(cfg: dict) -> None:
    """spec 配置错要在使用前报出来，而不是让一道题悄悄套错编排。"""
    for _n in cfg["specs"]:
        spec_mod.get(_n)


@app.get("/api/exam")
def api_exam(paper: str = "") -> JSONResponse:
    cfg = _resolve_paper(paper or None)
    if not cfg["exam"].exists():
        raise HTTPException(503, f"缺少 {cfg['exam'].name}，请重新上传题库")
    try:
        data = bank.read_bank(cfg["exam"])
    except bank.BankVersionError as e:
        # 旧版题库把缺损题直接删掉了，界面若照旧渲染，显示的就是一份"看起来完整"
        # 实则不知道丢了什么的卷子——宁可直接拒绝并说明怎么重生成。
        raise HTTPException(409, str(e)) from None
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"{cfg['exam'].name} 不是合法 JSON：{e}") from None
    try:
        _check_specs(cfg)
    except spec_mod.SpecError as e:
        raise HTTPException(500, f"题库声明的判定 spec 配置有误：{e}") from None
    return JSONResponse(data)


@app.get("/api/run")
def api_run(paper: str = "") -> JSONResponse:
    """本次（最近一次）求解的墙钟信息，用于展示全卷实际耗时。"""
    cfg = _resolve_paper(paper or None)
    f = cfg["traces"] / "_run.json"
    if not f.exists():
        return JSONResponse({})
    try:
        return JSONResponse(json.loads(f.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return JSONResponse({})


@app.get("/api/traces")
def api_traces(paper: str = "") -> JSONResponse:
    cfg = _resolve_paper(paper or None)
    out = {}
    if cfg["traces"].exists():
        for f in sorted(cfg["traces"].glob("*.json")):
            try:
                out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
    return JSONResponse(out)


@app.post("/api/solve")
async def api_solve(payload: dict) -> JSONResponse:
    paper = payload.get("paper") or None
    cfg = _resolve_paper(paper)
    exam = load_exam(paper)
    try:
        _check_specs(cfg)
    except spec_mod.SpecError as e:
        raise HTTPException(500, f"判定 spec 配置有误，未发起任何请求：{e}") from None
    by_id = {p["id"]: p for p in exam.get("passages", [])}
    nos = payload.get("nos")
    todo = [i for i in exam["items"] if i["answerable"]]
    if nos:
        want = {int(n) for n in nos}
        todo = [i for i in todo if i["no"] in want]
    if not todo:
        raise HTTPException(400, "没有可求解的题目")

    sem = asyncio.Semaphore(int(payload.get("concurrency", 4)))
    force = bool(payload.get("force", True))
    try:
        async with JevClient() as client:
            results = list(await asyncio.gather(
                *(solve_one(client, it, by_id.get(it.get("passage_id")), sem, force, cfg)
                  for it in todo)))
    except RuntimeError as e:
        # 客户端初始化失败（最常见：.env 里没配 jevkey）。未发起任何请求，如实说明。
        raise HTTPException(500, str(e)) from None

    traces = {}
    for r in results:
        f = cfg["traces"] / f"{r['id']}.json"
        if f.exists():
            traces[r["id"]] = json.loads(f.read_text(encoding="utf-8"))
    return JSONResponse({"results": results, "traces": traces})


@app.post("/api/clear")
def api_clear(payload: dict) -> JSONResponse:
    """删除已求解的判定轨迹。不带 nos 则清空该卷。"""
    cfg = _resolve_paper(payload.get("paper") or None)
    nos = payload.get("nos")
    if nos:
        targets = [cfg["traces"] / f"Q{int(n)}.json" for n in nos] + \
                  [cfg["traces"] / f"M{int(n)}.json" for n in nos]
    else:
        targets = sorted(cfg["traces"].glob("*.json"))
    removed: list[str] = []
    for f in targets:
        if f.exists():
            f.unlink()
            removed.append(f.stem)
    return JSONResponse({"removed": removed})


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
# 数学卷条目里的 images 引用 /data/math_items/*.png（试卷原始排版，公式保真用）。
# check_dir=False：data/ 不存在时服务照常启动（首次运行的空态是引导页，不是错误）。
app.mount("/data", StaticFiles(directory=str(ROOT / "data"), check_dir=False), name="data")


if __name__ == "__main__":
    import uvicorn

    print(f"打开 http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
