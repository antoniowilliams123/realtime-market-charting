"""Standard chart feature columns — moving averages, VWAPs, prior-period levels.

Pure pandas/numpy, no I/O. Everything here is textbook charting math:
  * ema20 / ema50            exponential moving averages of close
  * sma200                   simple moving average of close
  * vwap_d / vwap_w          session- and week-anchored VWAP (typical price)
  * pdh/pdl/pdc              prior session day's high / low / close
  * pwh/pwl/pwc              prior ISO week's high / low / close
  * pmh/pml/pmc              prior month's high / low / close
  * how/low_w, hom/lom       running high/low of the current week / month
  * session_hod/session_lod  running high/low of the current session day

Expects the bin columns from :func:`chart_compute.assign_bins` (``session_date``,
``iso_year``/``iso_week``, ``_ym``); computes them on the fly when absent.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import chart_compute as cc

EMA_PERIOD = 20
EMA50_PERIOD = 50
SMA_PERIOD = 200

# Intraday clock-bucket H/L/C lookups are not part of the open-source build;
# the app degrades gracefully when these are empty (levels simply not drawn).
FOUR_H_ANCHOR_MIN = cc.FOUR_H_ANCHOR_MIN
BIN_4H_HLC: dict[int, tuple[float, float, float]] = {}
BIN_1H_HLC: dict[int, tuple[float, float, float]] = {}


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return ``df`` with the standard feature columns appended (copy-safe)."""
    if "session_date" not in df.columns:
        df = cc.assign_bins(df)
    else:
        df = df.copy()

    close = df["close"].to_numpy(dtype=np.float64)
    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)
    volume = df["volume"].to_numpy(dtype=np.float64)
    n = len(close)
    typ = (high + low + close) / 3.0

    # Prior-day levels: per-session aggregates shifted one session back.
    daily = (df.groupby("session_date")
               .agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
               .sort_index())
    daily["pdh"] = daily["h"].shift(1)
    daily["pdl"] = daily["l"].shift(1)
    daily["pdc"] = daily["c"].shift(1)
    df = df.merge(daily[["pdh", "pdl", "pdc"]],
                  left_on="session_date", right_index=True, how="left")

    # Prior-week levels + running week high/low.
    weekly = (df.groupby(["iso_year", "iso_week"])
                .agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
                .sort_index())
    weekly["pwh"] = weekly["h"].shift(1)
    weekly["pwl"] = weekly["l"].shift(1)
    weekly["pwc"] = weekly["c"].shift(1)
    df = df.merge(weekly[["pwh", "pwl", "pwc"]],
                  left_on=["iso_year", "iso_week"], right_index=True, how="left")
    df["how"] = df.groupby(["iso_year", "iso_week"])["high"].cummax()
    df["low_w"] = df.groupby(["iso_year", "iso_week"])["low"].cummin()

    # Prior-month levels + running month high/low.
    monthly = (df.groupby("_ym")
                 .agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
                 .sort_index())
    monthly["pmh"] = monthly["h"].shift(1)
    monthly["pml"] = monthly["l"].shift(1)
    monthly["pmc"] = monthly["c"].shift(1)
    df = df.merge(monthly[["pmh", "pml", "pmc"]],
                  left_on="_ym", right_index=True, how="left")
    df["hom"] = df.groupby("_ym")["high"].cummax()
    df["lom"] = df.groupby("_ym")["low"].cummin()

    # Running session high/low.
    df["session_hod"] = df.groupby("session_date")["high"].cummax()
    df["session_lod"] = df.groupby("session_date")["low"].cummin()

    # Moving averages.
    sma_full = np.convolve(close, np.ones(SMA_PERIOD) / SMA_PERIOD,
                           mode="full")[:n]
    sma = np.full(n, np.nan)
    sma[SMA_PERIOD - 1:] = sma_full[SMA_PERIOD - 1:]
    df["ema20"] = pd.Series(close).ewm(span=EMA_PERIOD, adjust=False).mean().to_numpy()
    df["ema50"] = pd.Series(close).ewm(span=EMA50_PERIOD, adjust=False).mean().to_numpy()
    df["sma200"] = sma

    # Anchored VWAPs (typical price), session-day and ISO-week anchors.
    pv = typ * volume
    tmp = pd.DataFrame({
        "pv": pv, "v": volume,
        "sd": df["session_date"].to_numpy(),
        "yw": df["iso_year"].to_numpy() * 100 + df["iso_week"].to_numpy(),
    })
    cum_pv = tmp.groupby("sd")["pv"].cumsum()
    cum_v = tmp.groupby("sd")["v"].cumsum()
    df["vwap_d"] = np.where(cum_v > 0, cum_pv / cum_v, np.nan)
    cum_pv_w = tmp.groupby("yw")["pv"].cumsum()
    cum_v_w = tmp.groupby("yw")["v"].cumsum()
    df["vwap_w"] = np.where(cum_v_w > 0, cum_pv_w / cum_v_w, np.nan)

    return df
