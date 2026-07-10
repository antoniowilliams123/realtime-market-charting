"""Local 1-minute history store reader (Phase 1).

Serves the deep 1-minute candle archive that already lives on disk under
``$ARCHIVE_DIR/{TICKER}_1m/`` as monthly gzip partitions:

    $ARCHIVE_DIR/NQ_1m/NQ_1m_2010-07.csv.gz   →   ...2026-06   (back to 2010)
    columns: ts_utc,open,high,low,close,volume

The chart server uses this to SEED the recent display
window from disk instead of re-pulling ~60 days from the Databento Historical
API on every boot. Databento is then only asked for the small
``last_local_bar → now`` gap. Same continuous-contract provenance as the live
feed — verified tick-for-tick on NQ (open/high/low identical, close mean diff
0.0002, volume exact). A boot-time overlap check in the caller guards any
ticker whose local provenance differs (e.g. GC, pinned to a micro contract).

Pure file reader: no network, no global state — unit-tested in
tests/test_local_history.py against synthetic temp partitions.
"""
import re
import os
import pathlib
from pathlib import Path

import pandas as pd

DEFAULT_ROOT = pathlib.Path(os.environ.get("ARCHIVE_DIR", str(Path.home() / "market_archive")))
OHLCV = ["ts", "open", "high", "low", "close", "volume"]

# Match ONLY clean monthly partitions, e.g. "NQ_1m_2026-06.csv.gz".
# Deliberately excludes weekly backups like "...csv.gz.bak_wk" and any other
# suffix — reading those would inject duplicate / stale bars.
_MONTH_RE = re.compile(r"^(?P<ticker>[A-Z0-9]+)_1m_(?P<ym>\d{4}-\d{2})\.csv\.gz$")


def _dir(ticker: str, root) -> Path:
    return Path(root if root is not None else DEFAULT_ROOT) / f"{ticker}_1m"


def _partitions(ticker: str, root) -> list[tuple[str, Path]]:
    """Sorted [(YYYY-MM, path)] of clean monthly partitions for a ticker."""
    d = _dir(ticker, root)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        m = _MONTH_RE.match(p.name)
        if m and m.group("ticker") == ticker:
            out.append((m.group("ym"), p))
    out.sort()
    return out


def _read_partition(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # Archive months were written by different code over time: most use lowercase
    # OHLCV headers, but some (e.g. ES/YM/CL/MBT 2026-04) use Open,High,Low,Close,
    # Volume. Normalize casing so both parse identically.
    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={"ts_utc": "ts"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df[OHLCV]


def _empty() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="float64") for c in OHLCV})
    df["ts"] = pd.Series(dtype="datetime64[ns, UTC]")
    return df[OHLCV]


def load_range(ticker: str, start, end, root=None) -> pd.DataFrame:
    """Return local 1m OHLCV bars in [start, end] (inclusive), tz-aware UTC.

    Reads only the monthly partitions whose month intersects the window, so
    pulling a few recent days from a 15-year archive never touches old files.
    Returns an empty (correctly typed) frame when the ticker has no local data.
    """
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    lo_ym = f"{start.year:04d}-{start.month:02d}"
    hi_ym = f"{end.year:04d}-{end.month:02d}"
    frames = [_read_partition(p) for ym, p in _partitions(ticker, root)
              if lo_ym <= ym <= hi_ym]
    if not frames:
        return _empty()
    df = (pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["ts"], keep="last")
            .sort_values("ts")
            .reset_index(drop=True))
    df = df[(df["ts"] >= start) & (df["ts"] <= end)].reset_index(drop=True)
    return df


def reconcile(local_df: pd.DataFrame, db_df: pd.DataFrame,
              tol_frac: float = 1e-4) -> tuple[pd.DataFrame, bool]:
    """Merge a local seed with a fresh Databento fetch, guarding provenance.

    The caller fetches the recent ``last_local → now`` gap from Databento WITH a
    deliberate overlap back into the local region. Here we check that the
    overlapping bars agree: within the recent overlap window both series track the
    same front-month contract, so a clean local store matches to the tick. A
    ticker whose local archive has different provenance (e.g. GC's continuous
    series vs the app's pinned micro contract) will disagree — we then DISCARD the
    local seed and return the Databento data alone.

    Returns ``(merged_df, used_local)``. ``used_local`` is False whenever the
    local seed was empty, didn't overlap, or failed the agreement check — in all
    those cases ``merged_df`` is just the Databento data.
    """
    db_df = (db_df.drop_duplicates(subset=["ts"], keep="last")
                  .sort_values("ts").reset_index(drop=True))
    if local_df is None or local_df.empty:
        return db_df, False
    shared = local_df.merge(db_df, on="ts", suffixes=("_loc", "_db"))
    if shared.empty:
        return db_df, False                     # unvalidated → don't trust local
    denom = shared["close_db"].abs().mean() or 1.0
    diff = sum((shared[f"{c}_loc"] - shared[f"{c}_db"]).abs().mean()
               for c in ("open", "high", "low", "close")) / 4.0
    if diff / denom > tol_frac:
        return db_df, False                     # provenance mismatch → drop local
    # db wins on overlapping timestamps (concatenated last, keep="last")
    merged = (pd.concat([local_df, db_df], ignore_index=True)
                .drop_duplicates(subset=["ts"], keep="last")
                .sort_values("ts").reset_index(drop=True))
    return merged, True


def missing_spans(df: pd.DataFrame, max_gap=pd.Timedelta(days=3)) -> list:
    """Holes in a 1-minute series larger than a normal market break.

    The local archive occasionally has a truncated month (an incomplete
    download), which would punch a multi-day hole into the chart that the old
    full-Databento pull didn't have. This finds those gaps so the caller can
    backfill them. A normal weekend close (~49h) stays well under the default
    3-day threshold, so only genuine holes are returned.

    Returns ``[(ts_before_gap, ts_after_gap), ...]`` — the bracketing bars of
    each hole, so the caller can fetch ``[ts_before_gap, ts_after_gap]`` from
    Databento and merge (the endpoints dedupe away).
    """
    if df is None or len(df) < 2:
        return []
    ts = df["ts"].sort_values().reset_index(drop=True)
    deltas = ts.diff()
    out = []
    for i in range(1, len(ts)):
        if deltas.iloc[i] > max_gap:
            out.append((ts.iloc[i - 1], ts.iloc[i]))
    return out


def last_ts(ticker: str, root=None):
    """Timestamp of the newest locally-stored bar, or None if no local data."""
    parts = _partitions(ticker, root)
    if not parts:
        return None
    last = _read_partition(parts[-1][1])
    if last.empty:
        return None
    return last["ts"].max()
