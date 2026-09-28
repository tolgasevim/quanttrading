"""ECB euro foreign exchange reference rates via the ECB Data Portal SDMX API (FR-25)."""

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from quant.providers.base import Fetcher, FxObservation, ProviderError

DATA_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.{quotes}.EUR.SP00.A"


class EcbProvider:
    name = "ecb"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    def fetch_rates(self, quotes: list[str], start: date, end: date) -> list[FxObservation]:
        body = self._fetcher.get_text(
            self.name,
            DATA_URL.format(quotes="+".join(sorted(quotes))),
            {"startPeriod": start.isoformat(), "endPeriod": end.isoformat(), "format": "csvdata"},
        )
        return parse_csv(body)


def parse_csv(body: str) -> list[FxObservation]:
    text = body.strip()
    if not text:  # the API answers an empty body when the range has no observations
        return []
    reader = csv.DictReader(io.StringIO(text))
    required = {"CURRENCY", "TIME_PERIOD", "OBS_VALUE"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        raise ProviderError(f"ecb: unexpected payload: {text[:80]!r}")
    observations = []
    for row in reader:
        if not row["OBS_VALUE"]:
            continue
        try:
            observations.append(
                FxObservation(
                    quote=row["CURRENCY"],
                    date=date.fromisoformat(row["TIME_PERIOD"]),
                    rate=Decimal(row["OBS_VALUE"]),
                )
            )
        except (ValueError, InvalidOperation) as exc:
            raise ProviderError(f"ecb: malformed row: {exc}") from exc
    return observations
