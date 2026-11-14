"""Performance and risk of the signed-in user's portfolio (FR-21, FR-22)."""

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from quant.api.deps import CurrentUser, UserDb
from quant.portfolio import performance, service

router = APIRouter(prefix="/api/performance", tags=["performance"])

# Returns and risk figures are fractions (0.12 = 12%). They are statistics, not money.


class SeriesPointOut(BaseModel):
    day: date
    portfolio: float  # index, 100 on the first day
    benchmark: float | None


class CorrelationOut(BaseModel):
    isins: list[str]
    names: list[str]
    values: list[list[float | None]]


class BenchmarkOut(BaseModel):
    code: str
    name: str


class PerformanceOut(BaseModel):
    benchmark: str
    benchmark_name: str
    benchmarks: list[BenchmarkOut]
    period: str
    has_history: bool
    start: date | None
    end: date | None
    current_value: Decimal
    net_invested: Decimal
    income: Decimal
    twr: float | None
    xirr: float | None
    benchmark_twr: float | None
    benchmark_xirr: float | None
    volatility: float | None
    benchmark_volatility: float | None
    max_drawdown: float | None
    drawdown_peak: date | None
    drawdown_low: date | None
    beta: float | None
    var95: float | None
    cvar95: float | None
    observations: int
    days: int
    estimated_days: int
    unpriced: list[str]
    estimated: list[str]
    series: list[SeriesPointOut]
    correlation: CorrelationOut | None
    warnings: list[str]


@router.get("", response_model=PerformanceOut)
def get_performance(
    user: CurrentUser,
    db: UserDb,
    benchmark: Literal["SXRV", "EUNL", "SXR8"] = "SXRV",
    period: Literal["all", "ytd", "1y", "3y"] = "all",
) -> PerformanceOut:
    result = performance.analyse(db, user.id, benchmark, period, service.today())
    cents = Decimal("0.01")
    return PerformanceOut(
        benchmark=result.benchmark,
        benchmark_name=result.benchmark_name,
        benchmarks=[BenchmarkOut(code=c, name=n) for c, n in performance.BENCHMARKS.items()],
        period=result.period,
        has_history=result.has_history,
        start=result.start,
        end=result.end,
        current_value=result.current_value.quantize(cents),
        net_invested=result.net_invested.quantize(cents),
        income=result.income.quantize(cents),
        twr=result.twr,
        xirr=result.xirr,
        benchmark_twr=result.benchmark_twr,
        benchmark_xirr=result.benchmark_xirr,
        volatility=result.volatility,
        benchmark_volatility=result.benchmark_volatility,
        max_drawdown=result.max_drawdown,
        drawdown_peak=result.drawdown_peak,
        drawdown_low=result.drawdown_low,
        beta=result.beta,
        var95=result.var95,
        cvar95=result.cvar95,
        observations=result.observations,
        days=result.days,
        estimated_days=result.estimated_days,
        unpriced=result.unpriced,
        estimated=result.estimated,
        series=[
            SeriesPointOut(day=p.day, portfolio=p.portfolio, benchmark=p.benchmark)
            for p in result.series
        ],
        correlation=(
            None
            if result.correlation is None
            else CorrelationOut(
                isins=result.correlation.isins,
                names=result.correlation.names,
                values=result.correlation.values,
            )
        ),
        warnings=result.warnings,
    )
