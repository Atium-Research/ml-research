"""Variance risk premium: long names whose implied vol is cheap against realized, short rich.

Two versions of the score, both positive when implied is rich:

* `vrp_raw`: 60-day ATM implied vol minus trailing 22-session realized vol,
  both annualized, z-scored across names each day and clipped at +/-3.
  The premium as it stands today, with no model in it.
* `vrp`: the store's residualised premium, ln(iv60) - ln(HAR forecast of the
  forward 60-session realized vol), regressed cross-sectionally on beta x
  the SPX premium, the sector median and idio vol, smoothed over five
  sessions and z-scored.

Three weekly books per score on the panel engine (see `utils.books`).

    uv run python -m vrp.study
"""

from pathlib import Path

import ml_data_access
import polars as pl

from utils import books

STUDY_DIR = Path(__file__).resolve().parent
RV_WINDOW = 22


def build_raw_vrp(db) -> pl.DataFrame:
    surface_df = ml_data_access.load_surface(db, books.START, books.END)
    realized_df = ml_data_access.load_realized_vol(db, books.START, books.END)
    premium_df = (
        surface_df.select("date", "symbol", "iv60")
        .join(realized_df.select("date", "symbol", f"rv_{RV_WINDOW}"), on=["date", "symbol"])
        .filter(pl.col("iv60") > 0, pl.col(f"rv_{RV_WINDOW}") > 0, pl.col("symbol") != "SPX")
        .with_columns((pl.col("iv60") - pl.col(f"rv_{RV_WINDOW}")).alias("premium"))
    )
    return books.zscore_daily(premium_df, "premium")


def main() -> None:
    db = ml_data_access.connect()
    common = books.load_common(db)
    scores = {
        "vrp_raw": build_raw_vrp(db),
        "vrp": ml_data_access.load_signals(db, "vrp", books.START, books.END),
    }
    titles = {
        "vrp_raw": "Raw VRP (iv60 - rv22), $20k gross vega, weekly, no costs",
        "vrp": "Residualised VRP (iv60 vs HAR forecast), $20k gross vega, weekly, no costs",
    }
    books.run_study(scores, common, STUDY_DIR, titles)


if __name__ == "__main__":
    main()
