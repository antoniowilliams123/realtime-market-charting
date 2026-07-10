"""Pure-logic compute foundation for the futures charting app.

This module is intentionally free of any I/O, network, Flask, or Databento
dependencies.  Importing it has NO side effects.  Everything operates on
plain pandas/numpy DataFrames keyed by a UTC tz-aware ``ts`` column.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ET = "America/New_York"

# Clock anchors expressed as minutes past ET midnight. Bins are computed in ET
# WALL-CLOCK (see assign_bins) so boundaries stay on fixed ET hours across DST,
# matching the ThinkorSwim grid (migrated 2026-05-23).
ONE_H_ANCHOR_MIN = 0            # 1H buckets on the hour (TOS)
FOUR_H_ANCHOR_MIN = 60         # 4H anchor 1:00 ET -> boundaries 1/5/9/13/17/21 ET


# ---------------------------------------------------------------------------
# 3) BUCKET_SPEC + BUCKET_BIN_COL
# ---------------------------------------------------------------------------
# Per-timeframe configuration: aggregation size, how much history to load,
# display/zoom bar counts, which projected buckets to draw, and which static
# prior-period levels (PD/PW/PM) to overlay.
BUCKET_SPEC = {
    "1m":  {"bar_min": 1,    "history_days": 60,   "display_bars": 7200,
            "default_zoom": 2880, "buckets": ["1H", "4H"], "static": ["PD", "PW", "PM"]},
    "5m":  {"bar_min": 5,    "history_days": 90,   "display_bars": 2592,
            "default_zoom": 240, "buckets": ["1H", "4H"], "static": ["PD", "PW", "PM"]},
    # 15m/30m render the PROTOTYPE look (PROTO_buckets_example.png): per-day prior
    # H/L/C (1D segments → YH/YL/YC) + full-width prior week/month (PW/PM) levels,
    # with 9:59 ET + 16:59 ET (4:59 PM) dividers. "4H" adds the prior-4H H/L/C as
    # horizontal lines only (the frontend draws no 4H vertical divider on these TFs).
    "15m": {"bar_min": 15,   "history_days": 180,  "display_bars": 2000,
            "default_zoom": 460, "buckets": ["1D", "4H"], "static": ["PW", "PM"]},
    "30m": {"bar_min": 30,   "history_days": 365,  "display_bars": 2000,
            "default_zoom": 240, "buckets": ["1D", "4H"], "static": ["PW", "PM"]},
    # 1h conforms to the SAME prototype look as 15m/30m: per-day prior H/L/C
    # (1D segments) + full-width prior week/month (PW/PM), 9:59/16:59 dividers.
    "1h":  {"bar_min": 60,   "history_days": 365,  "display_bars": 2000,
            "default_zoom": 240, "buckets": ["1D"], "static": ["PW", "PM"]},
    # 4h + 1d draw the month grid CLIENT-SIDE (annual month-grid look) so it spans
    # lazy-loaded deep history too — no backend buckets/segments needed.
    "4h":  {"bar_min": 240,  "history_days": 730,  "display_bars": 2000,
            "default_zoom": 240, "buckets": [], "static": []},
    "1d":  {"bar_min": 1440, "history_days": 1825, "display_bars": 1500,
            "default_zoom": 240, "buckets": [], "static": []},
}

# Maps a projected-bucket label to the assign_bins column that identifies it.
BUCKET_BIN_COL = {
    "1H": "bin_1h",
    "4H": "bin_4h",
    "1D": "session_date",
    "1W": "iso_week_key",
    "1M": "_ym",
}


# ---------------------------------------------------------------------------
# 1) aggregate
# ---------------------------------------------------------------------------
_OHLCV_AGG = {"open": "first", "high": "max", "low": "min",
              "close": "last", "volume": "sum"}


def aggregate(df_1m: pd.DataFrame, bar_min: int) -> pd.DataFrame:
    """Resample 1-minute OHLCV bars up to ``bar_min`` minute bars.

    Futures candles are SESSION-anchored, not UTC-anchored, so a daily candle is
    the 18:00 ET session (not a UTC calendar day) and a 4h candle is anchored to
    the ThinkorSwim grid (boundaries 1/5/9/13/17/21 ET). Resampling for >= 1h is
    done in ET wall-clock via an offset so the bars line up with the
    session/divider grid; intraday (< 1h) stays on the plain grid (minute phases
    already match ET). ``bar_min == 1`` returns the input unchanged.
    """
    if bar_min == 1:
        return df_1m

    # ET-anchored offsets so candle boundaries == session/bucket boundaries.
    # 1h on the top of the hour; 4h at 1/5/9/13/17/21 ET (TOS grid: offset 1h from
    # ET midnight); 1d at 18:00 ET (session day). A futures trading day = 18:00 ET
    # -> 16:59 ET (17:00-18:00 = gap).
    et_offsets = {60: "0min", 240: "1h", 1440: "18h"}
    if bar_min in et_offsets:
        et_idx = df_1m["ts"].dt.tz_convert(ET)
        tmp = df_1m[["open", "high", "low", "close", "volume"]].copy()
        tmp.index = pd.DatetimeIndex(et_idx, name="ts")
        agg = (tmp.resample(f"{bar_min}min", label="left", closed="left",
                            offset=et_offsets[bar_min])
                  .agg(_OHLCV_AGG).dropna(subset=["open"]).reset_index())
        agg["ts"] = agg["ts"].dt.tz_convert("UTC")   # back to UTC for the rest of the pipeline
        return agg

    # Intraday (< 1h): plain grid (5m/15m/30m already align to ET :00/:30...).
    return (df_1m.set_index("ts")
            .resample(f"{bar_min}min", label="left", closed="left")
            .agg(_OHLCV_AGG).dropna(subset=["open"]).reset_index())


# ---------------------------------------------------------------------------
# 2) assign_bins
# ---------------------------------------------------------------------------
def assign_bins(df: pd.DataFrame) -> pd.DataFrame:
    """Annotate each bar with ET-local session/calendar bin columns.

    Adds:
      ts_et         -- ts converted to America/New_York
      session_date  -- datetime64[ns]; ET hour >= 18 folds into the NEXT day
      iso_year, iso_week, iso_week_key (= iso_year*100 + iso_week)
      _ym           -- session_date.year*100 + session_date.month
      bin_1h        -- 1H clock bucket id (on the hour, ET wall-clock)
      bin_4h        -- 4H clock bucket id (1/5/9/13/17/21 ET, TOS grid)

    Hour bins are computed in ET WALL-CLOCK (not UTC epoch) so the boundaries
    stay on the same ET hours across DST — matching the ThinkorSwim grid.
    """
    out = df.copy()
    ts_et = out["ts"].dt.tz_convert(ET)
    out["ts_et"] = ts_et

    # Session date: ET hour >= 18 belongs to the NEXT day's session.
    et_day = ts_et.dt.normalize().dt.tz_localize(None)
    roll = (ts_et.dt.hour >= 18).to_numpy()
    session_date = et_day + pd.to_timedelta(np.where(roll, 1, 0), unit="D")
    out["session_date"] = session_date

    iso = session_date.dt.isocalendar()
    out["iso_year"] = iso["year"].astype("int64").to_numpy()
    out["iso_week"] = iso["week"].astype("int64").to_numpy()
    out["iso_week_key"] = out["iso_year"] * 100 + out["iso_week"]

    out["_ym"] = (session_date.dt.year * 100 + session_date.dt.month).to_numpy()

    # Bins anchored to ET WALL-CLOCK (matches the ThinkorSwim grid): 1H on the
    # hour, 4H at 1/5/9/13/17/21 ET. ET-naive minutes keep boundaries on the same
    # ET hours across DST.
    et_naive_min = ts_et.dt.tz_localize(None).astype("int64") // 60_000_000_000
    out["bin_1h"] = (et_naive_min - ONE_H_ANCHOR_MIN) // 60
    out["bin_4h"] = (et_naive_min - FOUR_H_ANCHOR_MIN) // 240
    return out


# ---------------------------------------------------------------------------
# 4) bucket_segments
# ---------------------------------------------------------------------------
def _ts_ms(df: pd.DataFrame) -> np.ndarray:
    """UTC timestamps as integer milliseconds."""
    return (df["ts"].astype("int64") // 1_000_000).to_numpy()


def bucket_hlc_lookup(df: pd.DataFrame, label: str) -> dict:
    """Return {bin_id: (high, low, close)} for one bucket label.

    Intended to be computed from the 1-MINUTE base so the close is the true
    session/period close (the resampled higher-TF bars have a bar straddling the
    17:00-18:00 ET maintenance gap whose close is the reopen, not the session
    close). ``close`` is the last bar of each group in time order.
    """
    col = BUCKET_BIN_COL[label]
    agg = (df.groupby(col, sort=True)
             .agg(h=("high", "max"), l=("low", "min"), c=("close", "last")))
    return {bid: (float(r.h), float(r.l), float(r.c)) for bid, r in agg.iterrows()}


def bucket_segments(df: pd.DataFrame, bucket_labels, hlc_by_label=None) -> list[dict]:
    """Project each PRIOR closed bucket's H/L/C forward across the current one.

    For every label in ``bucket_labels`` we group the bars by the bin column
    given in ``BUCKET_BIN_COL``.  Walking the groups in time order, each group
    after the first emits three segments (H/L/C) whose ``value`` is taken from
    the *previous* group's aggregate.  The segment spans from the first bar of
    the current group to the first bar of the next group (or the last bar of
    the frame for the final group).

    ``hlc_by_label`` optionally supplies a {label: {bin_id: (h,l,c)}} override
    (e.g. computed from the 1-minute base via :func:`bucket_hlc_lookup`) so the
    projected VALUES are the true session H/L/C while the x-positions still come
    from ``df`` (the chart bars). When absent, values are computed from ``df``.

    Returns a flat list of dicts:
        {x_start_ms, x_end_ms, value, type}
    with ``type`` one of f"{label}H"/f"{label}L"/f"{label}C".
    """
    segments: list[dict] = []
    if len(df) == 0:
        return segments

    ms = _ts_ms(df)
    last_ms = int(ms[-1])

    for label in bucket_labels:
        col = BUCKET_BIN_COL[label]
        ids = df[col].to_numpy()
        ext = (hlc_by_label or {}).get(label)   # {bin_id: (h,l,c)} or None

        # Group boundaries: index of the first bar of each contiguous group.
        change = np.empty(len(ids), dtype=bool)
        change[0] = True
        change[1:] = ids[1:] != ids[:-1]
        starts = np.flatnonzero(change)

        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        close = df["close"].to_numpy()

        n_groups = len(starts)
        for gi in range(1, n_groups):  # skip leading group (no prior bucket)
            prev_lo = starts[gi - 1]
            prev_hi = starts[gi]  # exclusive end of the prior group
            cur_lo = starts[gi]
            x_start = int(ms[cur_lo])
            if gi + 1 < n_groups:
                x_end = int(ms[starts[gi + 1]])
            else:
                x_end = last_ms

            prior_id = ids[prev_lo]
            if ext is not None and prior_id in ext:
                prior_high, prior_low, prior_close = ext[prior_id]
            else:
                prior_high = float(high[prev_lo:prev_hi].max())
                prior_low = float(low[prev_lo:prev_hi].min())
                prior_close = float(close[prev_hi - 1])

            segments.append({"x_start_ms": x_start, "x_end_ms": x_end,
                             "value": prior_high, "type": f"{label}H"})
            segments.append({"x_start_ms": x_start, "x_end_ms": x_end,
                             "value": prior_low, "type": f"{label}L"})
            segments.append({"x_start_ms": x_start, "x_end_ms": x_end,
                             "value": prior_close, "type": f"{label}C"})

    return segments


# ---------------------------------------------------------------------------
# 5) dividers
# ---------------------------------------------------------------------------
# ET clock hours at which a 4H bucket boundary sits (TOS grid: anchor 1:00 ET, +4h).
FOUR_H_HOURS = {1, 5, 9, 13, 17, 21}


def _et_span(df: pd.DataFrame):
    """Return (lo, hi) ET timestamps and the data UTC [min,max] range.

    Span is the df ET min floored to the day minus 2h .. ET max ceiled to the
    next day plus 2h.  Marks are still clamped to the actual data UTC range so
    we never emit boundaries outside the loaded bars.
    """
    et = df["ts"].dt.tz_convert(ET)
    tmin = df["ts"].min()
    tmax = df["ts"].max()
    lo = et.min().normalize() - pd.Timedelta(hours=2)
    hi = et.max().normalize() + pd.Timedelta(days=1) + pd.Timedelta(hours=2)
    return lo, hi, tmin, tmax


def _et_marks_to_ms(times, tmin, tmax) -> list[int]:
    """Filter ET-tz timestamps to the data UTC range and return sorted ms."""
    out = []
    for t in times:
        u = t.tz_convert("UTC")
        if tmin <= u <= tmax:
            out.append(int(t.timestamp() * 1000))
    return sorted(set(out))


def dividers(df: pd.DataFrame, bucket_labels) -> dict:
    """Calendar-based vertical divider timestamps (UTC ms) keyed by label.

    Always includes ``cash_open`` (09:30 ET daily) and ``futures_open``
    (18:00 ET daily).  Per requested label:
      1H -> every :00 ET EXCEPT the 4H hours (so 1H/4H never double-draw)
      4H -> ET hours {1,5,9,13,17,21} on the hour (TOS grid)
      1D -> 18:00 ET daily
      1W -> Sunday 18:00 ET
      1M -> 18:00 ET the day before the 1st of each month

    All marks are clamped to the loaded data's UTC range.  Construction uses
    tz-aware ET date_range so DST transitions never introduce sub-hour drift.
    """
    result: dict = {}
    if len(df) == 0:
        for label in bucket_labels:
            result[label] = []
        result["cash_open"] = []
        result["futures_open"] = []
        return result

    lo, hi, tmin, tmax = _et_span(df)

    # Hourly grid on the hour ET (DST-stable via tz-aware date_range).
    hourly = pd.date_range(lo.floor("h"), hi, freq="h", tz=ET)
    # Daily grid at arbitrary clock hours (built per-need below).
    daily = pd.date_range(lo.normalize(), hi, freq="D", tz=ET)

    for label in bucket_labels:
        if label == "1H":
            # Exclude 4H hours so 1H and 4H dividers never double-draw at the
            # same boundary (matches the existing app's _boundaries_payload).
            times = [t for t in hourly
                     if t.minute == 0 and t.hour not in FOUR_H_HOURS]
        elif label == "4H":
            times = [t for t in hourly
                     if t.minute == 0 and t.hour in FOUR_H_HOURS]
        elif label == "1D":
            times = [d + pd.Timedelta(hours=18) for d in daily]
        elif label == "1W":
            # Sunday 18:00 ET (weekday()==6).
            times = [d + pd.Timedelta(hours=18)
                     for d in daily if d.weekday() == 6]
        elif label == "1M":
            # 18:00 ET on the day BEFORE the 1st of each month.
            times = [d + pd.Timedelta(hours=18)
                     for d in daily if (d + pd.Timedelta(days=1)).day == 1]
        else:
            times = []
        result[label] = _et_marks_to_ms(times, tmin, tmax)

    # Always-on session opens.
    result["cash_open"] = _et_marks_to_ms(
        [d + pd.Timedelta(hours=9, minutes=30) for d in daily], tmin, tmax)
    result["futures_open"] = _et_marks_to_ms(
        [d + pd.Timedelta(hours=18) for d in daily], tmin, tmax)
    # Prototype dividers: 9:59 ET and 16:59 ET (4:59 PM, the futures day end).
    # RTH only (Mon-Fri); weekends have no 9:59/16:59 bar so a snap would land on
    # a stray Sunday-open bar. The frontend draws these on 15m/30m/1h (and the
    # 9:59 line also on 1m/5m).
    weekdays = [d for d in daily if d.weekday() <= 4]
    result["am_0959"] = _et_marks_to_ms(
        [d + pd.Timedelta(hours=9, minutes=59) for d in weekdays], tmin, tmax)
    result["pm_0459"] = _et_marks_to_ms(
        [d + pd.Timedelta(hours=16, minutes=59) for d in weekdays], tmin, tmax)
    return result


# ---------------------------------------------------------------------------
# 5b) maintenance_gaps — CME 17:00-18:00 ET non-trading window
# ---------------------------------------------------------------------------
def maintenance_gaps(df: pd.DataFrame, bar_min: int) -> list[dict]:
    """List of CME daily maintenance-break windows (17:00-18:00 ET) in ``df``.

    The CME daily break is 17:00-18:00 ET: trading stops in the 17:00 ET hour and
    resumes at the 18:00 ET session open. We detect a real break gap that crosses
    that window (last bar in the 16:00/17:00 ET hour, next bar in the 18:00 ET
    hour) and emit a FIXED 17:00-18:00 ET box for it.

    Keying on the 18:00 ET re-open — not on raw bar deltas — is essential for
    illiquid products like gold (GC), whose overnight session has hundreds of
    one-bar no-trade holes. Those holes are NOT maintenance breaks and must not
    be shaded; only the true 17:00-18:00 ET window is. The 3-hour cap on the gap
    still excludes the Fri 17:00 -> Sun 18:00 weekend gap.
    """
    if len(df) < 2 or bar_min <= 0:
        return []
    ts = pd.to_datetime(df["ts"], utc=True)
    ts_ms = ts.astype("int64").to_numpy() // 1_000_000
    et = ts.dt.tz_convert("America/New_York")
    hour = et.dt.hour.to_numpy()
    bar_ms = bar_min * 60_000
    delta = ts_ms[1:] - ts_ms[:-1]
    # Real break gap (> 1.5 bars, < 3h to skip the weekend) crossing 17->18 ET.
    is_gap = (delta > int(bar_ms * 1.5)) & (delta < 3 * 60 * 60_000)
    cross = is_gap & np.isin(hour[:-1], (16, 17)) & (hour[1:] == 18)
    # Resumption bar floored to its hour == 18:00 ET; the box is the hour before.
    et_floor = et.dt.floor("h")
    out = []
    for i in np.where(cross)[0]:
        open18 = et_floor.iloc[i + 1]                 # 18:00 ET (tz-aware)
        start17 = open18 - pd.Timedelta(hours=1)       # 17:00 ET
        out.append({"start_ms": int(start17.value // 1_000_000),
                    "end_ms": int(open18.value // 1_000_000)})
    return out


# ---------------------------------------------------------------------------
# 6) validate_ticker_tf
# ---------------------------------------------------------------------------
TICKERS = ("ES", "NQ", "YM", "RTY", "GC", "CL")   # MBT removed 2026-06-15 (not traded)


def validate_ticker_tf(ticker: str, tf: str) -> tuple[str, str]:
    """Normalise + validate a (ticker, timeframe) pair.

    Upper-cases ``ticker``.  Raises ``ValueError`` if the ticker is unknown or
    the timeframe is not one of the configured BUCKET_SPEC keys.
    """
    t = ticker.upper()
    if t not in TICKERS:
        raise ValueError(f"unknown ticker: {ticker!r} (allowed: {TICKERS})")
    if tf not in BUCKET_SPEC:
        raise ValueError(
            f"unknown timeframe: {tf!r} (allowed: {tuple(BUCKET_SPEC)})")
    return t, tf


# ---------------------------------------------------------------------------
# 7) current_bucket_projection
# ---------------------------------------------------------------------------
def _bucket_id_for(label: str, ts_utc: pd.Timestamp):
    """Compute the bucket id (matching assign_bins semantics) for one ts."""
    et = ts_utc.tz_convert(ET)
    # 1H/4H bins are ET WALL-CLOCK (TOS grid), matching assign_bins.
    if label in ("1H", "4H"):
        et_naive_min = et.tz_localize(None).value // 60_000_000_000
        if label == "1H":
            return (et_naive_min - ONE_H_ANCHOR_MIN) // 60
        return (et_naive_min - FOUR_H_ANCHOR_MIN) // 240
    # Session/ISO-week/month buckets derive from ET local date.
    sess = et.normalize().tz_localize(None)
    if et.hour >= 18:
        sess = sess + pd.Timedelta(days=1)
    if label == "1D":
        return sess
    if label == "1W":
        iso = sess.isocalendar()
        return int(iso.year) * 100 + int(iso.week)
    if label == "1M":
        return sess.year * 100 + sess.month
    raise ValueError(f"unknown bucket label: {label!r}")


def current_bucket_projection(df: pd.DataFrame, bucket_labels,
                              now_et=None) -> list[dict]:
    """Project the last bar's (in-progress) bucket H/L/C into the current one.

    Mirrors :func:`bucket_segments` but for the live, not-yet-closed bucket.
    If ``now_et`` falls in a strictly later bucket than the most recent bar,
    the last bar's bucket H/L/C is projected forward across the window from the
    last bar to ``now_et``.  ``now_et`` is injectable for deterministic tests
    and defaults to ``pd.Timestamp.now(tz=ET)``.
    """
    segments: list[dict] = []
    if len(df) == 0:
        return segments
    if now_et is None:
        now_et = pd.Timestamp.now(tz=ET)

    now_utc = now_et.tz_convert("UTC")
    now_ms = int(now_utc.value // 1_000_000)

    ms = _ts_ms(df)
    last_ms = int(ms[-1])
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()

    for label in bucket_labels:
        col = BUCKET_BIN_COL[label]
        ids = df[col].to_numpy()
        last_id = ids[-1]
        now_id = _bucket_id_for(label, now_utc)
        if now_id <= last_id:
            continue  # still inside the last bar's bucket: nothing to project

        # Source = bars belonging to the last (in-progress) bucket.
        mask = ids == last_id
        src_high = float(high[mask].max())
        src_low = float(low[mask].min())
        src_close = float(close[mask][-1])

        for value, suffix in ((src_high, "H"), (src_low, "L"),
                              (src_close, "C")):
            segments.append({"x_start_ms": last_ms, "x_end_ms": now_ms,
                             "value": value, "type": f"{label}{suffix}",
                             "live": True})   # in-progress own-value: drawn, NOT labeled

    return segments
