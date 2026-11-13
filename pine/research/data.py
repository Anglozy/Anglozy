"""Load real market data and resample to 15-minute bars (UTC)."""
import pandas as pd
import numpy as np

D = "/tmp/claude-0/-home-user-Anglozy/62cd4c4f-9157-579a-95a9-f26ceb74b159/scratchpad/data"


def _resample(m1, rule="15min"):
    agg = m1.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last",
         "volume": "sum", "spread": "median"})
    return agg.dropna(subset=["open"])


def load_gold(tf="15min"):
    df = pd.read_parquet(f"{D}/sherwynjoel_xauusd-historical-data/xauusd_m1_full.parquet")
    df = df.rename(columns={"tick_volume": "volume"})
    df["spread"] = df["spread"] * 0.001          # points -> USD (3-digit quotes)
    df.index = df.index.tz_convert(None)
    out = _resample(df, tf)
    out["spread"] = out["spread"].clip(upper=1.0)  # cap bad prints (>$1.00)
    return out


def load_eurusd():
    df = pd.read_parquet(f"{D}/lxloby_15-minute-quick-win-strategy/data/cache/EURUSD_M15.parquet")
    df = df.rename(columns={"tick_volume": "volume"})
    df.index = pd.to_datetime(df["time_utc"])
    df["spread"] = df["spread"] * 0.00001          # points -> price (5-digit)
    df = df[["open", "high", "low", "close", "volume", "spread"]]
    df = df[df.index >= "2010-01-01"]
    df["spread"] = df["spread"].clip(upper=0.0003)
    return df


if __name__ == "__main__":
    g = load_gold(); e = load_eurusd()
    print(g.shape, g.index[0], g.index[-1], g.spread.median())
    print(e.shape, e.index[0], e.index[-1], e.spread.median())
