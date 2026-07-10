# gevent cooperative multitasking — MUST be the very first thing that runs so
# the feed's OS-thread stream loop, queue.Queue, and the gevent SSE greenlets
# interoperate cooperatively (spec §5.4.3, plan Task 7/12).
import gevent.monkey
gevent.monkey.patch_all()

"""
MULTICHART — realtime multi-quadrant market charting web app

Backend (Flask) streams live 1-minute bars (market-data vendor, or a synthetic
demo feed when no key is set) into a per-ticker base (TickerFeed), derives
per-(ticker,tf) snapshots (TimeframeView) and fans them out over a single
multiplexed SSE stream with a two-tier (hot/cold) live-update path. Frontend
renders candles + MAs/VWAPs/prior-period structural levels on a 2x2 grid of
independent quadrants.

Endpoints:
  GET  /                       → grid page
  GET  /quadrant?ticker&tf     → single-chart pop-out page
  GET  /api/init/<ticker>/<tf> → initial snapshot (bars + levels + dividers + segments)
  GET  /api/history/<t>/<tf>   → lazy deep-history chunks for scroll-back
  POST /api/subscribe          → declare a client's active (ticker,tf) topic set
  GET  /api/stream             → single multiplexed SSE per page (topic-tagged frames)

Usage:
  DEMO=1 python3 server.py --tickers NQ ES --port 8010     # no vendor account
  DATABENTO_API_KEY=... python3 server.py                  # live data
"""
# NB: no `from __future__ import annotations` — it must precede all code, but the
# gevent monkey-patch must run first (plan Task 12). Python 3.12 supports the
# `X | None` annotation syntax natively, so the future import is unnecessary.
import argparse
import itertools
import json
import os
import queue as _queue
import re
import threading
import time
import uuid
from json import dumps as json_dumps  # ensure json available in stream gen
from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util

import numpy as np
import pandas as pd
try:
    import databento as db
except ImportError:          # demo mode runs without the vendor SDK
    db = None
from flask import Flask, jsonify, request, Response

import chart_compute as cc
import local_history

import features as mod
import demo_feed

# Market-data vendor key (Databento). With no key set the app boots in DEMO
# mode: synthetic history + synthetic live stream (see demo_feed.py).
API_KEY = os.environ.get("DATABENTO_API_KEY", "")
DEMO_MODE = os.environ.get("DEMO", "").strip().lower() in ("1", "true", "yes") or not API_KEY
DATASET = "GLBX.MDP3"
ET = "America/New_York"

def _aggregate(df_1m: pd.DataFrame, bar_min: int) -> pd.DataFrame:
    """Aggregate 1-minute bars into N-minute bars anchored at session boundary.
    For tf >= 30m, anchor at 18:00 ET (futures session) so each bucket aligns
    with the trading day's start. For tf < 30m, anchor at minute 0 of day."""
    if bar_min == 1:
        return df_1m
    df = df_1m.copy()
    df = df.set_index("ts").sort_index()
    # Use closed='left', label='left' so bar timestamp = start of period
    # Offset by 0h so periods snap to UTC midnight; ET offset handled separately.
    rule = f"{bar_min}min"
    agg = df.resample(rule, label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum",
    }).dropna(subset=["open"]).reset_index()
    return agg


def _symbol(ticker: str) -> str:
    """Default Databento continuous symbol (calendar front-month).
    Using full-size contracts (NQ, ES, etc.) — the author's preference 2026-05-17.
    GC is resolved separately to a pinned highest-volume E-micro contract (see
    _highest_volume_contract) so the gold chart matches the author's pinned /MGC[Qxx]
    and never shows the price STEP the rolling .v.0 continuous leaves at a roll."""
    return f"{ticker}.c.0"

# CME futures month codes (Jan..Dec). Used to enumerate outright contracts.
_MONTH_CODES = "FGHJKMNQUVXZ"


def _candidate_contracts(root: str, n: int = 8) -> list[str]:
    """Next ~n outright contract symbols from today, e.g. MGC -> ['MGCM6',
    'MGCN6','MGCQ6',...]. Covers every calendar month; the volume probe discards
    the non-listed / illiquid ones (gold only lists G/J/M/Q/V/Z, etc.)."""
    now = datetime.now(timezone.utc)
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        out.append(f"{root}{_MONTH_CODES[m - 1]}{y % 10}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _highest_volume_contract(client, root: str, lookback_h: int = 3):
    """Probe candidate outright contracts and return (raw_symbol, volume) for the
    one with the most recent volume — i.e. the actively-traded contract to pin to.
    Returns (None, 0) if none have volume so the caller can fall back. This is the
    'highest-volume check' that keeps the pin auto-rolling to the right month."""
    end = datetime.now(timezone.utc) - timedelta(minutes=30)
    start = end - timedelta(hours=lookback_h)
    best, best_vol = None, -1
    for sym in _candidate_contracts(root):
        try:
            d = client.timeseries.get_range(
                dataset=DATASET, symbols=[sym], stype_in="raw_symbol",
                schema="ohlcv-1m", start=start, end=end).to_df()
            v = int(d["volume"].sum()) if len(d) else 0
        except Exception:
            v = 0
        if v > best_vol:
            best_vol, best = v, sym
    return (best, best_vol) if best_vol > 0 else (None, 0)


def _contract_label(symbol: str, stype: str) -> str:
    """TOS-style label for the resolved Databento symbol, shown on the chart so
    the author can see WHICH contract is live (and catch a stale/mis-rolled feed at a
    glance). A pinned outright 'NQU6' -> '/NQ[U26]', 'MGCQ6' -> '/MGC[Q26]'
    (matches his /MGC[Qxx] convention). A continuous symbol 'CL.c.0' -> '/CL'.
    The 1-digit Databento year is expanded to 2 digits against the current decade
    (near-dated contracts), rolling to the next decade for low digits."""
    if stype == "raw_symbol" and len(symbol) >= 3 and symbol[-2] in _MONTH_CODES \
            and symbol[-1].isdigit():
        root, mc, yd = symbol[:-2], symbol[-2], int(symbol[-1])
        cur = datetime.now(timezone.utc).year
        y = (cur // 10) * 10 + yd
        if y < cur - 1:                 # e.g. digit 0 seen in 2026 -> 2030, not 2020
            y += 10
        return f"/{root}[{mc}{y % 100:02d}]"
    return f"/{symbol.split('.')[0]}"   # continuous / unparseable -> plain root


PERSIST_DIR = Path("/tmp/multichart_cache")
PERSIST_DIR.mkdir(parents=True, exist_ok=True)

# Tickers whose candles are pinned to an outright contract but whose prior-period
# LEVELS must come from the volume-continuous series to match TOS (carry-spread /
# back-month-liquidity reasons). See _refresh_continuous_levels + memory
# feedback_tos_level_basis_continuous. All the PIN_ROOTS tickers qualify.
CONT_LEVEL_TICKERS = {"GC", "NQ", "ES", "YM", "RTY", "CL"}





# ===== Per-ticker live feed (1-minute base) =====
class TickerFeed:
    """Owns a single 1-minute Databento base stream for one ticker (historical
    seed + live 1m/1s + disk cache). Holds the 1-minute base df; per-tf
    TimeframeViews resample from it. Started for all 7 tickers at boot."""
    def __init__(self, ticker: str):
        self.ticker = ticker
        # Databento symbol + stype used for BOTH history load and live stream.
        # Default = calendar-front continuous; GC is refined to a pinned highest-
        # volume E-micro (MGC) contract during init_history (resolved once there).
        self.symbol = _symbol(ticker)
        self.stype = "continuous"
        # Gold only: prior-period levels (PD/PW/PM) come from the volume-CONTINUOUS
        # series, not the pinned MGCQ6 contract (whose older months carry a spread
        # and give a wrong PML/PWL). Matches TOS. {} for non-gold / until loaded.
        self.cont_levels: dict = {}
        self.cont_levels_day = None
        # Prior 2-3 weeks/months ago H/L (2WH/2WL/3WH/3WL/2MH/2ML/3MH/3ML). Needs
        # more than the 60d live base, so computed from the archive + cached here,
        # refreshed daily. {} until first refresh.
        self.hist_levels: dict = {}
        self.hist_levels_day = None
        self.lock = threading.Lock()
        self.df: pd.DataFrame = pd.DataFrame()
        self.last_bar_ts: int = 0
        self.last_update: float = 0
        self.ready = False
        self.state = "ok"                      # 'ok'/'degraded'/'down' (Task 16)
        # Derived per-tf views + a single per-feed cold-path worker.
        self.views: dict[str, "TimeframeView"] = {}
        self.recompute_q: _queue.Queue = _queue.Queue()
        self._worker_started = False

    @property
    def cache_path(self) -> Path:
        """Persisted-1m-base cache path, KEYED BY THE RESOLVED CONTRACT
        (self.symbol) — not just the ticker. A roll / re-pin (e.g. NQ M6->U6)
        changes self.symbol, so the new contract reads a FRESH file instead of
        merging the prior contract's bars into the new series. Without this, the
        boot cache-merge + the live stream's 20-min replay overlap blend two
        contracts into the same minutes (June open/low + Sept high/close = ~300 pt
        straddle candles). Sanitize '.' for continuous symbols (CL.c.0 -> CL_c_0)."""
        safe = self.symbol.replace(".", "_")
        return PERSIST_DIR / f"{self.ticker}_{safe}_1m_bars.parquet"

    @property
    def contract_label(self) -> str:
        """TOS-style label of the live contract (e.g. '/NQ[U26]') for display."""
        return _contract_label(self.symbol, self.stype)

    def view(self, tf: str) -> "TimeframeView":
        """Return (lazily create) the TimeframeView for ``tf``.
        First creation does an initial recompute so a snapshot exists."""
        with self.lock:
            v = self.views.get(tf)
            if v is None:
                v = TimeframeView(self, tf)
                self.views[tf] = v
                new = True
            else:
                new = False
        if new:
            v.recompute()
        return v

    def start_worker(self):
        """Start the per-feed cold-path recompute daemon worker once."""
        if self._worker_started:
            return
        self._worker_started = True
        threading.Thread(target=self._recompute_worker, daemon=True,
                         name=f"recompute-{self.ticker}").start()

    def _recompute_worker(self):
        """COLD PATH driver — pops base-bar-close events and recomputes every
        view off the stream/SSE threads (spec §5.4.2)."""
        while True:
            origin_ms = self.recompute_q.get()
            # coalesce: if more closes queued, keep only the latest origin
            while not self.recompute_q.empty():
                try:
                    origin_ms = self.recompute_q.get_nowait()
                except _queue.Empty:
                    break
            for view in list(self.views.values()):
                try:
                    view.recompute(origin_ms)
                except Exception as e:
                    print(f"[{self.ticker}/{view.tf}] recompute err: {e}", flush=True)

    def _refresh_continuous_levels(self):
        """GC + index futures: recompute ALL prior-period reference levels from
        a STITCHED continuous series so each period reads the contract that was
        active then, exactly like TOS: old months from the volume-continuous,
        recent days from the pinned contract. The chart candles stay pinned.

        Index-futures note (verified NQ 2026-06-15, May PMH 30536 / PMC 30389.5 /
        PDH 29760 / PDL 29230 to the tick): there is a persistent ~300pt M6->U6
        carry spread, so the pinned outright (NQU6) never matches the continuous
        (M6) within $3 and the stitch resolves to PURE continuous — which is what
        TOS draws under the U6 candles. Because the spread offsets the pinned df
        UNIFORMLY across all recent days, the 2-5 day / 2-3 week magnets must ALSO
        come from the continuous here (unlike GC, where micro==continuous so only
        month/week needed it). Close-dated `_ym` (compute_features) excludes the
        Sunday-evening session from the prior month — matches TOS. See memory
        feedback_tos_level_basis_continuous.

        The roll point is found automatically: continuous and MGCQ6 prices are
        identical AFTER the roll and diverge by the carry spread BEFORE it, so the
        last timestamp where they disagree by >$3 is the roll. Stitch continuous
        up to there + MGCQ6 after. Self-rolls each contract; no hardcoded date.

        Called at boot and once per day (levels only move at period boundaries).
        Cached in self.cont_levels (keyed PMH/PML/.../PDC)."""
        try:
            client = db.Historical(key=API_KEY)
            end = datetime.now(timezone.utc) - timedelta(minutes=30)
            start = end - timedelta(days=70)        # >2 months → full prior month
            vol_cont = "MGC.v.0" if self.ticker == "GC" else f"{self.ticker}.c.0"
            d = client.timeseries.get_range(
                dataset=DATASET, symbols=[vol_cont], stype_in="continuous",
                schema="ohlcv-1m", start=start, end=end).to_df()
            if d.index.name == "ts_event":
                d = d.reset_index().rename(columns={"ts_event": "ts"})
            elif "ts_event" in d.columns:
                d = d.rename(columns={"ts_event": "ts"})
            d["ts"] = pd.to_datetime(d["ts"], utc=True)
            cols = ["ts", "open", "high", "low", "close", "volume"]
            d = cc.assign_bins(d.sort_values("ts").reset_index(drop=True))
            # Stitch with the pinned MGCQ6 base (post-roll) at the auto-detected
            # roll, classified PER SESSION so intraday roll churn (the continuous
            # flipping June<->Aug minute-by-minute on roll days) can't skew it: a
            # session is post-roll only if its MEDIAN continuous-vs-MGCQ6 gap ~0.
            with self.lock:
                mgc = self.df.copy() if self.df is not None and len(self.df) else None
            if mgc is not None and len(mgc) and "session_date" in mgc:
                cmp = d[["ts", "session_date", "close"]].merge(
                    mgc[["ts", "close"]].rename(columns={"close": "m"}),
                    on="ts", how="inner")
                gap = cmp.groupby("session_date").apply(
                    lambda g: (g["close"] - g["m"]).abs().median())
                post = gap[gap < 3.0].index
                # VOLUME-roll fallback (index futures during the quarterly roll window):
                # TOS rolls by VOLUME ~4 days before expiry, but Databento's .c.0/.v.0
                # continuous lags to the CALENDAR roll, so the pinned outright (e.g. U6)
                # sits a full carry-spread (~300pt NQ) from the continuous (M6) and the
                # price-agreement test above never fires -> levels wrongly stay on the
                # OLD month for recent sessions (yesterday/this-week ~300 below the U6
                # candles, off-screen). Detect the roll by volume instead: the pinned
                # contract owns every session from the point it leads volume, CONTIGUOUS
                # to the latest, so PD/PW/CW levels read the ACTUAL traded contract and
                # sit on the candles exactly like TOS. (Pre-roll sessions stay continuous.)
                if not len(post) and "volume" in mgc.columns and "volume" in d.columns:
                    vp = mgc.groupby("session_date")["volume"].sum()
                    vc = d.groupby("session_date")["volume"].sum()
                    sess_all = sorted(set(vp.index) | set(vc.index))
                    roll_sd = None
                    for s in reversed(sess_all):           # walk back while pinned still leads
                        if float(vp.get(s, 0)) > float(vc.get(s, 0)):
                            roll_sd = s
                        else:
                            break
                    if roll_sd is not None:
                        post = pd.Index([s for s in sess_all if s >= roll_sd])
                        print(f"[{self.ticker}] volume-roll detected: pinned owns sessions >= {roll_sd}", flush=True)
                if len(post):
                    roll = post.min()                     # first session the pinned contract took over
                    stitched = pd.concat(
                        [d[d["session_date"] < roll][cols + ["session_date"]],
                         mgc[mgc["session_date"] >= roll][cols + ["session_date"]]],
                        ignore_index=True).drop_duplicates(
                        subset=["ts"], keep="last").sort_values("ts")
                else:
                    stitched = d
            else:
                stitched = d
            stitched = cc.assign_bins(stitched[cols].reset_index(drop=True))
            stitched = mod.compute_features(stitched)
            rec = stitched.iloc[-1]
            lv = {}
            for k in ("pmh", "pml", "pmc", "pwh", "pwl", "pwc", "pdh", "pdl", "pdc"):
                if k in rec and pd.notna(rec[k]):
                    lv[k.upper()] = float(rec[k])
            # 2-5 day + 2-3 week magnets from the SAME continuous series (carry-spread
            # consistency for index futures; harmless for GC). Guarded so a missing
            # column can never abort the PD/PW/PM refresh.
            try:
                lv.update(_multi_day_levels(stitched))
                lv.update(_multi_period_levels(stitched, "iso_week_key", "W"))
                # CURRENT week+month H/L (CWH/CWL/CMH/CML) from the SAME stitched
                # continuous, so they read the real front-month extreme (not the thin
                # off-front pinned outright). Locked at last close; daily refresh.
                lv.update(_current_period_levels(stitched))
            except Exception as _e:
                print(f"[{self.ticker}] cont multi-day/week skipped: {_e!r}", flush=True)
            if lv:
                self.cont_levels = lv
                self.cont_levels_day = rec.get("session_date")
                print(f"[{self.ticker}] stitched levels: PML={lv.get('PML')} "
                      f"PWL={lv.get('PWL')} PDL={lv.get('PDL')}", flush=True)
        except Exception as e:
            print(f"[{self.ticker}] continuous-levels refresh failed: {e}", flush=True)

    def _refresh_history_levels(self):
        """Prior 2-3 MONTHS-ago H/L (2MH/2ML/3MH/3ML). The 60d live base can't reach
        back that far, so read ~130 days from the archive and pick each target
        calendar month's H/L by ABSOLUTE year-month (anchored to NOW, so a stale
        archive tail can't shift which month is which). Cached in self.hist_levels."""
        try:
            now_et = pd.Timestamp.now(tz=ET)
            def ym_minus(k):                       # year-month k calendar months before now
                y, m = now_et.year, now_et.month - k
                while m <= 0:
                    m += 12; y -= 1
                return y * 100 + m
            targets = {2: ym_minus(2), 3: ym_minus(3)}
            end = datetime.now(timezone.utc) - timedelta(minutes=30)
            start = end - timedelta(days=130)      # current + ~4 prior months
            df = local_history.load_range(self.ticker, start, end)
            if df is None or len(df) == 0:
                return
            df = cc.assign_bins(df.sort_values("ts").reset_index(drop=True))
            lv = {}
            for k, ym in targets.items():
                sub = df[df["_ym"] == ym]
                if len(sub):
                    lv[f"{k}MH"] = float(sub["high"].max())
                    lv[f"{k}ML"] = float(sub["low"].min())
            if lv:
                self.hist_levels = lv
                print(f"[{self.ticker}] history levels: 2M H/L={lv.get('2MH')}/{lv.get('2ML')} "
                      f"3M H/L={lv.get('3MH')}/{lv.get('3ML')}", flush=True)
        except Exception as e:
            print(f"[{self.ticker}] history-levels refresh failed: {e!r}", flush=True)

    def init_history(self):
        # Feed is timeframe-agnostic: always load + hold the 1-MINUTE base.
        # Views resample on demand. Use the 1m spec's history span.
        hist_days = cc.BUCKET_SPEC["1m"]["history_days"]
        if DEMO_MODE:
            print(f"[{self.ticker}] DEMO mode — synthetic {hist_days}d history", flush=True)
            base = demo_feed.synthetic_history(self.ticker, days=hist_days)
            with self.lock:
                self.df = cc.assign_bins(base)
                self.last_bar_ts = int(self.df["ts"].iloc[-1].value)
                self.last_update = time.time()
            self.symbol, self.stype = f"{self.ticker}.demo", "demo"
            self.ready = True
            return
        print(f"[{self.ticker}] loading {hist_days}d 1m history...", flush=True)
        end = datetime.now(timezone.utc) - timedelta(minutes=30)
        start = end - timedelta(days=hist_days)
        client = db.Historical(key=API_KEY)
        # Pin to the single highest-volume OUTRIGHT contract found by a live volume
        # probe, so the chart matches the author's TOS exactly. TOS /NQ etc. roll by
        # VOLUME, so near a quarterly expiry they jump to the next contract
        # (e.g. M6->U6) days before the calendar .c.0 continuous does — that roll-
        # timing gap is the ~300 pt M6/U6 carry spread the author saw between TOS and the
        # chart. The probe auto-rolls with TOS every quarter (no hand-editing).
        # Index futures use the FULL-size root (the author's preference 2026-05-17); gold
        # uses the E-micro MGC root (his pinned /MGC[Qxx]); crude uses full CL and
        # auto-rolls to its MONTHLY front (e.g. CLN6 = /CL[N26]). On an empty probe
        # (Databento outage) fall back to the calendar-front continuous.
        PIN_ROOTS = {"GC": "MGC", "NQ": "NQ", "ES": "ES", "YM": "YM", "RTY": "RTY",
                     "CL": "CL"}
        if self.ticker in PIN_ROOTS:
            root = PIN_ROOTS[self.ticker]
            pinned, vol = _highest_volume_contract(client, root)
            if pinned:
                self.symbol, self.stype = pinned, "raw_symbol"
                print(f"[{self.ticker}] pinned to {pinned} "
                      f"(highest {root} volume {vol:,}/3h)", flush=True)
            elif self.ticker == "GC":
                self.symbol, self.stype = "GC.v.0", "continuous"
                print(f"[{self.ticker}] MGC volume probe empty — "
                      f"falling back to GC.v.0", flush=True)
            else:
                # Index futures keep the .c.0 calendar continuous default set in
                # __init__ (and the Sunday-open fill below still applies to it).
                print(f"[{self.ticker}] {root} volume probe empty — staying on "
                      f"{self.symbol} continuous", flush=True)
        # Primary load — retry transient gateway errors (Databento 504s) so one
        # blip at boot doesn't crash all 7 feeds. Factored so the local-seed path
        # below can re-use the exact same fetch+normalize for the gap (and for a
        # full reload if the local seed fails its overlap check).
        MAX_FETCH_RETRIES = 6                 # transient 504/timeout retries (~63s)
        def _fetch_db(fstart, fend=None):
            data = None
            cur_end = fend or end
            attempt = 0
            while attempt < MAX_FETCH_RETRIES:
                try:
                    data = client.timeseries.get_range(
                        dataset=DATASET, symbols=[self.symbol],
                        stype_in=self.stype, schema="ohlcv-1m",
                        start=fstart, end=cur_end,
                    )
                    break
                except Exception as e:
                    # Databento availability lags real time; if our `end` sits past
                    # the available range it 422s with the available-up-to ts in the
                    # message. Retrying the SAME end never recovers, so parse that ts
                    # and clamp `end` to just inside it. This correction is FREE — it
                    # must not eat into the transient-error retry budget below.
                    msg = str(e)
                    if "data_end_after_available_end" in msg:
                        m = re.search(r"available up to '([^']+)'", msg)
                        if m:
                            try:
                                avail = pd.to_datetime(m.group(1), utc=True).to_pydatetime()
                                clamped = avail - timedelta(minutes=1)
                                if clamped < cur_end:
                                    cur_end = clamped
                                    print(f"[{self.ticker}] clamped end to "
                                          f"available range {cur_end}", flush=True)
                                    continue   # free retry — does NOT consume budget
                            except Exception:
                                pass
                    attempt += 1
                    if attempt >= MAX_FETCH_RETRIES:
                        raise
                    wait = 2 ** (attempt - 1)
                    print(f"[{self.ticker}] primary fetch retry "
                          f"{attempt}/{MAX_FETCH_RETRIES} after {e!r}; "
                          f"sleep {wait}s", flush=True)
                    time.sleep(wait)
            if data is None:
                raise RuntimeError(f"[{self.ticker}] fetch exhausted retries with no data")
            d = data.to_df()
            if d.index.name == "ts_event":
                d = d.reset_index().rename(columns={"ts_event": "ts"})
            elif "ts_event" in d.columns:
                d = d.rename(columns={"ts_event": "ts"})
            d["ts"] = pd.to_datetime(d["ts"], utc=True)
            return d.sort_values("ts").reset_index(drop=True)

        # ---- Local-archive seed (Phase 1) -------------------------------------
        # Seed the bulk of the window from the on-disk 1-minute archive
        # ($ARCHIVE_DIR/{TICKER}_1m, e.g. back to 2010) so boot stops re-pulling ~60d
        # from Databento every time — only the recent last_local→now gap is
        # fetched. Skipped for non-continuous symbols (GC = pinned micro contract),
        # and reconcile() drops the seed if its overlap with Databento disagrees
        # (the generic provenance guard). The archive already carries the Sunday
        # 18:00-20:00 ET open bars, so the front-month fill below is likewise only
        # needed over the Databento gap when the seed is trusted.
        # GC is never seeded from the archive: its correct series is the pinned
        # highest-volume MGC micro (or GC.v.0 on probe-empty fallback), and the
        # gold archive's deep-history provenance (.v.0 vs .c.0, micro vs full) is
        # special-cased elsewhere. Always take GC's unchanged Databento path.
        NO_LOCAL_SEED = {"GC"}
        OVERLAP = timedelta(hours=6)            # fetched into local region to validate
        local_df = None
        fetch_start = start
        if self.ticker not in NO_LOCAL_SEED and self.stype == "continuous":
            # A malformed/unreadable archive partition must NOT take down the feed:
            # degrade to the full Databento pull (the pre-seed behavior) and say so.
            try:
                seed = local_history.load_range(self.ticker, start, end)
            except Exception as e:
                seed = None
                print(f"[{self.ticker}] local seed failed ({e!r}) — "
                      f"full Databento pull", flush=True)
            if seed is not None and len(seed) > 1000:
                local_df = seed
                fetch_start = max(start, seed["ts"].iloc[-1] - OVERLAP)
                print(f"[{self.ticker}] local seed {len(seed):,} bars "
                      f"({seed['ts'].iloc[0]} → {seed['ts'].iloc[-1]}); "
                      f"Databento gap from {fetch_start}", flush=True)
        try:
            df = _fetch_db(fetch_start)
        except Exception as e:
            # Databento unreachable for the recent gap (504/timeout/etc). If we
            # already loaded an on-disk seed, BOOT ON IT rather than crashing the
            # whole worker — a transient Databento blip must not take down all 7
            # feeds when the data is already local. The live stream replays the
            # gap forward from the seed's last bar once it connects.
            if local_df is None:
                raise
            print(f"[{self.ticker}] Databento gap fetch FAILED ({e!r}) — "
                  f"booting on local seed alone; live stream will catch up",
                  flush=True)
            df = local_df.copy()
            local_df = None                      # skip reconcile/backfill (no fresh df)
        fill_start = start                       # front-month fill window (below)
        if local_df is not None:
            df, used_local = local_history.reconcile(local_df, df)
            if used_local:
                fill_start = fetch_start
                print(f"[{self.ticker}] merged local seed → {len(df):,} bars "
                      f"(skipped ~{(fetch_start - start).days}d Databento pull)",
                      flush=True)
                # The archive can have a truncated month (incomplete download) that
                # would punch a multi-day hole the old full-Databento pull didn't
                # have. Backfill any such hole from Databento so completeness is
                # preserved; widen the front-month Sunday fill to cover it too.
                spans = local_history.missing_spans(df)
                for gs, ge in spans:
                    patch = _fetch_db(gs, ge)
                    df = (pd.concat([df, patch], ignore_index=True)
                            .drop_duplicates(subset=["ts"], keep="last")
                            .sort_values("ts").reset_index(drop=True))
                    print(f"[{self.ticker}] backfilled archive hole {gs} → {ge} "
                          f"(+{len(patch):,} bars)", flush=True)
                if spans:
                    # Sunday-fill must cover the backfilled holes + the recent gap,
                    # but not the early seeded region (archive already has its
                    # Sunday opens — verified). Start the fill at the earliest hole.
                    fill_start = min(fetch_start, min(gs for gs, _ in spans))
            else:
                print(f"[{self.ticker}] local seed REJECTED (overlap mismatch) — "
                      f"full Databento reload", flush=True)
                df = _fetch_db(start)
        # Secondary fill — continuous .c.0 drops the Sunday-open ET bars
        # (Sun 18:00-20:00) every week. Pull the explicit front-month contract
        # over the FULL history window and gap-fill those missing bars so every
        # session opens at 18:00 ET (not 20:00). Within the current front-month
        # period .c.0 == the raw contract, so we keep .c.0 on any overlap and add
        # the raw bars only where .c.0 has none (the Sunday gaps).
        # Raw contract used ONLY to backfill the Sunday 18:00-20:00 ET open that the
        # continuous symbol drops. It MUST match the contract .c.0 tracks at that
        # timestamp, or the fill injects a wrong-priced bar (a carry-spread spike) at
        # the Sunday open. NOTE: index futures are normally VOLUME-PINNED above (a
        # raw_symbol whose own series carries the Sunday bars), so this fill applies
        # to them ONLY in the .c.0 continuous fallback (empty volume probe). In that
        # fallback the contract must equal whatever .c.0 tracks RIGHT NOW — the
        # CALENDAR front month, which rolls quarterly (H/M/U/Z) at expiry, ~4 days
        # AFTER TOS's volume roll. As of 2026-06-15 .c.0 is still on M6 (rolls to U6
        # at the Jun-19 M6 expiry); bump this to U6 then if the fallback is ever hit.
        FRONT_MONTH = {"NQ": "NQM6", "ES": "ESM6", "YM": "YMM6", "RTY": "RTYM6"}
        # GC and CL roll WITHIN the history window — GC by active month
        # (G/J/M/Q/V/Z; .v.0 rolled GCM6->GCQ6 ~late May) and CL monthly
        # (CLM6 expired -> CLN6). No single fixed raw contract matches the continuous
        # series on both sides of that roll, so any fill bar lands at the wrong
        # contract's price and spikes the Sunday open. Skip the fill for these (the
        # .c.0 FALLBACK path only — both are normally volume-pinned above); the
        # continuous symbol's own bars are correct (the Sunday session may just start
        # ~20:00 ET instead of 18:00 ET — honest, no fake spike).
        ROLLING_TICKERS = {"GC", "CL"}
        # Only the .c.0 continuous series drops the Sunday open; a volume-pinned
        # raw_symbol (index futures normal path, gold) already carries its own Sunday
        # bars, so the fill is a no-op there — gate it to the continuous case.
        raw_sym = (FRONT_MONTH.get(self.ticker)
                   if self.stype == "continuous" and self.ticker not in ROLLING_TICKERS
                   else None)
        if raw_sym:
            try:
                # fill_start == start for a full pull; == the Databento gap start
                # when a local seed was trusted (archive already has older Sundays).
                fill = client.timeseries.get_range(
                    dataset=DATASET, symbols=[raw_sym], stype_in="raw_symbol",
                    schema="ohlcv-1m", start=fill_start, end=end,
                ).to_df()
                if len(fill) > 0:
                    if fill.index.name == "ts_event":
                        fill = fill.reset_index().rename(columns={"ts_event": "ts"})
                    elif "ts_event" in fill.columns:
                        fill = fill.rename(columns={"ts_event": "ts"})
                    fill["ts"] = pd.to_datetime(fill["ts"], utc=True)
                    fill_only = fill[["ts","open","high","low","close","volume"]]
                    df_only = df[["ts","open","high","low","close","volume"]]
                    before = len(df_only)
                    # .c.0 wins on overlap (keep="last" + .c.0 concatenated last);
                    # raw bars only fill timestamps .c.0 is missing.
                    df = pd.concat([fill_only, df_only], ignore_index=True) \
                           .drop_duplicates(subset=["ts"], keep="last") \
                           .sort_values("ts").reset_index(drop=True)
                    print(f"[{self.ticker}] {raw_sym} gap-fill: +{len(df)-before:,} bars "
                          f"(fetched {len(fill):,})", flush=True)
            except Exception as e:
                print(f"[{self.ticker}] fill skipped: {e}", flush=True)
        # Merge with previously-persisted live 1m bars (survive restarts). Keyed by
        # contract (self.cache_path), so a roll/re-pin starts from a fresh file
        # rather than blending the prior contract's bars into the new series.
        cache_path = self.cache_path
        if cache_path.exists():
            try:
                cached = pd.read_parquet(cache_path)
                cached["ts"] = pd.to_datetime(cached["ts"], utc=True)
                df = pd.concat([df, cached], ignore_index=True) \
                       .drop_duplicates(subset=["ts"], keep="last") \
                       .sort_values("ts").reset_index(drop=True)
                print(f"[{self.ticker}] merged {len(cached):,} cached bars", flush=True)
            except Exception as e:
                print(f"[{self.ticker}] cache merge failed: {e}", flush=True)
        # Keep 1-MINUTE base; views resample on demand (plan Task 7 Step 1).
        df = cc.assign_bins(df)
        with self.lock:
            self.df = df                       # 1m base, bins assigned
            self.last_bar_ts = int(df["ts"].iloc[-1].value)
            self.ready = True
        print(f"[{self.ticker}] history ready ({len(df):,} 1m bars total)", flush=True)
        # GC + index futures: seed prior-period levels from the continuous series
        # (see method) so they match TOS despite the pinned-contract candles.
        if self.ticker in CONT_LEVEL_TICKERS:
            self._refresh_continuous_levels()

    def _connect_live(self):
        """Connect the vendor live client; subscribe 1m+1s with gap replay."""
        live = db.Live(key=API_KEY)
        # Replay only the gap between the last loaded history bar and now (with a
        # 20-min overlap for dedup), NOT the full 00:00-UTC day. init_history
        # already fills intraday holes via the Historical + front-month gap-fill,
        # so a full-day replay is unnecessary — and after a long downtime (e.g. an
        # overnight sleep) that 1s backlog is huge and the heavy per-bar work
        # (assign_bins + parquet + recompute) prevents the stream from ever
        # reaching the live edge. Clamp to >= 00:00 UTC (Live's earliest).
        today_utc_midnight_ns = int(datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp() * 1_000_000_000)
        try:
            with self.lock:
                hist_last_ns = int(self.df["ts"].iloc[-1].value)
        except Exception:
            hist_last_ns = today_utc_midnight_ns
        overlap_ns = 20 * 60 * 1_000_000_000   # 20-min dedup overlap
        start_ns = max(today_utc_midnight_ns, hist_last_ns - overlap_ns)
        # Use the SAME symbol/stype the history load resolved (GC = pinned MGC
        # contract) so live bars continue the exact series with no contract switch.
        kwargs = dict(dataset=DATASET, schema="ohlcv-1m",
                       symbols=[self.symbol], stype_in=self.stype,
                       start=start_ns)
        live.subscribe(**kwargs)
        kwargs_1s = {**kwargs, "schema": "ohlcv-1s"}
        live.subscribe(**kwargs_1s)
        from datetime import datetime as _dt
        print(f"[{self.ticker}] subscribed (replay from {_dt.fromtimestamp(start_ns/1e9, tz=timezone.utc)})", flush=True)
        return live

    def stream(self):
        """Background loop: append closed 1-min bars + track in-flight bar via 1s.
        Subscribes Live with start=last_historical_ts so Databento replays the
        gap between historical end and 'now' before continuing in real time."""
        if DEMO_MODE:
            print(f"[{self.ticker}] demo stream starting...", flush=True)
            live = demo_feed.DemoLive(self.ticker, self.df)
        else:
            print(f"[{self.ticker}] live stream starting...", flush=True)
            live = self._connect_live()

        raw_cols = ["ts", "open", "high", "low", "close", "volume"]
        for record in live:
            if not hasattr(record, "open"): continue
            ts_ns = int(record.ts_event)
            # No age skip — we explicitly asked for replay. Dedupe by ts below.
            scale = 1e-9 if record.open > 1e8 else 1.0
            o = float(record.open) * scale; h = float(record.high) * scale
            l = float(record.low) * scale;  c = float(record.close) * scale
            v = int(record.volume)

            # Distinguish 1m vs 1s by record's rtype attribute (returns
            # schema string like 'ohlcv-1m' / 'ohlcv-1s').
            rtype = getattr(record, "rtype", None)
            is_1m = (str(rtype) == "ohlcv-1m")

            if is_1m:
                # ---- 1-minute close: maintain the 1m BASE, queue cold recompute ----
                minute_ts = pd.Timestamp((ts_ns // 60_000_000_000) * 60_000_000_000,
                                         unit="ns", tz="UTC")
                with self.lock:
                    df_raw = self.df[raw_cols].copy()
                existing = df_raw["ts"] == minute_ts
                if existing.any():
                    idx = df_raw.index[existing][0]
                    df_raw.at[idx, "high"] = max(df_raw.at[idx, "high"], h)
                    df_raw.at[idx, "low"] = min(df_raw.at[idx, "low"], l)
                    df_raw.at[idx, "close"] = c
                    df_raw.at[idx, "volume"] = df_raw.at[idx, "volume"] + v
                else:
                    df_raw = pd.concat([df_raw, pd.DataFrame([{
                        "ts": minute_ts, "open": o, "high": h, "low": l,
                        "close": c, "volume": v}])], ignore_index=True)
                df_raw = (df_raw.sort_values("ts")
                          .drop_duplicates(subset=["ts"], keep="last")
                          .reset_index(drop=True))
                # Retain the FULL seeded 1m base — higher-tf views (up to the 60d
                # load horizon) resample from it, so never truncate below the
                # loaded history. ~60d ≈ 86k 1m bars; keep a comfortable ceiling.
                keep = 100_000
                if len(df_raw) > keep:
                    df_raw = df_raw.iloc[-keep:].reset_index(drop=True)
                new_df = cc.assign_bins(df_raw)
                with self.lock:
                    self.df = new_df
                    self.last_bar_ts = int(new_df["ts"].iloc[-1].value)
                    self.last_update = time.time()
                # persist 1m base
                try:
                    recent = new_df[new_df["ts"] > new_df["ts"].iloc[-1] - pd.Timedelta(days=3)]
                    recent[raw_cols].to_parquet(self.cache_path)
                except Exception as e:
                    print(f"[{self.ticker}] persist err: {e}", flush=True)
                # Cold-path recompute off-thread (spec §5.4.2): queue, don't run inline.
                self.recompute_q.put(int(time.time() * 1000))
            else:
                # ---- 1-second sub-bar — HOT PATH (spec §5.4.1) ----
                origin_ms = int(time.time() * 1000)   # tick-origin stamp (§5.4.6)
                for view in list(self.views.values()):
                    bar_ns = view.cfg["bar_min"] * 60_000_000_000
                    b_start = (ts_ns // bar_ns) * bar_ns
                    cb = view.current_bar
                    if cb is None or cb["ts_ms"] != b_start // 1_000_000:
                        cb = {"ts_ms": b_start // 1_000_000, "open": o, "high": h,
                              "low": l, "close": c, "volume": v}
                    else:
                        cb = {**cb, "high": max(cb["high"], h), "low": min(cb["low"], l),
                              "close": c, "volume": cb["volume"] + v}
                    cb["origin_ms"] = origin_ms
                    view.current_bar = cb
                    publish_hot(self.ticker, view.tf, cb)   # Task 11
                self.last_update = time.time()


# ===== Flask app =====
app = Flask(__name__)
STATES: dict[str, "TickerFeed"] = {}


# ===== Multiplexed SSE pub/sub registry (spec §5.1, §5.4.4; plan Task 11) =====
SCHEMA_VERSION = 1
SUBSCRIBERS: dict[str, set[tuple[str, str]]] = {}   # client_id → {(ticker,tf)}
CLIENT_QUEUES: dict[str, _queue.Queue] = {}         # client_id → frame queue
SUB_LOCK = threading.Lock()
_SEQ = itertools.count(1)                            # process-wide monotonic seq


def _push(topic_t: str, topic_f: str, frame: dict):
    """Push a frame to every client subscribed to (topic_t, topic_f). O(clients)."""
    frame["schema_version"] = SCHEMA_VERSION
    frame["sequence_id"] = next(_SEQ)
    frame["ts"] = int(time.time() * 1000)            # send-stamp (transit only)
    with SUB_LOCK:
        targets = [CLIENT_QUEUES[c] for c, tops in SUBSCRIBERS.items()
                   if (topic_t, topic_f) in tops and c in CLIENT_QUEUES]
    for q in targets:
        try:
            q.put_nowait(frame)
        except _queue.Full:
            pass                                     # slow client — drop (latest wins on reconnect)


def publish_hot(ticker: str, tf: str, bar: dict):
    _push(ticker, tf, {"topic": {"ticker": ticker, "tf": tf}, "kind": "hot",
                       "bar": bar, "origin_ms": bar.get("origin_ms")})


def publish_cold(ticker: str, tf: str, snap: dict):
    _push(ticker, tf, {"topic": {"ticker": ticker, "tf": tf}, "kind": "cold",
                       "snap": snap, "snapshot_version": snap.get("snapshot_version"),
                       "origin_ms": snap.get("origin_ms")})


# ===== Per-(ticker,tf) derived view (cold path off-thread; plan Task 8) =====
class TimeframeView:
    """Derived, cached resample of a TickerFeed for one timeframe. Recomputed by
    the feed's background worker on each base-bar close (cold path). SSE readers
    only read self.snapshot (an immutable dict). Hot-path ticks update
    self.current_bar."""
    def __init__(self, feed: "TickerFeed", tf: str):
        self.feed = feed
        self.tf = tf
        self.cfg = cc.BUCKET_SPEC[tf]
        self.lock = threading.Lock()
        self.snapshot: dict | None = None       # immutable; atomic-swapped by recompute()
        self.current_bar: dict | None = None    # hot path
        self.version = 0                         # == snapshot_version (monotonic)

    def recompute(self, origin_ms: int | None = None):
        """COLD PATH — heavy. Runs on the feed's background worker, never on an
        SSE/request thread (spec §5.4.2)."""
        # Narrow the lock: grab a reference, copy OUTSIDE the lock so the hot path
        # is never blocked by this view's numpy work (Set-1 perf note).
        with self.feed.lock:
            base_ref = self.feed.df
        if base_ref is None or len(base_ref) == 0:
            return
        base = base_ref[["ts", "open", "high", "low", "close", "volume"]].copy()
        agg = cc.aggregate(base, self.cfg["bar_min"])
        agg = cc.assign_bins(agg)
        agg = mod.compute_features(agg)         # ema/sma/vwap + PD/PW/PM columns
        agg["ema96"] = agg["close"].ewm(span=96, adjust=False).mean()    # EMA-stack preset (96 EMA)
        agg["ema200"] = agg["close"].ewm(span=200, adjust=False).mean()  # EMA-stack preset (200 EMA — distinct from the 200 SMA)
        # Re-apply chart_compute's ET-wall-clock TOS-grid bins after the feature
        # pass so the level segments always match the dividers.
        agg = cc.assign_bins(agg)
        # Prior-MONTH levels (PMH/PML/PMC) must group by the FUTURES TRADING DAY's
        # month — the trading day runs 18:00 ET → 16:59 ET next day and is owned by
        # its OPEN (6 PM) date. compute_features groups by `_ym`, which assign_bins
        # derives from session_date = the CLOSE date (open + 1 day). That split the
        # trading day at midnight, so the 5/31-open session's high (the 6/1 13:43 ET
        # print, 30693) was wrongly excluded from May — PMH read the 5/29 level
        # instead (the author caught this 2026-06-10). The trading-day OPEN date =
        # session_date - 1 day; grouping the monthly H/L/C by its month captures the
        # whole 5/31-open session in May. (Weekly stays session-based — correct: the
        # trading WEEK genuinely opens Sun 18:00 ET.)
        if {"pmh", "pml", "pmc"}.issubset(agg.columns) and len(agg) and "session_date" in agg.columns:
            _open = pd.to_datetime(agg["session_date"]) - pd.Timedelta(days=1)   # 6PM trading-day open date
            agg["_ym_td"] = (_open.dt.year * 100 + _open.dt.month).to_numpy()
            _mon = (agg.groupby("_ym_td")
                       .agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
                       .sort_index())
            _mon["pmh"] = _mon["h"].shift(1)
            _mon["pml"] = _mon["l"].shift(1)
            _mon["pmc"] = _mon["c"].shift(1)
            agg = (agg.drop(columns=["pmh", "pml", "pmc"])
                      .merge(_mon[["pmh", "pml", "pmc"]], left_on="_ym_td",
                             right_index=True, how="left")
                      .sort_values("ts").reset_index(drop=True))
        n = self.cfg["display_bars"]
        view_df = agg.tail(n).reset_index(drop=True)
        # Day/week/month bucket H/L/C come from the 1-MINUTE base so the close is
        # the true session close (resampled higher-TF bars have a bar straddling
        # the 17:00-18:00 ET maintenance gap whose close is the reopen price).
        SESSION_B = ("1D", "1W", "1M")
        intraday_b = [b for b in self.cfg["buckets"] if b not in SESSION_B]
        session_b = [b for b in self.cfg["buckets"] if b in SESSION_B]
        segs = []; base_binned = None
        if intraday_b:
            segs += cc.bucket_segments(view_df, intraday_b)
            segs += cc.current_bucket_projection(view_df, intraday_b)
        if session_b:
            base_binned = cc.assign_bins(base)
            hlc = {lab: cc.bucket_hlc_lookup(base_binned, lab) for lab in session_b}
            segs += cc.bucket_segments(view_df, session_b, hlc_by_label=hlc)
            segs += cc.current_bucket_projection(view_df, session_b)
        # Prior-period reference levels. Gold uses the volume-CONTINUOUS series
        # (self.feed.cont_levels) so PML/PWL match TOS despite the pinned-contract
        # candles; refresh once per day (levels only move at period boundaries).
        if self.cfg["static"]:
            if self.feed.ticker in CONT_LEVEL_TICKERS and self.feed.cont_levels:
                snap_levels = dict(self.feed.cont_levels)
                cur_day = view_df["session_date"].iloc[-1] if "session_date" in view_df else None
                if not DEMO_MODE and cur_day is not None and cur_day != self.feed.cont_levels_day:
                    self.feed.cont_levels_day = cur_day   # claim now → one refresh
                    threading.Thread(target=self.feed._refresh_continuous_levels,
                                     daemon=True).start()
            else:
                snap_levels = _levels_payload(agg)
        else:
            snap_levels = {}
        # weekday 2-5 day levels (recent swing magnets). Skip the month-grid TFs
        # (1d/4h) — they use the clean annual month-grid look (month grid only, no
        # weekday H/L pills).
        snap_titles = {}
        if self.tf not in ("1d", "4h"):
            # For CONT_LEVEL_TICKERS the 2-5D / 2-3W magnets are already in
            # snap_levels (from the continuous series, via _refresh_continuous_levels);
            # the pinned-df agg values are carry-spread-offset, so don't clobber them.
            _use_cont = self.feed.ticker in CONT_LEVEL_TICKERS and self.feed.cont_levels
            if not _use_cont:
                snap_levels.update(_multi_day_levels(agg))
                # 2-3 WEEKS ago H/L: from the current agg (60d base covers ~8 weeks).
                snap_levels.update(_multi_period_levels(agg, "iso_week_key", "W"))
            snap_titles = _multi_day_titles(agg)        # weekday labels are contract-independent
            # 2-3 MONTHS ago H/L: need >60d, so from the archive — computed once and
            # cached per feed, refreshed daily (claim like the GC continuous levels).
            cur_day = view_df["session_date"].iloc[-1] if "session_date" in view_df else None
            if not DEMO_MODE and cur_day is not None and cur_day != self.feed.hist_levels_day:
                self.feed.hist_levels_day = cur_day
                threading.Thread(target=self.feed._refresh_history_levels, daemon=True).start()
            snap_levels.update(self.feed.hist_levels)
        # CURRENT week+month H/L (CWH/CWL/CMH/CML) arrive via cont_levels (computed
        # from the stitched CONTINUOUS in _refresh_continuous_levels — the real
        # front-month extreme, NOT the thin off-front pinned outright). Here we only
        # merge levels at the same price into ONE line with a COMBINED label (e.g.
        # 'YH·CWH'), instead of hiding the lower-TF one; combined titles flow through
        # level_titles so every TF's render path shows them (the author 2026-06-16).
        snap_levels, _combined_titles = _dedup_current_levels(snap_levels)
        snap_titles.update(_combined_titles)
        snap = {
            "timeframe": self.tf,
            "contract": self.feed.contract_label,   # '/NQ[U26]' — live contract label
            "default_zoom": self.cfg["default_zoom"],
            "bars": _bars_payload(agg, n, bar_min=self.cfg["bar_min"]),
            "levels": snap_levels,
            "level_titles": snap_titles,

            "segments": segs,
            "boundaries": cc.dividers(view_df, self.cfg["buckets"]),
            # 17:00-18:00 ET CME maintenance breaks (intraday only — 4H+ bars
            # aggregate over the gap, so a single bar already covers it).
            "maintenance_gaps": (cc.maintenance_gaps(view_df, self.cfg["bar_min"])
                                 if self.cfg["bar_min"] < 240 else []),
            "last_ts_ms": int(agg["ts"].iloc[-1].value // 1_000_000),
            "feed_state": self.feed.state,      # 'ok'/'degraded'/'down' (Task 16)
        }
        with self.lock:
            self.version += 1
            snap["snapshot_version"] = self.version
            snap["origin_ms"] = origin_ms if origin_ms is not None else int(time.time() * 1000)
            self.snapshot = snap                # atomic swap of an immutable dict (§5.5)
        publish_cold(self.feed.ticker, self.tf, snap)   # push, don't poll


def _bars_payload(df: pd.DataFrame, n: int, after_ts_ns: int | None = None,
                  bar_min: int = 1):
    if after_ts_ns is not None:
        sub = df[df["ts"].astype("int64") > after_ts_ns]
    else:
        sub = df.tail(n)
    rows = []
    for _, r in sub.iterrows():
        rows.append({
            "time": int(r["ts"].value // 1_000_000),  # ms epoch
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": int(r["volume"]),
            "ema20": float(r["ema20"]) if pd.notna(r["ema20"]) else None,
            "ema50": float(r["ema50"]) if pd.notna(r["ema50"]) else None,
            "ema96": float(r["ema96"]) if pd.notna(r["ema96"]) else None,
            "ema200": float(r["ema200"]) if pd.notna(r["ema200"]) else None,
            "sma200": float(r["sma200"]) if pd.notna(r["sma200"]) else None,
            "vwap_d": float(r["vwap_d"]) if pd.notna(r["vwap_d"]) else None,
            "vwap_w": float(r["vwap_w"]) if pd.notna(r["vwap_w"]) else None,
        })
    # On a full snapshot for intraday TFs, inject WhitespaceData slots
    # (just {"time": ms}) inside each maintenance gap so the 17:00 4H bucket
    # renders at a full 4-hour width with the missing hour as visible empty
    # space. 4H+ bars aggregate over the gap so no whitespace is needed.
    if after_ts_ns is None and bar_min < 240 and len(sub) > 0:
        gaps = cc.maintenance_gaps(sub, bar_min)
        bar_ms = bar_min * 60_000
        for g in gaps:
            t = g["start_ms"]
            while t < g["end_ms"]:
                rows.append({"time": t})
                t += bar_ms
        rows.sort(key=lambda r: r["time"])
    return rows


# ===== Deep-history scroll-back (to 2010) =====
# Served on demand from the local archive, OFF the live path: the live append /
# cold recompute never touch deep history, so live candles keep printing fast.
HIST_MAX_N = 2000
# Cache of Databento gap-fills (ticker, gap_start_ns, gap_end_ns) -> 1m df, so a
# truncated-month hole in the archive is fetched from Databento ONCE, not on every
# scroll back into that region.
_HIST_GAP_CACHE: dict = {}


def _f(x):
    """float() or None for NaN/None — for nullable indicator fields in JSON."""
    return float(x) if x is not None and pd.notna(x) else None


def _fetch_databento_1m(ticker: str, start, end) -> pd.DataFrame:
    """Fetch 1-minute bars [start, end] from Databento (continuous .c.0) to fill a
    hole in the local archive. Returns an empty frame on any failure."""
    cols = ["ts", "open", "high", "low", "close", "volume"]
    try:
        client = db.Historical(key=API_KEY)
        d = client.timeseries.get_range(
            dataset=DATASET, symbols=[_symbol(ticker)], stype_in="continuous",
            schema="ohlcv-1m", start=start, end=end).to_df()
        if d.index.name == "ts_event":
            d = d.reset_index().rename(columns={"ts_event": "ts"})
        elif "ts_event" in d.columns:
            d = d.rename(columns={"ts_event": "ts"})
        d["ts"] = pd.to_datetime(d["ts"], utc=True)
        return d[cols].sort_values("ts").reset_index(drop=True)
    except Exception as e:
        print(f"[{ticker}] history gap-fill failed: {e!r}", flush=True)
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in cols})


def _backfill_archive_gaps(ticker: str, df_1m: pd.DataFrame) -> pd.DataFrame:
    """Fill truncated-month holes (gaps > 3 days) in the archive 1m frame from
    Databento, so scroll-back history is as complete as the live boot path.
    Cached per gap so repeated scrolls don't re-fetch."""
    spans = local_history.missing_spans(df_1m)
    if not spans:
        return df_1m
    parts = [df_1m]
    for gs, ge in spans:
        key = (ticker, int(gs.value), int(ge.value))
        fill = _HIST_GAP_CACHE.get(key)
        if fill is None:
            fill = _fetch_databento_1m(ticker, gs, ge)
            _HIST_GAP_CACHE[key] = fill
        if len(fill):
            parts.append(fill)
    return (pd.concat(parts, ignore_index=True)
              .drop_duplicates(subset=["ts"], keep="last")
              .sort_values("ts").reset_index(drop=True))


def _resample_history(df_1m: pd.DataFrame, bar_min: int, before_ts, n: int) -> pd.DataFrame:
    """Resample 1m → bar_min, keep bars strictly before before_ts, compute MAs
    (ema/sma), and return the last n. Pure — no I/O. The caller passes a window
    with MA warmup lead-in so the returned bars' MAs are valid."""
    cols = ["ts", "open", "high", "low", "close", "volume"]
    if df_1m is None or len(df_1m) == 0:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in cols})
    agg = cc.aggregate(df_1m, bar_min)
    agg = agg[agg["ts"] < before_ts]
    if len(agg) == 0:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in cols})
    agg = cc.assign_bins(agg)
    agg = mod.compute_features(agg)          # ema20/ema50/sma200/vwap
    agg["ema96"] = agg["close"].ewm(span=96, adjust=False).mean()    # EMA-stack preset
    agg["ema200"] = agg["close"].ewm(span=200, adjust=False).mean()
    return agg.tail(int(n)).reset_index(drop=True)


def _history_payload(ticker: str, tf: str, before_ms: int, n: int) -> list:
    """Older candles (strictly before before_ms) for scroll-back: read the archive
    1m, backfill any truncated-month holes from Databento, resample to tf, and
    compute the moving averages so the MA lines continue across deep history.
    Reads a window sized for n bars + ~MA warmup."""
    cfg = cc.BUCKET_SPEC[tf]
    bar_min = cfg["bar_min"]
    n = max(1, min(int(n), HIST_MAX_N))
    before_ts = pd.Timestamp(int(before_ms), unit="ms", tz="UTC")
    # Window: n bars + ~260 bars MA warmup, inflated for non-trading time.
    span = pd.Timedelta(minutes=int((n + 260) * bar_min * 2)) + pd.Timedelta(days=4)
    df = local_history.load_range(ticker, before_ts - span, before_ts)
    if len(df):
        df = _backfill_archive_gaps(ticker, df)
    # The on-disk archive lags real time (per-ticker: e.g. CL ends ~5 days back while the
    # live snapshot starts at "now"). Scrolling back from the snapshot's oldest bar lands in
    # that archive-lag GAP, which returns 0 bars and latches the client's `exhausted` flag —
    # so deep-history scroll-back dies ("everything falls off the left"). The live feed's
    # in-memory 1m base IS continuous over the recent ~60d (Databento-gap-filled at boot), so
    # union any feed bars in this window to bridge the gap. Older-than-feed requests are
    # unaffected (the feed has nothing there → archive serves alone, back to 2010).
    feed = STATES.get(ticker)
    if feed is not None and getattr(feed, "df", None) is not None:
        try:
            with feed.lock:
                fdf = feed.df
                fdf = fdf[(fdf["ts"] < before_ts) & (fdf["ts"] >= before_ts - span)][
                    ["ts", "open", "high", "low", "close", "volume"]].copy()
        except Exception:
            fdf = None
        if fdf is not None and len(fdf):
            df = (pd.concat([df, fdf], ignore_index=True) if df is not None and len(df) else fdf) \
                   .drop_duplicates(subset=["ts"], keep="last") \
                   .sort_values("ts").reset_index(drop=True)
    agg = _resample_history(df, bar_min, before_ts, n)
    if not len(agg):
        return []
    tms = (agg["ts"].astype("int64") // 1_000_000).to_numpy()
    o = agg["open"].to_numpy();  h = agg["high"].to_numpy()
    lo = agg["low"].to_numpy();  c = agg["close"].to_numpy(); v = agg["volume"].to_numpy()
    def col(name):
        return agg[name].to_numpy() if name in agg.columns else np.full(len(agg), np.nan)
    e20, e50, s200 = col("ema20"), col("ema50"), col("sma200")
    e96, e200 = col("ema96"), col("ema200")
    rows = []
    for i in range(len(agg)):
        rows.append({
            "time": int(tms[i]),
            "open": float(o[i]), "high": float(h[i]), "low": float(lo[i]),
            "close": float(c[i]), "volume": int(v[i]),
            "ema20": _f(e20[i]), "ema50": _f(e50[i]), "ema96": _f(e96[i]),
            "ema200": _f(e200[i]), "sma200": _f(s200[i]),
            "vwap_d": None, "vwap_w": None,   # session/week VWAP not drawn on deep history
        })
    return rows


def _snap_to_bucket_start(ts_et_pd, anchor_min_offset: int, bucket_min: int) -> int:
    """Return ms epoch of the most recent bucket-start boundary at or before this ts.
    anchor_min_offset = minutes-of-day where buckets land (e.g. 0 for on-the-hour 1H,
    or 60 for 1:00-anchored 4H (TOS grid) — modulo-bucket arithmetic, any anchor works).
    bucket_min = bucket size in minutes (60 or 240)."""
    # minute-of-day (ET) of this ts
    et_min = ts_et_pd.hour * 60 + ts_et_pd.minute
    # offset from anchor within the bucket cycle
    offset = (et_min - anchor_min_offset) % bucket_min
    # subtract that many minutes (and zero seconds/micros) to land on the boundary
    snapped = ts_et_pd.replace(second=0, microsecond=0) - pd.Timedelta(minutes=int(offset))
    return int(snapped.timestamp() * 1000)


def _segments_payload(df: pd.DataFrame, n: int):
    """Per-bucket H/L/C segments projected forward (240 bars for 4H, 60 for 1H).
    Returns list of {x_start_ms, x_end_ms, value, type}."""
    sub = df.tail(n).reset_index(drop=True)
    bin_4h = sub["bin_4h"].to_numpy()
    bin_1h = sub["bin_1h"].to_numpy()
    high = sub["high"].to_numpy(); low = sub["low"].to_numpy(); close = sub["close"].to_numpy()
    ts_ms = (sub["ts"].astype("int64") // 1_000_000).to_numpy()
    n_bars = len(sub)
    out = []
    if n_bars < 2: return out

    # Leading buckets — from bin lookup, project from chart left to first divider
    if hasattr(mod, "BIN_4H_HLC") and len(mod.BIN_4H_HLC) > 0:
        leading_4h_id = int(bin_4h[0]) - 1
        first_4h_x_ms = None
        for i in range(1, n_bars):
            if bin_4h[i] != bin_4h[i-1]: first_4h_x_ms = int(ts_ms[i]); break
        if leading_4h_id in mod.BIN_4H_HLC and first_4h_x_ms is not None:
            h4, l4, c4 = mod.BIN_4H_HLC[leading_4h_id]
            out.append({"x_start_ms": int(ts_ms[0]), "x_end_ms": first_4h_x_ms, "value": h4, "type": "4HH"})
            out.append({"x_start_ms": int(ts_ms[0]), "x_end_ms": first_4h_x_ms, "value": l4, "type": "4HL"})
            out.append({"x_start_ms": int(ts_ms[0]), "x_end_ms": first_4h_x_ms, "value": c4, "type": "4HC"})
    if hasattr(mod, "BIN_1H_HLC") and len(mod.BIN_1H_HLC) > 0:
        leading_1h_id = int(bin_1h[0]) - 1
        first_1h_x_ms = None
        for i in range(1, n_bars):
            if bin_1h[i] != bin_1h[i-1]: first_1h_x_ms = int(ts_ms[i]); break
        if leading_1h_id in mod.BIN_1H_HLC and first_1h_x_ms is not None:
            h1, l1, c1 = mod.BIN_1H_HLC[leading_1h_id]
            out.append({"x_start_ms": int(ts_ms[0]), "x_end_ms": first_1h_x_ms, "value": h1, "type": "1HH"})
            out.append({"x_start_ms": int(ts_ms[0]), "x_end_ms": first_1h_x_ms, "value": l1, "type": "1HL"})
            out.append({"x_start_ms": int(ts_ms[0]), "x_end_ms": first_1h_x_ms, "value": c1, "type": "1HC"})

    # In-window per-bucket segments — at each bucket transition, use the FULL
    # H/L/C of the just-closed bucket from BIN_*_HLC (not just chart-window bars),
    # snapped to the TOS grid boundary (1H on the hour, 4H at 1/5/9/13/17/21 ET).
    et_arr = sub["ts_et"]
    # 4H anchor 1:00 ET — within-day offset modulo 4H = 60 % 240 = 60
    ANCHOR_4H = mod.FOUR_H_ANCHOR_MIN % 240
    # 1H anchor on the hour = 0 mod 60
    ANCHOR_1H = 0
    BUCKET_4H_MS = 240 * 60_000
    BUCKET_1H_MS = 60 * 60_000

    cur_4h = int(bin_4h[0])
    for i in range(1, n_bars):
        if int(bin_4h[i]) != cur_4h:
            # cur_4h just closed — fetch its FULL H/L/C from the lookup
            if cur_4h in mod.BIN_4H_HLC:
                h_, l_, c_ = mod.BIN_4H_HLC[cur_4h]
                xs = _snap_to_bucket_start(et_arr.iloc[i], ANCHOR_4H, 240)
                xe = xs + BUCKET_4H_MS
                out.append({"x_start_ms": xs, "x_end_ms": xe, "value": h_, "type": "4HH"})
                out.append({"x_start_ms": xs, "x_end_ms": xe, "value": l_, "type": "4HL"})
                out.append({"x_start_ms": xs, "x_end_ms": xe, "value": c_, "type": "4HC"})
            cur_4h = int(bin_4h[i])
    # 4H safety net (same as 1H below): if wall clock has crossed into a new 4H
    # bucket but no df bar has landed there yet, project the latest df bar's
    # 4H H/L/C into the current bucket area.
    now_et_ts = pd.Timestamp.now(tz=ET)
    cur_4h_now = _snap_to_bucket_start(now_et_ts, ANCHOR_4H, 240)
    last_bar_4h_start = _snap_to_bucket_start(et_arr.iloc[-1], ANCHOR_4H, 240)
    if cur_4h_now > last_bar_4h_start:
        last_4h_bin = int(bin_4h[-1])
        if last_4h_bin in mod.BIN_4H_HLC:
            h_, l_, c_ = mod.BIN_4H_HLC[last_4h_bin]
            xs = cur_4h_now
            xe = xs + BUCKET_4H_MS
            out.append({"x_start_ms": xs, "x_end_ms": xe, "value": h_, "type": "4HH"})
            out.append({"x_start_ms": xs, "x_end_ms": xe, "value": l_, "type": "4HL"})
            out.append({"x_start_ms": xs, "x_end_ms": xe, "value": c_, "type": "4HC"})

    cur_1h = int(bin_1h[0])
    for i in range(1, n_bars):
        if int(bin_1h[i]) != cur_1h:
            if cur_1h in mod.BIN_1H_HLC:
                h_, l_, c_ = mod.BIN_1H_HLC[cur_1h]
                xs = _snap_to_bucket_start(et_arr.iloc[i], ANCHOR_1H, 60)
                xe = xs + BUCKET_1H_MS
                out.append({"x_start_ms": xs, "x_end_ms": xe, "value": h_, "type": "1HH"})
                out.append({"x_start_ms": xs, "x_end_ms": xe, "value": l_, "type": "1HL"})
                out.append({"x_start_ms": xs, "x_end_ms": xe, "value": c_, "type": "1HC"})
            cur_1h = int(bin_1h[i])
    # Safety net: even if no closed bar has landed in the NEW bucket yet, project
    # the latest df bar's bucket H/L/C into the bucket the WALL CLOCK is now in.
    # This fixes the "new bucket has no prior-hour levels until refresh" bug
    # caused by live-stream lag.
    now_et_ts = pd.Timestamp.now(tz=ET)
    cur_bucket_now = _snap_to_bucket_start(now_et_ts, ANCHOR_1H, 60)
    last_bar_bucket_start = _snap_to_bucket_start(et_arr.iloc[-1], ANCHOR_1H, 60)
    if cur_bucket_now > last_bar_bucket_start:
        last_bin_id = int(bin_1h[-1])
        if last_bin_id in mod.BIN_1H_HLC:
            h_, l_, c_ = mod.BIN_1H_HLC[last_bin_id]
            xs = cur_bucket_now
            xe = xs + BUCKET_1H_MS
            out.append({"x_start_ms": xs, "x_end_ms": xe, "value": h_, "type": "1HH"})
            out.append({"x_start_ms": xs, "x_end_ms": xe, "value": l_, "type": "1HL"})
            out.append({"x_start_ms": xs, "x_end_ms": xe, "value": c_, "type": "1HC"})

    return out


def _boundaries_payload(df: pd.DataFrame, n: int):
    """4H / 1H / 09:30-cash boundaries (ms timestamps) within last n bars.
    Each boundary is snapped to its exact TOS-grid anchor (1H on the hour, 4H at
    1/5/9/13/17/21 ET) so it lines up exactly with the segment endpoints — forming
    perfect boxes regardless of whether a bar exists right at the boundary."""
    sub = df.tail(n).reset_index(drop=True)
    bin_4h = sub["bin_4h"].to_numpy()
    bin_1h = sub["bin_1h"].to_numpy()
    et_arr = sub["ts_et"]
    et_hr = et_arr.dt.hour.to_numpy()
    et_mn = et_arr.dt.minute.to_numpy()
    ANCHOR_4H = mod.FOUR_H_ANCHOR_MIN % 240
    ANCHOR_1H = 0
    # Calendar-based boundary generation — independent of whether bars exist at
    # the exact boundary timestamp (Databento often skips low-volume minutes).
    start_dt = et_arr.min().floor("D") - pd.Timedelta(hours=2)
    end_dt = et_arr.max().ceil("D") + pd.Timedelta(hours=2)
    # 1H boundaries on the hour
    one_h_all = pd.date_range(
        start_dt.replace(minute=0, second=0, microsecond=0),
        end_dt, freq="60min", tz=ET,
    )
    one_h = [int(t.timestamp() * 1000) for t in one_h_all]
    # 4H boundaries at 1 / 5 / 9 / 13 / 17 / 21 ET (TOS grid)
    four_h_hours = [1, 5, 9, 13, 17, 21]
    four_h_set = set(four_h_hours)
    four_h = [ms for ms, t in zip(one_h, one_h_all) if t.hour in four_h_set]
    # Remove 4H boundaries from 1H list (don't double-draw)
    four_h_set_ms = set(four_h)
    one_h = [ms for ms in one_h if ms not in four_h_set_ms]
    # 09:30 ET cash open every day
    cash_all = pd.date_range(
        start_dt.replace(hour=9, minute=30, second=0, microsecond=0),
        end_dt, freq="1D", tz=ET,
    )
    cash = [int(t.timestamp() * 1000) for t in cash_all]
    # 18:00 ET futures reopen every day
    futures_all = pd.date_range(
        start_dt.replace(hour=18, minute=0, second=0, microsecond=0),
        end_dt, freq="1D", tz=ET,
    )
    futures_open = [int(t.timestamp() * 1000) for t in futures_all]
    # Upcoming boundaries — start of the next 1H and 4H bucket after the latest bar
    latest_et = et_arr.iloc[-1]
    this_1h_start = _snap_to_bucket_start(latest_et, ANCHOR_1H, 60)
    this_4h_start = _snap_to_bucket_start(latest_et, ANCHOR_4H, 240)
    next_1h = this_1h_start + 60 * 60_000
    next_4h = this_4h_start + 240 * 60_000
    # Weekly verticals — Sun 18:00 ET (every week of futures-open)
    week_all = pd.date_range(start_dt, end_dt, freq="1W-SUN", tz=ET)
    week_open = [int(t.replace(hour=18, minute=0, second=0, microsecond=0).timestamp() * 1000)
                  for t in week_all]
    return {"four_h": four_h, "one_h": one_h, "cash_open": cash,
             "futures_open": futures_open, "week_open": week_open,
             "next_1h": next_1h, "next_4h": next_4h}


def _levels_payload(df: pd.DataFrame):
    rec = df.iloc[-1]
    levels = {}
    for k in ("pmh", "pml", "pmc", "pwh", "pwl", "pwc", "pdh", "pdl", "pdc"):
        if k in rec and pd.notna(rec[k]):
            levels[k.upper()] = float(rec[k])
    return levels


def _current_period_levels(df: pd.DataFrame) -> dict:
    """CURRENT (in-progress) week + month High/Low. MUST be fed the STITCHED
    CONTINUOUS series (the same `stitched` _refresh_continuous_levels uses for
    PMH/PWL), NOT the pinned `agg`: the current month straddles the contract roll,
    and the pinned outright is the THIN off-front contract before its volume roll
    (e.g. NQU6 printed an 8-lot 31,100 wick in early June while the real front-month
    high was M6 30,807.75). The continuous reads the active contract for each part
    of the period — matching the prior levels and TOS. the author caught the U6-wick bug
    2026-06-15. Computed through the LAST COMPLETED trading day only — i.e. as of the most recent
    4:59 PM ET close, EXCLUDING the in-progress session that opened at the last 6 PM
    (the author 2026-06-15: the level must be 'what it was at 4:59 PM', a fixed reference
    during the live day; it steps once per daily close). CWH/CWL = current ISO week
    (session-based — the trading WEEK opens Sun 18:00 ET). CMH/CML = current month by
    the TRADING-DAY OPEN date (session_date - 1 day, the 6 PM open) — the SAME
    convention PMH/PML use, so a prior-month last-evening open doesn't leak in (the
    5/31-open fix the author caught 2026-06-10). A period whose only session so far is the
    in-progress one yields no level (e.g. CWH/CWL on a Monday before that day closes)."""
    out = {}
    if df is None or len(df) == 0 or "session_date" not in df.columns:
        return out
    completed = df["session_date"] != df["session_date"].iloc[-1]   # drop the live (in-progress) session
    if not completed.any():
        return out
    # current WEEK (session-based) — H/L over COMPLETED days in this week
    if "iso_week_key" in df.columns:
        wk = completed & (df["iso_week_key"] == df["iso_week_key"].iloc[-1])
        if wk.any():
            out["CWH"] = float(df.loc[wk, "high"].max()); out["CWL"] = float(df.loc[wk, "low"].min())
    # current MONTH by 6 PM trading-day OPEN date — H/L over COMPLETED days
    open_dt = pd.to_datetime(df["session_date"]) - pd.Timedelta(days=1)
    ym_td = open_dt.dt.year * 100 + open_dt.dt.month
    mo = completed & (ym_td == ym_td.iloc[-1])
    if mo.any():
        out["CMH"] = float(df.loc[mo, "high"].max()); out["CML"] = float(df.loc[mo, "low"].min())
    return out


_LEVEL_TF_RANK = {"M": 3, "W": 2, "D": 1}   # Month > Week > Day


_LEVEL_DISPLAY = {"PDH": "YH", "PDL": "YL", "PDC": "YC"}   # prior-day keys display as Y* (PROTO convention)


def _dedup_current_levels(levels: dict):
    """Collapse levels sitting at the SAME price (within an H/L/C suffix group) into ONE
    line with a COMBINED label, instead of dropping the lower-TF one (the author 2026-06-16:
    'combine label, and the same on all timeframes'). This supersedes the 2026-06-15
    drop-the-duplicate rule — no level is ever silently hidden; coincident ones share a
    single line whose label lists them all (e.g. today PDH==CWH -> one line 'YH·CWH').

    Survivor (the key kept, so the LINE keeps the dominant style) = highest-TF key
    (Month>Week>Day; a CURRENT 'C' level beats the PRIOR of the same TF). The combined
    title lists every coincident label ascending by TF so the day reference reads first
    (YH·CWH, not CWH·YH). Prior-day keys render as Y* (PDH->YH).
    Returns (levels, titles): `titles` maps the survivor key -> combined display label,
    merged into the snapshot's level_titles so every render path shows it."""
    out = dict(levels)
    groups = {}
    for l, p in list(out.items()):
        if len(l) >= 3 and l[1].upper() in _LEVEL_TF_RANK and l[-1].upper() in "HLC":
            groups.setdefault((l[-1].upper(), round(float(p), 2)), []).append(l)
    titles = {}
    rk = lambda k: (_LEVEL_TF_RANK[k[1].upper()], 1 if k[:1] == "C" else 0)
    for keys in groups.values():
        if len(keys) < 2:
            continue
        survivor = max(keys, key=rk)
        ordered = sorted(keys, key=rk)              # ascending TF → day label first
        titles[survivor] = "·".join(_LEVEL_DISPLAY.get(k, k) for k in ordered)
        for k in keys:
            if k != survivor:
                out.pop(k, None)
    return out, titles


def _multi_day_levels(df: pd.DataFrame, days=(2, 3, 4, 5)) -> dict:
    """Prior 2..5 day HIGH/LOW — the recent daily-swing magnets the immediate PDH/PDL
    miss. Taken from the most recent COMPLETED sessions (excludes the in-progress one).
    Pinned-contract df is fine: 2-5 days back is post-roll, so no continuous stitch is
    needed (unlike month/week levels). Returns {'2DH':.., '2DL':.., ...}."""
    if df is None or len(df) == 0 or "session_date" not in df.columns:
        return {}
    cur = df["session_date"].iloc[-1]
    comp = df[df["session_date"] != cur]
    if comp.empty:
        return {}
    sd = comp.drop_duplicates("session_date", keep="last")["session_date"].tolist()
    g = comp.groupby("session_date"); hi = g["high"].max(); lo = g["low"].min()
    out = {}
    for k in days:                                  # k=1 is PDH/PDL (already emitted)
        if len(sd) >= k:
            d = sd[-k]
            out[f"{k}DH"] = float(hi.loc[d]); out[f"{k}DL"] = float(lo.loc[d])
    return out


def _multi_day_titles(df: pd.DataFrame, days=(2, 3, 4, 5)) -> dict:
    """Display titles for the 2-5 day levels keyed by their WEEKDAY (e.g. 2DL -> 'MON-L'),
    so the chart shows which weekday's high/low it is instead of an opaque '2DL'."""
    if df is None or len(df) == 0 or "session_date" not in df.columns:
        return {}
    cur = df["session_date"].iloc[-1]
    comp = df[df["session_date"] != cur]
    if comp.empty:
        return {}
    sd = comp.drop_duplicates("session_date", keep="last")["session_date"].tolist()
    out = {}
    for k in days:
        if len(sd) >= k:
            wd = pd.Timestamp(sd[-k]).strftime("%a").upper()
            out[f"{k}DH"] = f"{wd}-H"; out[f"{k}DL"] = f"{wd}-L"
    return out


def _multi_period_levels(df: pd.DataFrame, col: str, suffix: str, periods=(2, 3)) -> dict:
    """High/Low of the Nth-prior COMPLETED bucket (excludes the in-progress one),
    grouped by `col`. e.g. col='iso_week_key' suffix='W' -> {'2WH','2WL','3WH','3WL'}
    (2 = a full bucket beyond the immediate prior one, which is PW/PM). col='_ym'
    suffix='M' -> {'2MH','2ML','3MH','3ML'}."""
    if df is None or len(df) == 0 or col not in df.columns:
        return {}
    cur = df[col].iloc[-1]
    comp = df[df[col] != cur]
    if comp.empty:
        return {}
    keys = comp.drop_duplicates(col, keep="last")[col].tolist()
    g = comp.groupby(col); hi = g["high"].max(); lo = g["low"].min()
    out = {}
    for k in periods:                                   # k=1 is PW/PM (already emitted)
        if len(keys) >= k:
            key = keys[-k]
            out[f"{k}{suffix}H"] = float(hi.loc[key]); out[f"{k}{suffix}L"] = float(lo.loc[key])
    return out


@app.route("/static/<path:filename>")
def static_file(filename):
    """Serve files from ~/CIB_DEPLOYMENT_4H_WICK/one_minute/static/ (e.g. logo.png).
    no-store on JS/CSS so the browser never serves a stale chart bundle."""
    from flask import send_from_directory
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    resp = send_from_directory(static_dir, filename)
    if filename.endswith((".js", ".css")):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


def _asset_ver() -> str:
    """mtime-based version stamp for static assets so the browser is FORCED to
    refetch when multichart.js/.css change (defeats aggressive JS caching)."""
    d = Path(__file__).parent / "static"
    try:
        return str(int(max(os.path.getmtime(d / "multichart.js"),
                           os.path.getmtime(d / "multichart.css"))))
    except OSError:
        return str(int(time.time()))


def _versioned(html: str) -> str:
    v = _asset_ver()
    return (html.replace("/static/multichart.js", f"/static/multichart.js?v={v}")
                .replace("/static/multichart.css", f"/static/multichart.css?v={v}"))


@app.route("/")
def index():
    # No-store HTML + version-stamped asset URLs so the browser ALWAYS loads the
    # latest JS/CSS. Grid shell = four quadrants over one multiplexed stream.
    resp = Response(_versioned(INDEX_HTML), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/quadrant")
def quadrant():
    """Single-chart pop-out page (Phase 4 wires makeQuadrant against it)."""
    resp = Response(_versioned(QUADRANT_HTML), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/init/<ticker>/<tf>")
def api_init(ticker, tf):
    try:
        ticker, tf = cc.validate_ticker_tf(ticker, tf)
    except ValueError as e:
        return jsonify({"error": str(e), "valid_tickers": cc.TICKERS,
                        "valid_timeframes": list(cc.BUCKET_SPEC)}), 400
    feed = STATES.get(ticker)
    if not feed or not feed.ready:
        return jsonify({"error": "ticker not ready"}), 503
    view = feed.view(tf)
    with view.lock:
        snap = dict(view.snapshot or {})
        snap["current"] = view.current_bar
    snap["ticker"] = ticker
    snap["contract"] = feed.contract_label
    return jsonify(snap)


@app.route("/api/history/<ticker>/<tf>")
def api_history(ticker, tf):
    """Deep-history candles older than before_ms, for scroll-back to 2010.
    Reads the archive directly — independent of the live feed/snapshot."""
    try:
        ticker, tf = cc.validate_ticker_tf(ticker, tf)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        before_ms = int(request.args.get("before_ms", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "before_ms (ms epoch) required"}), 400
    n = request.args.get("n", 800)
    try:
        bars = _history_payload(ticker, tf, before_ms, n)
    except Exception as e:
        print(f"[HISTORY] {ticker}/{tf} before_ms={before_ms} ERROR {e!r}", flush=True)
        return jsonify({"error": repr(e)}), 500
    # exhausted = nothing older exists (reached the start of the archive / 2010)
    try:
        _b4 = pd.Timestamp(int(before_ms), unit="ms", tz="UTC")
        _ob = (pd.Timestamp(bars[0]["time"], unit="ms", tz="UTC") if bars else None)
        print(f"[HISTORY] {ticker}/{tf} before={_b4:%Y-%m-%d %H:%M} → {len(bars)} bars"
              f"{(' (oldest '+format(_ob,'%Y-%m-%d %H:%M')+')') if _ob is not None else ''}"
              f" exhausted={len(bars)==0}", flush=True)
    except Exception:
        pass
    return jsonify({"ticker": ticker, "tf": tf, "bars": bars,
                    "exhausted": len(bars) == 0})


@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    """Declare a client's active (ticker,tf) topic set. Returns a client_id and
    immediately pushes a current cold snapshot for each subscribed view."""
    data = request.get_json(force=True) or {}
    cid = data.get("client_id") or uuid.uuid4().hex
    topics: set[tuple[str, str]] = set()
    for t in data.get("topics", []):
        try:
            topics.add(cc.validate_ticker_tf(t.get("ticker"), t.get("tf")))
        except (ValueError, AttributeError):
            continue   # ignore unknown/malformed topics (spec §5.1)
    with SUB_LOCK:
        SUBSCRIBERS[cid] = topics
        CLIENT_QUEUES.setdefault(cid, _queue.Queue(maxsize=256))
    # ensure each subscribed view exists + immediately push its current cold snapshot
    for tk, tf in topics:
        f = STATES.get(tk)
        if f and f.ready:
            v = f.view(tf)
            if v.snapshot is not None:
                publish_cold(tk, tf, v.snapshot)
    return jsonify({"client_id": cid,
                    "topics": [{"ticker": a, "tf": b} for a, b in topics]})


@app.route("/api/stream")
def api_stream():
    """Single multiplexed SSE per page. Event-driven: blocks on the client's
    queue and emits the instant a view publishes a hot/cold frame (spec §5.4.4).
    The 15s timeout is a keepalive only; cleanup removes the client on disconnect."""
    cid = request.args.get("client_id", "")
    with SUB_LOCK:
        q = CLIENT_QUEUES.setdefault(cid, _queue.Queue(maxsize=256))

    def gen():
        try:
            while True:
                try:
                    frame = q.get(timeout=15)            # BLOCKS until a frame is pushed
                    yield f"data: {json.dumps(frame)}\n\n"
                except _queue.Empty:
                    yield ": keepalive\n\n"               # heartbeat only when truly idle
        finally:
            with SUB_LOCK:                                # client gone — clean up (no leak)
                CLIENT_QUEUES.pop(cid, None)
                SUBSCRIBERS.pop(cid, None)
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(gen(), mimetype="text/event-stream", headers=headers)


# ===== Frontend =====
# Grid shell: a 2x2 responsive grid of four <div class="quadrant"> built by
# static/multichart.js, plus a fixed top-left QE brand block (spec §3.4 / A.9).
# The brand sits in a reserved top band (#grid padding-top); the maximized
# single-chart view starts BELOW that band so it never overlaps the controls.
# All chart logic lives in static/multichart.js (makeQuadrant factory + shared
# multiplexed stream). lightweight-charts is pinned to v4.2.3.
INDEX_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"/>
<title>Multichart</title>
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<link rel="stylesheet" href="/static/multichart.css"/>
</head><body>
  <div id="brand">
    <span class="brand-name">MULTICHART</span>
    <button id="dual-btn" title="Toggle side-by-side dual view of Q0's ticker: 1H (last 5 weekdays) | 15m (last 3 trading days)">📊 Dual</button>
  </div>
  <div id="grid">
    <div class="quadrant-slot" data-quadrant="0"></div>
    <div class="quadrant-slot" data-quadrant="1"></div>
    <div class="quadrant-slot" data-quadrant="2"></div>
    <div class="quadrant-slot" data-quadrant="3"></div>
  </div>
  <div id="dual">
    <div class="dual-slot" data-side="left"></div>
    <div class="dual-slot" data-side="right"></div>
  </div>
  <script src="/static/multichart.js"></script>
</body></html>"""


# One-quadrant shell for pop-outs. Reads ticker/tf from location.search and calls
# window.makeQuadrant(document.body, ticker, tf) which opens its OWN multiplexed
# stream for that single topic (spec §5.1 — separate browsing context).
QUADRANT_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>QE Chart</title>
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<link rel="stylesheet" href="/static/multichart.css"/>
<style>html,body{margin:0;height:100%;background:#fff;}</style>
</head><body>
<script src="/static/multichart.js"></script>
<script>
  (function () {
    var p = new URLSearchParams(location.search);
    var ticker = p.get('ticker') || 'NQ';
    var tf = p.get('tf') || '1m';
    function go() { if (window.makeQuadrant) window.makeQuadrant(document.body, ticker, tf); }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', go);
    } else { go(); }
  })();
</script>
</body></html>"""

_BOOTED = False


def boot_feeds(tickers=None):
    """Boot the requested ticker feeds (all 7 by default): load 1m history in
    parallel, then start each live stream + cold-path worker. Idempotent.
    Side-effect-free at import — only runs when explicitly called (plan Task 10/12)."""
    global _BOOTED
    if _BOOTED:
        return
    _BOOTED = True
    tickers = list(tickers) if tickers else list(cc.TICKERS)
    print(f"\nMultichart · {len(tickers)} feeds · 1m base\n", flush=True)

    for t in tickers:
        STATES[t] = TickerFeed(t)
    from concurrent.futures import ThreadPoolExecutor
    print(f"Loading {len(tickers)} feeds in parallel...", flush=True)
    with ThreadPoolExecutor(max_workers=max(len(tickers), 1)) as ex:
        list(ex.map(lambda s: s.init_history(), STATES.values()))
    print("All histories loaded — starting live streams + cold-path workers", flush=True)
    for t, feed in STATES.items():
        feed.start_worker()
        threading.Thread(target=feed.stream, daemon=True, name=f"stream-{t}").start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=list(cc.TICKERS),
                          help="Tickers to stream (default: all 7)")
    parser.add_argument("--port", type=int, default=8010)
    # --timeframe retained for back-compat / dev; ignored (feed is 1m base).
    parser.add_argument("--timeframe", default="1m")
    args = parser.parse_args()
    boot_feeds(args.tickers)
    print(f"\nMultichart: http://localhost:{args.port}/", flush=True)
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


# Boot under gunicorn when the module is imported with CHARTS_BOOT=1 (run_multichart.sh).
# Importing the module WITHOUT this flag is side-effect-free (required for tests).
if os.environ.get("CHARTS_BOOT") == "1":
    boot_feeds(os.environ.get("CHARTS_TICKERS", "").split() or None)


if __name__ == "__main__":
    main()
