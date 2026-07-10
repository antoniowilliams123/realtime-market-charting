from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import local_history as lh


# ---------------------------------------------------------------------------
# helpers: build synthetic monthly .csv.gz partitions in the archive layout
#   {root}/{ticker}_1m/{ticker}_1m_{YYYY-MM}.csv.gz   cols: ts_utc,open,high,low,close,volume
# ---------------------------------------------------------------------------
def _write_month(root: Path, ticker: str, month: str, ts_start: str, n: int,
                 base: float = 20000.0, suffix: str = ".csv.gz",
                 caps: bool = False) -> pd.DataFrame:
    d = root / f"{ticker}_1m"
    d.mkdir(parents=True, exist_ok=True)
    ts = pd.date_range(pd.Timestamp(ts_start, tz="UTC"), periods=n, freq="1min")
    close = base + np.arange(n, dtype="float64")
    df = pd.DataFrame({
        "ts_utc": ts.astype(str),
        "open": close - 0.5, "high": close + 0.5, "low": close - 1.0,
        "close": close, "volume": np.full(n, 100, dtype="int64"),
    })
    # Some archive months were written with capitalized OHLCV headers
    # (Open,High,Low,Close,Volume) — load_range must normalize them.
    if caps:
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
    df.to_csv(d / f"{ticker}_1m_{month}{suffix}", index=False, compression="gzip")
    return df


def test_load_range_single_month_returns_ohlcv_with_utc_ts(tmp_path):
    _write_month(tmp_path, "NQ", "2010-07", "2010-07-01 00:00:00", 60)
    out = lh.load_range("NQ", pd.Timestamp("2010-07-01 00:00", tz="UTC"),
                        pd.Timestamp("2010-07-01 01:00", tz="UTC"), root=tmp_path)
    assert list(out.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert str(out["ts"].dt.tz) == "UTC"
    assert len(out) == 60          # 00:00..00:59 inclusive
    assert out["close"].iloc[0] == pytest.approx(20000.0)


def test_load_range_spans_two_months_sorted_and_concatenated(tmp_path):
    _write_month(tmp_path, "NQ", "2010-07", "2010-07-31 23:30:00", 30, base=100.0)
    _write_month(tmp_path, "NQ", "2010-08", "2010-08-01 00:00:00", 30, base=200.0)
    out = lh.load_range("NQ", pd.Timestamp("2010-07-31 23:00", tz="UTC"),
                        pd.Timestamp("2010-08-01 00:30", tz="UTC"), root=tmp_path)
    assert len(out) == 60
    assert out["ts"].is_monotonic_increasing
    assert out["ts"].iloc[0] == pd.Timestamp("2010-07-31 23:30", tz="UTC")
    assert out["ts"].iloc[-1] == pd.Timestamp("2010-08-01 00:29", tz="UTC")


def test_load_range_filters_to_window_inclusive(tmp_path):
    _write_month(tmp_path, "NQ", "2010-07", "2010-07-01 00:00:00", 120)
    out = lh.load_range("NQ", pd.Timestamp("2010-07-01 00:10", tz="UTC"),
                        pd.Timestamp("2010-07-01 00:20", tz="UTC"), root=tmp_path)
    assert out["ts"].iloc[0] == pd.Timestamp("2010-07-01 00:10", tz="UTC")
    assert out["ts"].iloc[-1] == pd.Timestamp("2010-07-01 00:20", tz="UTC")
    assert len(out) == 11


def test_load_range_ignores_non_standard_suffix_files(tmp_path):
    # a .bak_wk partition must NOT be read (would duplicate / inject stale bars)
    _write_month(tmp_path, "NQ", "2010-07", "2010-07-01 00:00:00", 10, base=1.0)
    _write_month(tmp_path, "NQ", "2010-07", "2010-07-01 00:00:00", 10, base=999.0,
                 suffix=".csv.gz.bak_wk")
    out = lh.load_range("NQ", pd.Timestamp("2010-07-01 00:00", tz="UTC"),
                        pd.Timestamp("2010-07-01 00:09", tz="UTC"), root=tmp_path)
    assert len(out) == 10                      # only the clean file, no dupes
    assert out["close"].iloc[0] == pytest.approx(1.0)   # not the .bak_wk values


def test_load_range_normalizes_capitalized_headers(tmp_path):
    # ES/YM/CL/MBT 2026-04 archive files use Open,High,Low,Close,Volume.
    _write_month(tmp_path, "ES", "2026-04", "2026-04-08 00:00:00", 60,
                 base=5000.0, caps=True)
    out = lh.load_range("ES", pd.Timestamp("2026-04-08 00:00", tz="UTC"),
                        pd.Timestamp("2026-04-08 00:59", tz="UTC"), root=tmp_path)
    assert list(out.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert len(out) == 60
    assert out["close"].iloc[0] == pytest.approx(5000.0)


def test_load_range_mixed_casing_across_months(tmp_path):
    # lowercase month + capitalized month must concatenate cleanly
    _write_month(tmp_path, "ES", "2026-04", "2026-04-30 23:30:00", 30, base=100.0, caps=True)
    _write_month(tmp_path, "ES", "2026-05", "2026-05-01 00:00:00", 30, base=200.0)
    out = lh.load_range("ES", pd.Timestamp("2026-04-30 23:00", tz="UTC"),
                        pd.Timestamp("2026-05-01 00:30", tz="UTC"), root=tmp_path)
    assert len(out) == 60
    assert out["ts"].is_monotonic_increasing
    assert not out["close"].isna().any()


def test_load_range_missing_ticker_returns_empty_typed_frame(tmp_path):
    out = lh.load_range("ZZ", pd.Timestamp("2010-07-01", tz="UTC"),
                        pd.Timestamp("2010-07-02", tz="UTC"), root=tmp_path)
    assert list(out.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert len(out) == 0


def test_last_ts_returns_latest_local_bar(tmp_path):
    _write_month(tmp_path, "NQ", "2010-07", "2010-07-01 00:00:00", 30)
    _write_month(tmp_path, "NQ", "2010-08", "2010-08-01 00:00:00", 45)
    assert lh.last_ts("NQ", root=tmp_path) == pd.Timestamp("2010-08-01 00:44", tz="UTC")


def test_last_ts_missing_returns_none(tmp_path):
    assert lh.last_ts("ZZ", root=tmp_path) is None


# ---------------------------------------------------------------------------
# reconcile(local_df, db_df): merge the local seed with the Databento gap fetch,
# but only trust local if the overlapping bars match (provenance safety net).
# ---------------------------------------------------------------------------
def _frame(ts_start: str, n: int, base: float):
    ts = pd.date_range(pd.Timestamp(ts_start, tz="UTC"), periods=n, freq="1min")
    close = base + np.arange(n, dtype="float64")
    return pd.DataFrame({"ts": ts, "open": close - 0.5, "high": close + 0.5,
                         "low": close - 1.0, "close": close,
                         "volume": np.full(n, 100, dtype="int64")})


def test_reconcile_empty_local_returns_db_untrusted():
    db = _frame("2026-06-01 00:00", 30, 20000.0)
    out, used_local = lh.reconcile(lh._empty(), db)
    assert used_local is False
    assert len(out) == 30


def test_reconcile_matching_overlap_merges_and_trusts_local():
    # local covers 00:00..00:59, db covers 00:50..01:19 (10-min overlap, same data)
    local = _frame("2026-06-01 00:00", 60, 20000.0)
    db = _frame("2026-06-01 00:50", 30, 20050.0)   # continues the same series
    out, used_local = lh.reconcile(local, db)
    assert used_local is True
    assert out["ts"].iloc[0] == pd.Timestamp("2026-06-01 00:00", tz="UTC")
    assert out["ts"].iloc[-1] == pd.Timestamp("2026-06-01 01:19", tz="UTC")
    assert out["ts"].is_monotonic_increasing
    assert not out["ts"].duplicated().any()


def test_reconcile_mismatched_overlap_discards_local():
    # db reports a wholly different price in the overlap → local provenance is wrong
    local = _frame("2026-06-01 00:00", 60, 20000.0)
    db = _frame("2026-06-01 00:50", 30, 99000.0)
    out, used_local = lh.reconcile(local, db)
    assert used_local is False
    assert out["ts"].iloc[0] == pd.Timestamp("2026-06-01 00:50", tz="UTC")   # db only
    assert len(out) == 30


def test_reconcile_no_overlap_does_not_trust_local():
    local = _frame("2026-06-01 00:00", 30, 20000.0)
    db = _frame("2026-06-01 05:00", 30, 20300.0)   # gap, no shared timestamps
    out, used_local = lh.reconcile(local, db)
    assert used_local is False
    assert len(out) == 30


# ---------------------------------------------------------------------------
# missing_spans(df, max_gap): find holes in a 1m series bigger than a normal
# market break, so the caller can backfill them from Databento (the local
# archive sometimes has a truncated/incomplete month).
# ---------------------------------------------------------------------------
def test_missing_spans_contiguous_has_none():
    df = _frame("2026-06-01 00:00", 200, 20000.0)
    assert lh.missing_spans(df, max_gap=pd.Timedelta(days=3)) == []


def test_missing_spans_skips_normal_weekend_gap():
    # Fri close → Sun open ≈ 49h: below a 3-day threshold, not a hole.
    a = _frame("2026-05-29 20:00", 60, 20000.0)     # Fri ... 20:59 UTC
    b = _frame("2026-05-31 22:00", 60, 20100.0)     # Sun 22:00 UTC reopen (~49h later)
    df = pd.concat([a, b], ignore_index=True)
    assert lh.missing_spans(df, max_gap=pd.Timedelta(days=3)) == []


def test_missing_spans_flags_multiday_hole_with_endpoints():
    a = _frame("2026-04-24 00:00", 60, 20000.0)     # ends 2026-04-24 00:59
    b = _frame("2026-05-01 00:00", 60, 20100.0)     # resumes ~6 days later
    df = pd.concat([a, b], ignore_index=True)
    spans = lh.missing_spans(df, max_gap=pd.Timedelta(days=3))
    assert len(spans) == 1
    gs, ge = spans[0]
    assert gs == pd.Timestamp("2026-04-24 00:59", tz="UTC")   # last bar before hole
    assert ge == pd.Timestamp("2026-05-01 00:00", tz="UTC")   # first bar after hole


def test_missing_spans_multiple_holes():
    a = _frame("2026-04-01 00:00", 30, 1.0)
    b = _frame("2026-04-10 00:00", 30, 2.0)
    c = _frame("2026-04-20 00:00", 30, 3.0)
    df = pd.concat([a, b, c], ignore_index=True)
    spans = lh.missing_spans(df, max_gap=pd.Timedelta(days=3))
    assert len(spans) == 2
