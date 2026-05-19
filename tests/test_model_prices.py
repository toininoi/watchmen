import pytest

from watchmen import model_prices


def test_parse_pricing_normalizes_openrouter_per_token_strings_to_per_million():
    pricing = model_prices._parse_pricing({
        "prompt": "0.000003",
        "completion": "0.000015",
        "cache_creation_input": "0.00000375",
        "cache_creation_input_1h": "0.000006",
        "cache_read_input": "0.0000003",
    })

    assert pricing is not None
    assert pricing.as_tuple() == pytest.approx((3.0, 3.75, 6.0, 0.3, 15.0))


def test_parse_pricing_normalizes_default_cache_prices_to_per_million():
    pricing = model_prices._parse_pricing({
        "prompt": "0.000001",
        "completion": "0.000005",
    })

    assert pricing is not None
    assert pricing.as_tuple() == pytest.approx((1.0, 1.0, 1.0, 0.1, 5.0))


def test_price_for_model_uses_api_backed_prices_in_per_million_units(monkeypatch):
    parsed = model_prices._parse_pricing({
        "prompt": "0.000003",
        "completion": "0.000015",
        "cache_creation_input": "0.00000375",
        "cache_creation_input_1h": "0.000006",
        "cache_read_input": "0.0000003",
    })
    assert parsed is not None

    db = model_prices.PriceDatabase(prices={"deepseek-v4-flash": parsed})
    monkeypatch.setattr(model_prices, "get_price_database", lambda: db)

    assert model_prices.price_for_model("openrouter/deepseek-v4-flash") == pytest.approx(
        (3.0, 3.75, 6.0, 0.3, 15.0)
    )


def test_turn_cost_usd_uses_api_backed_prices_in_per_million_units(monkeypatch):
    parsed = model_prices._parse_pricing({
        "prompt": "0.000003",
        "completion": "0.000015",
        "cache_creation_input": "0.00000375",
        "cache_creation_input_1h": "0.000006",
        "cache_read_input": "0.0000003",
    })
    assert parsed is not None

    db = model_prices.PriceDatabase(prices={"deepseek-v4-flash": parsed})
    monkeypatch.setattr(model_prices, "get_price_database", lambda: db)

    assert model_prices.turn_cost_usd(
        "deepseek-v4-flash",
        input_tokens=1_000_000,
        cache_creation_5m=1_000_000,
        cache_creation_1h=1_000_000,
        cache_read=1_000_000,
        output_tokens=1_000_000,
    ) == pytest.approx(28.05)


def test_hardcoded_price_math_does_not_hit_price_database(monkeypatch):
    def fail_if_called():
        pytest.fail("hardcoded model pricing should not fetch cache or API")

    monkeypatch.setattr(model_prices, "get_price_database", fail_if_called)

    assert model_prices.turn_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=1_000_000,
        cache_creation_5m=0,
        cache_creation_1h=0,
        cache_read=0,
        output_tokens=1_000_000,
    ) == pytest.approx(18.0)


def test_api_fetch_uses_short_timeout_for_cold_cache_path(monkeypatch):
    timeouts = []

    class SlowClient:
        def __init__(self, timeout):
            timeouts.append(timeout)

        def __enter__(self):
            raise TimeoutError("slow OpenRouter response")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(model_prices, "_get_api_key", lambda: "sk-or-test")
    monkeypatch.setattr(model_prices.httpx, "Client", SlowClient)

    assert model_prices._fetch_from_api() is None
    assert timeouts
    assert timeouts[0] < 30.0
