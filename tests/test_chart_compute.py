import numpy as np
import pandas as pd
import pytest

import chart_compute as cc
from tests.conftest import make_1m, ET


# ---------------------------------------------------------------------------
# 1) aggregate
# ---------------------------------------------------------------------------
def test_aggregate_5m_from_15_1m_bars():
    df = make_1m("2026-05-04 09:30", 15, base=20000.0, step=1.0)
    out = cc.aggregate(df, 5)
    assert len(out) == 3
    # first 5m bar: bars 0..4 (close = 20000..20004)
    first = out.iloc[0]
    assert first["open"] == pytest.approx(20000.0 - 0.5)   # open of bar 0
    assert first["high"] == pytest.approx(20004.0 + 0.5)   # max high
    assert first["low"] == pytest.approx(20000.0 - 1.0)    # min low (bar 0)
    assert first["close"] == pytest.approx(20004.0)        # close of bar 4
    assert first["volume"] == 500
    # second 5m bar: bars 5..9
    second = out.iloc[1]
    assert second["open"] == pytest.approx(20005.0 - 0.5)
    assert second["close"] == pytest.approx(20009.0)
    assert second["volume"] == 500


def test_aggregate_bar_min_1_is_identity():
    df = make_1m("2026-05-04 09:30", 10)
    out = cc.aggregate(df, 1)
    pd.testing.assert_frame_equal(out, df)


# ---------------------------------------------------------------------------
# 2) assign_bins
# ---------------------------------------------------------------------------
def test_assign_bins_has_all_columns():
    df = make_1m("2026-05-04 09:30", 5)
    out = cc.assign_bins(df)
    for col in ("ts_et", "session_date", "iso_year", "iso_week",
                "iso_week_key", "_ym", "bin_1h", "bin_4h"):
        assert col in out.columns


def test_assign_bins_session_boundary_1758_1759_same_session():
    # 17:58 and 17:59 ET belong to the SAME (current) session
    df = make_1m("2026-05-04 17:58", 2)
    out = cc.assign_bins(df)
    assert out["session_date"].iloc[0] == out["session_date"].iloc[1]
    # session date is 2026-05-04 (current day)
    assert out["session_date"].iloc[0] == pd.Timestamp("2026-05-04")


def test_assign_bins_1800_rolls_to_next_session():
    # 17:59 ET -> session 05-04; 18:00/18:01 ET -> session 05-05 and greater
    df = make_1m("2026-05-04 17:59", 3)  # 17:59, 18:00, 18:01
    out = cc.assign_bins(df)
    assert out["session_date"].iloc[0] == pd.Timestamp("2026-05-04")
    assert out["session_date"].iloc[1] == pd.Timestamp("2026-05-05")
    assert out["session_date"].iloc[2] == pd.Timestamp("2026-05-05")
    assert out["session_date"].iloc[1] > out["session_date"].iloc[0]


def test_assign_bins_sunday_1800_and_monday_share_iso_week():
    # Sunday 2026-05-03 18:00 ET folds into Monday's session & same ISO week
    sun = make_1m("2026-05-03 18:00", 1)
    mon = make_1m("2026-05-04 09:30", 1)
    df = pd.concat([sun, mon], ignore_index=True)
    out = cc.assign_bins(df)
    assert out["iso_year"].iloc[0] == out["iso_year"].iloc[1]
    assert out["iso_week"].iloc[0] == out["iso_week"].iloc[1]
    assert out["iso_week_key"].iloc[0] == out["iso_week_key"].iloc[1]
    # Sunday 18:00 folds to Monday's session date
    assert out["session_date"].iloc[0] == pd.Timestamp("2026-05-04")


# ---------------------------------------------------------------------------
# 3) BUCKET_SPEC + BUCKET_BIN_COL
# ---------------------------------------------------------------------------
def test_bucket_spec_keys_are_seven_timeframes():
    assert set(cc.BUCKET_SPEC.keys()) == {
        "1m", "5m", "15m", "30m", "1h", "4h", "1d"}


def test_bucket_spec_15m_present_and_shaped():
    cfg = cc.BUCKET_SPEC["15m"]
    assert cfg["bar_min"] == 15
    assert cfg["history_days"] == 180
    assert cfg["display_bars"] == 2000
    assert cfg["default_zoom"] == 460     # ~5-day rolling window (PROTO style)
    assert cfg["buckets"] == ["1D", "4H"]  # per-day H/L/C + prior-4H horizontal lines
    assert cfg["static"] == ["PW", "PM"]  # full-width prior week/month


def test_bucket_spec_30m_proto_buckets():
    # 15m and 30m use the PROTO style: per-day 1D segments + prior-4H lines +
    # week/month statics; 1m/5m keep their 1H/4H buckets.
    assert cc.BUCKET_SPEC["30m"]["buckets"] == ["1D", "4H"]
    assert cc.BUCKET_SPEC["30m"]["static"] == ["PW", "PM"]
    assert cc.BUCKET_SPEC["1m"]["buckets"] == ["1H", "4H"]
    assert cc.BUCKET_SPEC["5m"]["buckets"] == ["1H", "4H"]


def test_bucket_spec_1h_buckets_and_static():
    # 1h uses the same PROTO style as 15m/30m: per-day 1D segments + PW/PM statics.
    cfg = cc.BUCKET_SPEC["1h"]
    assert cfg["buckets"] == ["1D"]
    assert cfg["static"] == ["PW", "PM"]


def test_bucket_spec_every_cfg_has_bar_min_and_zoom():
    for tf, cfg in cc.BUCKET_SPEC.items():
        assert cfg["bar_min"] >= 1
        assert "default_zoom" in cfg


def test_bucket_bin_col_mapping():
    assert cc.BUCKET_BIN_COL == {
        "1H": "bin_1h", "4H": "bin_4h", "1D": "session_date",
        "1W": "iso_week_key", "1M": "_ym"}


# ---------------------------------------------------------------------------
# 4) bucket_segments
# ---------------------------------------------------------------------------
def _two_sessions_hourly():
    """Two distinct sessions of hourly bars with separable highs.

    Session A: 2026-05-04 09:30 ET for 8 hourly bars (highs around base+...).
    Session B: 2026-05-05 09:30 ET for 8 hourly bars at a clearly different,
    higher price band so the two sessions' highs are distinct values.
    """
    a = make_1m("2026-05-04 09:30", 8 * 60, base=20000.0, step=1.0)
    a = cc.aggregate(a, 60)
    b = make_1m("2026-05-05 09:30", 8 * 60, base=30000.0, step=1.0)
    b = cc.aggregate(b, 60)
    df = pd.concat([a, b], ignore_index=True)
    return cc.assign_bins(df), a, b


def test_bucket_segments_projects_prior_session_high():
    df, a, b = _two_sessions_hourly()
    segs = cc.bucket_segments(df, ["1D"])
    high_vals = [s["value"] for s in segs if s["type"] == "1DH"]
    session_a_high = a["high"].max()
    session_b_high = b["high"].max()
    # The prior session's (A) high must be projected forward into session B.
    assert any(v == pytest.approx(session_a_high) for v in high_vals)
    # Session B's OWN high must NOT appear (we only project the PRIOR bucket).
    assert not any(v == pytest.approx(session_b_high) for v in high_vals)


def test_bucket_segments_dict_shape():
    df, a, b = _two_sessions_hourly()
    segs = cc.bucket_segments(df, ["1D"])
    assert segs, "expected at least one projected segment"
    s = segs[0]
    assert set(s.keys()) == {"x_start_ms", "x_end_ms", "value", "type"}
    assert s["x_end_ms"] >= s["x_start_ms"]
    assert s["type"] in ("1DH", "1DL", "1DC")


# ---------------------------------------------------------------------------
# 5) dividers
# ---------------------------------------------------------------------------
def _ms_to_et(ms_list):
    return [pd.Timestamp(int(m), unit="ms", tz="UTC").tz_convert(ET)
            for m in ms_list]


def test_dividers_has_always_on_keys():
    df = make_1m("2026-05-04 09:30", 390)
    div = cc.dividers(df, ["1H", "4H"])
    assert "cash_open" in div
    assert "futures_open" in div
    assert "am_0959" in div     # 9:59 ET line (15m/30m/1h + 1m/5m)
    assert "pm_0459" in div     # PROTO 15m/30m: 16:59 ET (4:59 PM, day end)


def test_dividers_proto_marks_at_0959_and_1659_et():
    # Monday 2026-05-04 RTH: 09:30 -> 17:00 covers both 9:59 and 16:59.
    df = make_1m("2026-05-04 09:30", 450)
    div = cc.dividers(df, ["1D"])
    am = _ms_to_et(div["am_0959"])
    pm = _ms_to_et(div["pm_0459"])
    assert am and all(t.hour == 9 and t.minute == 59 for t in am)
    assert pm and all(t.hour == 16 and t.minute == 59 for t in pm)
    # RTH only — no Saturday/Sunday marks.
    assert all(t.weekday() <= 4 for t in am + pm)


def test_dividers_1h_all_minute_30_et():
    df = make_1m("2026-05-04 09:30", 390)
    div = cc.dividers(df, ["1H"])
    assert div["1H"], "expected 1H divider marks"
    for t in _ms_to_et(div["1H"]):
        assert t.minute == 0   # TOS grid: 1H dividers on the hour


def test_dividers_1w_marks_sunday_1800_et():
    # window spanning a Sunday so a 1W boundary exists
    df = make_1m("2026-05-01 09:30", 6 * 24 * 60)  # ~6 days from Fri
    div = cc.dividers(df, ["1W"])
    assert div["1W"], "expected a 1W divider"
    for t in _ms_to_et(div["1W"]):
        assert t.weekday() == 6  # Sunday
        assert t.hour == 18


def test_dividers_1h_unique_and_hourly_across_dst_spring_forward():
    # DST spring-forward: 2026-03-08 02:00 ET -> 03:00 ET
    df = make_1m("2026-03-08 00:00", 600)
    div = cc.dividers(df, ["1H"])
    marks = sorted(div["1H"])
    assert len(marks) == len(set(marks))  # unique
    diffs = np.diff(marks)
    # 1H excludes the 4H hours, so gaps are 1h normally and 2h across a 4H mark.
    # DST-robustness = every gap is a clean multiple of 1h (no sub-hour drift).
    assert all(d % 3_600_000 == 0 for d in diffs)
    assert set(diffs) <= {3_600_000, 7_200_000}


# ---------------------------------------------------------------------------
# 6) validate_ticker_tf
# ---------------------------------------------------------------------------
def test_validate_ticker_tf_lowercases_ticker():
    assert cc.validate_ticker_tf("nq", "5m") == ("NQ", "5m")


def test_validate_ticker_tf_bad_ticker_raises():
    with pytest.raises(ValueError):
        cc.validate_ticker_tf("AAPL", "5m")


def test_validate_ticker_tf_bad_tf_raises():
    with pytest.raises(ValueError):
        cc.validate_ticker_tf("NQ", "2m")


# ---------------------------------------------------------------------------
# 7) current_bucket_projection
# ---------------------------------------------------------------------------
def test_current_bucket_projection_emits_when_now_in_later_bucket():
    # 90 1m bars from 09:30 ET -> last bar at 10:59 ET (within the 10:00..10:59
    # hour bucket). now_et at 12:15 ET sits in a strictly later 1H bucket.
    df = make_1m("2026-05-04 09:30", 90)
    df = cc.assign_bins(df)
    now_et = pd.Timestamp("2026-05-04 12:15", tz=ET)
    segs = cc.current_bucket_projection(df, ["1H"], now_et=now_et)
    assert segs, "expected a projected in-progress segment"
    s = segs[0]
    assert set(s.keys()) == {"x_start_ms", "x_end_ms", "value", "type", "live"}
    assert s["live"] is True   # in-progress projection is flagged so the UI won't label it
    assert s["type"] in ("1HH", "1HL", "1HC")
    # The projected high equals the last (in-progress source) bucket's high.
    last_bucket_id = df["bin_1h"].iloc[-1]
    last_group = df[df["bin_1h"] == last_bucket_id]
    proj_high = [x["value"] for x in segs if x["type"] == "1HH"][0]
    assert proj_high == pytest.approx(last_group["high"].max())


def test_current_bucket_projection_empty_when_now_in_same_bucket():
    df = make_1m("2026-05-04 09:00", 30)  # last bar 09:29 ET, 09:00 hour bucket
    df = cc.assign_bins(df)
    # now_et inside the SAME 1H (on-the-hour) bucket as the last bar -> no projection.
    now_et = pd.Timestamp("2026-05-04 09:45", tz=ET)
    segs = cc.current_bucket_projection(df, ["1H"], now_et=now_et)
    assert segs == []


def test_aggregate_daily_is_session_anchored_1800et():
    # 1m bars spanning a couple sessions; daily candles must start at 18:00 ET
    # (futures session), NOT UTC midnight.
    df = make_1m("2026-05-18 17:55", 600)   # crosses the 18:00 ET session open
    out = cc.aggregate(df, 1440)
    et = out["ts"].dt.tz_convert(ET)
    assert (et.dt.hour == 18).all() and (et.dt.minute == 0).all(), \
        f"daily bars not anchored to 18:00 ET: {et.tolist()}"


def test_aggregate_4h_is_anchored_tos_grid():
    df = make_1m("2026-05-20 18:00", 600)   # covers 4h boundaries (TOS grid)
    out = cc.aggregate(df, 240)
    et = out["ts"].dt.tz_convert(ET)
    assert (et.dt.minute == 0).all(), f"4h bars not on the hour: {et.tolist()}"
    assert set(et.dt.hour.unique()) <= {1, 5, 9, 13, 17, 21}, \
        f"4h bars not on the TOS 1/5/9/13/17/21 ET cycle: {sorted(et.dt.hour.unique())}"


def test_bucket_segments_hlc_override_uses_base_values():
    # view bars (resampled) would give a different close than the 1m-base lookup;
    # the override must win for the projected values, x-positions stay from view.
    df, a, b = _two_sessions_hourly()
    override = {"1D": cc.bucket_hlc_lookup(df, "1D")}
    # tweak the override close for session A so we can detect it's used
    a_id = df["session_date"].iloc[0]
    h, l, _ = override["1D"][a_id]
    override["1D"][a_id] = (h, l, 12345.0)
    segs = cc.bucket_segments(df, ["1D"], hlc_by_label=override)
    closes = [s["value"] for s in segs if s["type"] == "1DC"]
    assert any(v == 12345.0 for v in closes), "override close not applied"
