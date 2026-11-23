"""FastAPI application."""

from fastapi import FastAPI
from sqlalchemy import text

from quant.api import (
    admin,
    ai,
    allocation,
    auth,
    book,
    cash,
    concentration,
    costs,
    crypto,
    delivery,
    holdings,
    imports,
    market,
    markets,
    notifications,
    passkeys,
    performance,
    prices,
    scores,
    tax,
    transactions,
)
from quant.api.deps import DbSession

app = FastAPI(
    title="QuantTrading", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json"
)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(market.router)
app.include_router(imports.router)
app.include_router(transactions.router)
app.include_router(holdings.router)
app.include_router(tax.router)
app.include_router(costs.router)
app.include_router(prices.router)
app.include_router(notifications.router)
app.include_router(ai.router)
app.include_router(performance.router)
app.include_router(cash.router)
app.include_router(concentration.router)
app.include_router(allocation.router)
app.include_router(book.router)
app.include_router(crypto.router)
app.include_router(markets.router)
app.include_router(scores.router)
app.include_router(passkeys.router)
app.include_router(delivery.router)


@app.get("/api/health")
def health(db: DbSession) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
