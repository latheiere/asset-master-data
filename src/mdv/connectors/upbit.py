from __future__ import annotations

from mdv.connectors.base import SingleEndpointConnector, required_text
from mdv.models import MarketRecord, MarketSnapshot


class UpbitSpotConnector(SingleEndpointConnector[MarketSnapshot]):
    source = "UPBIT_SPOT"
    venue = "UPBIT"
    market_type = "SPOT"
    product = "SPOT"
    url = "https://api.upbit.com/v1/market/all?is_details=true"

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        if not isinstance(payload, list):
            raise ValueError(f"{self.source}: response is not an array")

        markets = []
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{self.source}: market is not an object")
            raw_symbol = required_text(
                row, "market", source=self.source, record_kind="market"
            ).upper()
            components = raw_symbol.split("-")
            if len(components) != 2 or not all(components):
                raise ValueError(
                    f"{self.source}: invalid quote-base market {raw_symbol!r}"
                )
            quote_symbol, base_symbol = components
            markets.append(
                MarketRecord(
                    self.source,
                    self.venue,
                    self.market_type,
                    self.product,
                    raw_symbol,
                    base_symbol,
                    quote_symbol,
                    None,
                    "SPOT",
                    "TRADING",
                    True,
                    None,
                    dict(row),
                    venue_product=self.product,
                    venue_status="AVAILABLE",
                )
            )

        snapshot = MarketSnapshot(
            self.source,
            self.venue,
            self.market_type,
            self.product,
            observed_at,
            tuple(markets),
        )
        snapshot.validate()
        return snapshot


def upbit_connectors() -> list[UpbitSpotConnector]:
    return [UpbitSpotConnector()]
