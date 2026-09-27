"""Load the instrument seed list (YAML) into the database, upserting by code."""

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.models import Instrument

REQUIRED = {"code", "name", "asset_class", "currency", "symbols"}


def load_instruments(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text())
    items: list[dict[str, Any]] = data.get("instruments", []) if data else []
    for item in items:
        missing = REQUIRED - item.keys()
        if missing:
            raise ValueError(f"instrument {item.get('code', '?')} is missing {sorted(missing)}")
    return items


def seed_instruments(session: Session, items: list[dict[str, Any]]) -> int:
    existing = {i.code: i for i in session.scalars(select(Instrument))}
    for item in items:
        instrument = existing.get(item["code"]) or Instrument(code=item["code"])
        instrument.isin = item.get("isin")
        instrument.name = item["name"]
        instrument.asset_class = item["asset_class"]
        instrument.currency = item["currency"]
        instrument.symbols = dict(item["symbols"])
        instrument.active = item.get("active", True)
        session.add(instrument)
    session.commit()
    return len(items)
