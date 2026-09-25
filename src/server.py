"""FastAPI 服务：托管三栏前端，并提供试卷、判定轨迹与求解接口。

支持多份试卷（见 src/papers.py）。密钥只在服务端使用，前端从不接触 API Key。
启动: python src/server.py   然后打开 http://127.0.0.1:8848
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import papers  # noqa: E402
import bank  # noqa: E402
from judge import spec as spec_mod  # noqa: E402
from jev_client import JevClient  # noqa: E402
from solver import load_exam, solve_one  # noqa: E402

WEB = ROOT / "web"
HOST, PORT = "127.0.0.1", 8848

app = FastAPI(title="JEV 智能改卷系统")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/api/papers")
def api_papers() -> JSONResponse:
    return JSONResponse(papers.listing())


@app.get("/api/exam")
def api_exam(paper: str = papers.DEFAULT) -> JSONResponse:
    try:
        cfg = papers.get(paper)
    except KeyError:
        raise HTTPException(404, f"没有这份试卷：{paper}")
    if not cfg["exam"].exists():
        raise HTTPException(503, f"缺少 {cfg['exam'].name}，请先运行对应的解析脚本")
    try:
        data = bank.read_bank(cfg["exam"])
    except bank.BankVersionError as e:
        # 旧版题库把缺损题直接删掉了，界面若照旧渲染，显示的就是一份"看起来完整"
        # 实则不知道丢了什么的卷子——宁可直接拒绝并说明怎么重生成。
        raise HTTPException(409, str(e)) from None
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"{cfg['exam'].name} 不是合法 JSON：{e}") from None
    return JSONResponse(data)


@app.get("/api/run")
def api_run(paper: str = papers.DEFAULT) -> JSONResponse:
    """本次（最近一次）求解的墙钟信息，用于展示全卷实际耗时。"""
    try:
        cfg = papers.get(paper)
    except KeyError:
        raise HTTPException(404, f"没有这份试卷：{paper}")
    f = cfg["traces"] / "_run.json"
    if not f.exists():
        return JSONResponse({})
    try:
        return JSONResponse(json.loads(f.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return JSONResponse({})


@app.get("/api/traces")
def api_traces(paper: str = papers.DEFAULT) -> JSONResponse:
    try:
        cfg = papers.get(paper)
    except KeyError:
        raise HTTPException(404, f"没有这份试卷：{paper}")
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
    paper = payload.get("paper") or papers.DEFAULT
    try:
        cfg = papers.get(paper)
    except KeyError:
        raise HTTPException(404, f"没有这份试卷：{paper}")
    exam = load_exam(paper)
    try:
        for _n in cfg["specs"]:
            spec_mod.get(_n)
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
    async with JevClient() as client:
        results = list(await asyncio.gather(
            *(solve_one(client, it, by_id.get(it.get("passage_id")), sem, force, cfg)
              for it in todo)))

    traces = {}
    for r in results:
        f = cfg["traces"] / f"{r['id']}.json"
        if f.exists():
            traces[r["id"]] = json.loads(f.read_text(encoding="utf-8"))
    return JSONResponse({"results": results, "traces": traces})


@app.post("/api/clear")
def api_clear(payload: dict) -> JSONResponse:
    """删除已求解的判定轨迹。不带 nos 则清空该卷。"""
    try:
        cfg = papers.get(payload.get("paper") or papers.DEFAULT)
    except KeyError:
        raise HTTPException(404, "没有这份试卷")
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


if __name__ == "__main__":
    import uvicorn

    print(f"打开 http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
