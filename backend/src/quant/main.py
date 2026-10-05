"""FastAPI application."""

from fastapi import FastAPI
from sqlalchemy import text

from quant.api import admin, auth, holdings, imports, market, tax, transactions
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


@app.get("/api/health")
def health(db: DbSession) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
