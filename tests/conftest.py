import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ET = "America/New_York"


def make_1m(start_et: str, n: int, base: float = 20000.0, step: float = 1.0) -> pd.DataFrame:
    start = pd.Timestamp(start_et, tz=ET).tz_convert("UTC")
    ts = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = base + np.arange(n) * step
    return pd.DataFrame({"ts": ts, "open": close - 0.5, "high": close + 0.5,
                         "low": close - 1.0, "close": close,
                         "volume": np.full(n, 100, dtype="int64")})


@pytest.fixture
def one_session_1m():
    return make_1m("2026-05-04 09:30", 390)
