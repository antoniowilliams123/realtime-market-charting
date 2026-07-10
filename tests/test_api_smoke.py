"""Flask endpoint shape + validation smoke tests.

Importing ``server`` must be side-effect-free (no feed boot), so
these run without a live Databento connection.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _client():
    import server as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_init_bad_ticker_returns_400():
    c = _client()
    r = c.get("/api/init/AAPL/5m")
    assert r.status_code == 400
    body = r.get_json()
    assert "valid_tickers" in body and "valid_timeframes" in body


def test_init_bad_tf_returns_400():
    c = _client()
    r = c.get("/api/init/NQ/2m")
    assert r.status_code == 400
    body = r.get_json()
    assert "valid_tickers" in body and "valid_timeframes" in body


def test_init_unknown_ready_ticker_returns_503():
    # Valid ticker + tf, but no feed booted at import → not ready.
    c = _client()
    r = c.get("/api/init/NQ/1m")
    assert r.status_code == 503


def test_subscribe_ignores_bad_topics():
    c = _client()
    r = c.post("/api/subscribe", json={"client_id": "smoke1",
               "topics": [{"ticker": "AAPL", "tf": "5m"},
                          {"ticker": "NQ", "tf": "2m"}]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["client_id"] == "smoke1"
    assert body["topics"] == []   # both malformed/unknown topics dropped
