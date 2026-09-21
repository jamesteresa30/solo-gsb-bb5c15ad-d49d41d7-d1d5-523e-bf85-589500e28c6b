"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import RequestValidationErrors
from app.forecast import forecast
from app.models import (
    ForecastRequest,
    ForecastResponse,
    PlanResponse,
    SimulateRequest,
    SimulateResponse,
)
from app.planning import plan_initial_balances
from app.simulation import simulate

app = FastAPI(
    title="多层配额原子试算 API",
    description=(
        "接收若干令牌桶与带时间戳的请求序列，按时间顺序模拟连续补充；"
        "仅当请求桶链上所有桶余额充足时原子扣减，否则全部保持不变。"
        "另提供 /forecast：在不扣减状态的前提下，预测桶链首次同时满足成本所需的等待时间；"
        "以及 /plan-initial-balances：反推使全部请求放行所需的各桶最低初始余额。"
    ),
    version="1.1.0",
)


@app.exception_handler(RequestValidationErrors)
async def cross_validation_exception_handler(request: Request, exc: RequestValidationErrors):
    return JSONResponse(status_code=422, content={"detail": exc.errors})


@app.exception_handler(RequestValidationError)
async def pydantic_validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/", tags=["meta"])
def root():
    return {
        "service": app.title,
        "docs": "/docs",
        "health": "/health",
        "simulate": "/simulate",
        "forecast": "/forecast",
        "plan-initial-balances": "/plan-initial-balances",
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post("/simulate", response_model=SimulateResponse, tags=["simulation"])
def simulate_endpoint(payload: SimulateRequest) -> SimulateResponse:
    return simulate(payload)


@app.post("/forecast", response_model=ForecastResponse, tags=["forecast"])
def forecast_endpoint(payload: ForecastRequest) -> ForecastResponse:
    return forecast(payload)


@app.post(
    "/plan-initial-balances",
    response_model=PlanResponse,
    tags=["planning"],
)
def plan_initial_balances_endpoint(payload: SimulateRequest) -> PlanResponse:
    return plan_initial_balances(payload)
