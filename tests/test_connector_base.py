import asyncio

import httpx
import pytest

from mdv.connectors import base
from mdv.connectors.base import (
    SingleEndpointConnector,
    required_text,
    strict_epoch_timestamp,
)
from mdv.connectors.registry import default_collection_connectors
from mdv.models import MarketSnapshot


class ExampleSingleEndpointConnector(SingleEndpointConnector[MarketSnapshot]):
    url = "https://example.test/markets"

    def parse(self, payload: object, *, observed_at: str) -> MarketSnapshot:
        assert payload == {"records": []}
        return MarketSnapshot("TEST", "TEST", "SPOT", "SPOT", observed_at, ())


def test_single_endpoint_connector_fetches_once_and_timestamps_parsing(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={"records": []})

    monkeypatch.setattr(base, "utc_now", lambda: "2026-01-01T00:00:00+00:00")

    async def run() -> MarketSnapshot:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ExampleSingleEndpointConnector().fetch(client)

    snapshot = asyncio.run(run())

    assert [str(request.url) for request in requests] == [
        "https://example.test/markets"
    ]
    assert snapshot.observed_at == "2026-01-01T00:00:00+00:00"


def test_uniform_single_endpoint_connector_classes_inherit_fetch():
    connector_types = {
        type(connector)
        for connector in default_collection_connectors()
        if isinstance(connector, SingleEndpointConnector)
    }

    assert len(connector_types) == 25
    assert all("fetch" not in connector_type.__dict__ for connector_type in connector_types)


def test_required_text_preserves_provider_text_and_record_category():
    assert required_text({"symbol": " MixedCase "}, "symbol", source="TEST") == "MixedCase"

    with pytest.raises(ValueError, match="TEST: instrument has no symbol"):
        required_text(
            {"symbol": " "},
            "symbol",
            source="TEST",
            record_kind="instrument",
        )


def test_strict_epoch_timestamp_supports_units_and_missing_value_policies():
    assert strict_epoch_timestamp(
        1,
        milliseconds=False,
        source="TEST",
        field="expiry",
        allow_missing=False,
    ) == "1970-01-01T00:00:01+00:00"
    assert strict_epoch_timestamp(
        1000,
        milliseconds=True,
        source="TEST",
        field="expiry",
        allow_missing=False,
    ) == "1970-01-01T00:00:01+00:00"
    assert (
        strict_epoch_timestamp(
            None,
            milliseconds=True,
            source="TEST",
            field="expiry",
            allow_missing=True,
        )
        is None
    )

    with pytest.raises(ValueError, match="TEST: missing expiry"):
        strict_epoch_timestamp(
            None,
            milliseconds=True,
            source="TEST",
            field="expiry",
            allow_missing=False,
        )
    with pytest.raises(ValueError, match="TEST: invalid expiry 'invalid'"):
        strict_epoch_timestamp(
            "invalid",
            milliseconds=True,
            source="TEST",
            field="expiry",
            allow_missing=True,
        )
