"""Synthetic market-data source for demo mode.

Lets the whole app run with NO market-data vendor account: seeds each ticker
with a deterministic random-walk 1-minute history that honors the CME session
calendar (daily 17:00-18:00 ET maintenance break, Friday 17:00 ET -> Sunday
18:00 ET weekend closure), then streams live 1-second and 1-minute records with
the same interface the vendor client yields, so ``TickerFeed.stream`` consumes
either source unchanged.

Enable with ``DEMO=1`` (see README). Synthetic prices are obviously not real —
the point is exercising the full pipeline: ingest -> resample -> features ->
levels -> SSE -> chart.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

ET = "America/New_York"

# Plausible price scale / tick size / per-minute volatility per demo ticker.
SPECS = {
    "ES":  {"px": 5600.0,  "tick": 0.25, "vol": 0.00012},
    "NQ":  {"px": 20500.0, "tick": 0.25, "vol": 0.00018},
    "YM":  {"px": 42500.0, "tick": 1.0,  "vol": 0.00011},
    "RTY": {"px": 2250.0,  "tick": 0.10, "vol": 0.00016},
    "GC":  {"px": 2700.0,  "tick": 0.10, "vol": 0.00009},
    "CL":  {"px": 74.0,    "tick": 0.01, "vol": 0.00022},
}


def _session_mask(ts_utc: pd.DatetimeIndex) -> np.ndarray:
    """True for minutes when CME equity-hours trading is open.

    Open Sun 18:00 ET -> Fri 17:00 ET with a daily 17:00-18:00 ET break.
    """
    et = ts_utc.tz_convert(ET)
    dow = et.dayofweek.to_numpy()          # Mon=0 .. Sun=6
    hour = et.hour.to_numpy()
    open_ = np.ones(len(et), dtype=bool)
    open_ &= ~(hour == 17)                                   # daily break
    open_ &= ~(dow == 5)                                     # Saturday
    open_ &= ~((dow == 6) & (hour < 18))                     # Sunday pre-open
    open_ &= ~((dow == 4) & (hour >= 17))                    # Friday close
    return open_


def synthetic_history(ticker: str, days: int = 60,
                      end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Deterministic synthetic 1-minute OHLCV history for one ticker.

    Seeded by ticker name so every boot draws the same series. Volatility gets
    a U-shaped intraday profile (busy at the open/close of the NY morning) and
    volume follows it, which makes the session structure visible on the chart.
    """
    spec = SPECS.get(ticker.upper(), {"px": 100.0, "tick": 0.01, "vol": 0.0002})
    if end is None:
        end = pd.Timestamp.now(tz="UTC").floor("min")
    idx = pd.date_range(end - pd.Timedelta(days=days), end, freq="1min", tz="UTC")
    idx = idx[_session_mask(idx)]
    n = len(idx)

    rng = np.random.default_rng(abs(hash(ticker.upper())) % (2**32))
    # U-shaped intraday volatility multiplier peaking near 09:30-11:00 ET.
    et_min = (idx.tz_convert(ET).hour * 60 + idx.tz_convert(ET).minute).to_numpy()
    m930 = 9 * 60 + 30
    intraday = 1.0 + 1.4 * np.exp(-((et_min - m930 - 45) / 150.0) ** 2)

    step = rng.standard_normal(n) * spec["vol"] * intraday
    # Gentle mean-reverting drift so multi-day charts show swings, not a line.
    drift = np.sin(np.linspace(0, days / 2.5 * np.pi, n)) * spec["vol"] * 0.4
    close = spec["px"] * np.exp(np.cumsum(step + drift))

    tick = spec["tick"]
    close = np.round(close / tick) * tick
    opn = np.empty(n); opn[0] = close[0]; opn[1:] = close[:-1]
    spread = np.abs(rng.standard_normal(n)) * spec["vol"] * intraday * close
    high = np.maximum(opn, close) + np.round(spread / tick) * tick
    low = np.minimum(opn, close) - np.round(spread * rng.random(n) / tick) * tick
    volume = (rng.integers(50, 400, n) * intraday).astype(np.int64)

    return pd.DataFrame({"ts": idx, "open": opn, "high": high, "low": low,
                         "close": close, "volume": volume})


@dataclass
class _Record:
    """Duck-typed stand-in for a vendor OHLCV record."""
    ts_event: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    rtype: str = "ohlcv-1s"


@dataclass
class DemoLive:
    """Iterable that emits synthetic 1s records (and a 1m record at each minute
    close), paced in real time. Same consumption contract as the vendor's live
    client: ``for record in live: ...``."""
    ticker: str
    seed_df: pd.DataFrame
    _rng: np.random.Generator = field(init=False)

    def __post_init__(self):
        self._rng = np.random.default_rng()
        spec = SPECS.get(self.ticker.upper(), {"px": 100.0, "tick": 0.01,
                                               "vol": 0.0002})
        self._tick = spec["tick"]
        self._vol = spec["vol"]
        self._px = float(self.seed_df["close"].iloc[-1]) if len(self.seed_df) \
            else spec["px"]

    def __iter__(self):
        minute_bar = None
        while True:
            now = time.time()
            ts_ns = int(now * 1_000_000_000)
            self._px *= float(np.exp(self._rng.standard_normal() * self._vol / 7.75))
            px = round(self._px / self._tick) * self._tick
            jitter = abs(self._rng.standard_normal()) * self._vol * px / 4
            hi = round((px + jitter) / self._tick) * self._tick
            lo = round((px - jitter) / self._tick) * self._tick
            vol = int(self._rng.integers(1, 30))
            yield _Record(ts_ns, px, hi, lo, px, vol, "ohlcv-1s")

            minute_start_ns = (ts_ns // 60_000_000_000) * 60_000_000_000
            if minute_bar is None or minute_bar["ts"] != minute_start_ns:
                if minute_bar is not None:
                    yield _Record(minute_bar["ts"], minute_bar["o"],
                                  minute_bar["h"], minute_bar["l"], px,
                                  minute_bar["v"], "ohlcv-1m")
                minute_bar = {"ts": minute_start_ns, "o": px, "h": hi,
                              "l": lo, "v": vol}
            else:
                minute_bar["h"] = max(minute_bar["h"], hi)
                minute_bar["l"] = min(minute_bar["l"], lo)
                minute_bar["v"] += vol
            time.sleep(max(0.0, 1.0 - (time.time() - now)))
