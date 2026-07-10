"""Backend history-fetch (scroll-back to 2010) — resample + window logic."""
import numpy as np
import pandas as pd
import pytest

import server as pop


def _make_1m(start_utc: str, n: int, base: float = 100.0) -> pd.DataFrame:
    ts = pd.date_range(pd.Timestamp(start_utc, tz="UTC"), periods=n, freq="1min")
    close = base + np.arange(n, dtype="float64")
    return pd.DataFrame({"ts": ts, "open": close - 0.5, "high": close + 0.5,
                         "low": close - 1.0, "close": close,
                         "volume": np.full(n, 10, dtype="int64")})


def test_resample_history_returns_bars_strictly_before_cutoff():
    df = _make_1m("2015-03-02 00:00", 600)          # 10 hours of 1m
    before = pd.Timestamp("2015-03-02 05:00", tz="UTC")
    out = pop._resample_history(df, bar_min=1, before_ts=before, n=1000)
    assert (out["ts"] < before).all()
    assert out["ts"].max() == pd.Timestamp("2015-03-02 04:59", tz="UTC")


def test_resample_history_caps_at_n_and_ascending():
    df = _make_1m("2015-03-02 00:00", 600)
    before = pd.Timestamp("2015-03-02 10:00", tz="UTC")
    out = pop._resample_history(df, bar_min=1, before_ts=before, n=50)
    assert len(out) == 50
    assert out["ts"].is_monotonic_increasing
    assert out["ts"].max() == pd.Timestamp("2015-03-02 09:59", tz="UTC")   # newest 50


def test_resample_history_aggregates_to_5m():
    df = _make_1m("2015-03-02 00:00", 60, base=100.0)   # 60 1m bars → 12 5m bars
    before = pd.Timestamp("2015-03-02 01:00", tz="UTC")
    out = pop._resample_history(df, bar_min=5, before_ts=before, n=1000)
    assert len(out) == 12
    # first 5m bar = 1m bars 0..4: open=99.5, high=104.5, low=99.0, close=104, vol=50
    first = out.iloc[0]
    assert first["open"] == pytest.approx(99.5)
    assert first["high"] == pytest.approx(104.5)
    assert first["close"] == pytest.approx(104.0)
    assert first["volume"] == 50


def test_resample_history_computes_moving_averages():
    # enough bars that ema20 (and the start) is populated — MAs must come back so
    # the lines continue across deep-history scroll-back.
    df = _make_1m("2015-03-02 00:00", 600, base=100.0)
    before = pd.Timestamp("2015-03-03 00:00", tz="UTC")
    out = pop._resample_history(df, bar_min=1, before_ts=before, n=300)
    assert "ema20" in out.columns and "sma200" in out.columns
    # the most recent returned bar (warmed by 600 bars) has a real ema20
    assert pd.notna(out["ema20"].iloc[-1])


def test_resample_history_empty_input_returns_empty():
    out = pop._resample_history(_make_1m("2015-01-01 00:00", 0), bar_min=1,
                               before_ts=pd.Timestamp("2015-01-01", tz="UTC"), n=10)
    assert len(out) == 0
