import asyncio
import os
import random
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class InferenceRequest(BaseModel):
    prompt: str = Field(min_length=1)
    max_tokens: int = Field(gt=0, le=4096)


class CapacityResponse(BaseModel):
    worker_id: str
    active_requests: int
    queued_tokens: int
    max_concurrent: int
    healthy: bool


class GenerateResponse(BaseModel):
    worker_id: str
    output: str
    latency_ms: int


class WorkerState:
    def __init__(self) -> None:
        self.active_requests = 0
        self.queued_tokens = 0
        self.lock = asyncio.Lock()


worker_id = os.getenv("WORKER_ID", "worker-local")
base_delay_ms = int(os.getenv("BASE_DELAY_MS", "250"))
jitter_ms = int(os.getenv("JITTER_MS", "150"))
max_concurrent = int(os.getenv("MAX_CONCURRENT", "8"))
state = WorkerState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(title="LLM Worker Simulator", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/capacity", response_model=CapacityResponse)
async def capacity() -> CapacityResponse:
    return CapacityResponse(
        worker_id=worker_id,
        active_requests=state.active_requests,
        queued_tokens=state.queued_tokens,
        max_concurrent=max_concurrent,
        healthy=state.active_requests < max_concurrent,
    )


@app.post("/generate", response_model=GenerateResponse)
async def generate(req: InferenceRequest) -> GenerateResponse:
    async with state.lock:
        if state.active_requests >= max_concurrent:
            raise HTTPException(status_code=429, detail="worker saturated")
        state.active_requests += 1
        token_cost = max(req.max_tokens, 1)
        state.queued_tokens += token_cost

    started = time.perf_counter()
    try:
        simulated_ms = compute_latency_ms(req.prompt, req.max_tokens)
        await asyncio.sleep(simulated_ms / 1000)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return GenerateResponse(
            worker_id=worker_id,
            output=(
                f"Simulated completion for prompt length {len(req.prompt)} "
                f"and max_tokens {req.max_tokens}."
            ),
            latency_ms=elapsed_ms,
        )
    finally:
        async with state.lock:
            state.active_requests -= 1
            state.queued_tokens = max(0, state.queued_tokens - max(req.max_tokens, 1))


def compute_latency_ms(prompt: str, max_tokens: int) -> int:
    prompt_cost = len(prompt) * 3
    generation_cost = max_tokens * 2
    jitter = random.randint(0, jitter_ms)
    return base_delay_ms + prompt_cost + generation_cost + jitter
