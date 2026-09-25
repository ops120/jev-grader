"""TypeSafe JEV (System One) HTTP 客户端。

契约（实测 2026-09，模型 jev-1.13.0）：
  POST https://api.typesafe.ai/v1/systemone
  Authorization: Bearer <API_KEY>
  {"state": <string|object>, "model": "jev-latest", "questions": {<id>: {...}}}
  -> {"model": ..., "answers": {<id>: {type, choice|noul|score, probabilities, confidence}}, "usage": {...}}

密钥只从 .env / 环境变量读取，仅存在于服务端。
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
RETRY_STATUS = {429, 500, 502, 503, 504, 529}


def load_key() -> str:
    key = os.environ.get("jevkey") or os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("jevkey="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("未找到 API Key：请在 .env 中配置 jevkey=...")


class JevError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"JEV {status}: {detail}")
        self.status = status
        self.detail = detail


class JevClient:
    """带指数退避的并发客户端。System One 单次请求可承载多个独立判断。"""

    def __init__(self, key: str | None = None, timeout: float = 90.0, max_retries: int = 4):
        self.key = key or load_key()
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "JevClient":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    async def ask(self, state: Any, questions: dict, model: str = MODEL) -> dict:
        if self._client is None:
            raise RuntimeError("请使用 async with JevClient() as c")
        body = {"state": state, "model": model, "questions": questions}
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                r = await self._client.post(ENDPOINT, json=body, headers=headers)
            except httpx.HTTPError as e:
                last = e
                if attempt >= self.max_retries:
                    raise
            else:
                if r.status_code == 200:
                    data = r.json()
                    data["_latency_ms"] = round((time.perf_counter() - t0) * 1000)
                    return data
                if r.status_code not in RETRY_STATUS:
                    raise JevError(r.status_code, r.text[:400])
                last = JevError(r.status_code, r.text[:200])
                if attempt >= self.max_retries:
                    raise last
                # 429/529 按官方建议指数退避
                ra = r.headers.get("retry-after")
                if ra and ra.isdigit():
                    delay = max(delay, float(ra))
            await asyncio.sleep(delay)
            delay *= 2
        raise last if last else RuntimeError("未知失败")


async def smoke() -> None:
    """最小连通性自检。"""
    async with JevClient() as c:
        out = await c.ask(
            "The city replaced downtown parking lots with parks. Traffic dropped 11%.",
            {"t": {"type": "noul", "instructions": "Does this passage report a traffic decrease?"}},
        )
    print(out["model"], out["answers"], out.get("usage"))


if __name__ == "__main__":
    asyncio.run(smoke())
