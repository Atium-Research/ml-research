"""Time-series IV z-score: buy names whose ATM vol is cheap for itself, sell those rich.

The score is `iv_zscore` from the store: each name's log 60-day ATM implied
vol against its own trailing 250-session mean and std (at least 120
sessions), positive when rich, clipped at +/-3. Three books trade it weekly
at $20k gross vega on the panel engine:

* quantile spread: long the cheapest decile, short the richest, equal weight;
* MVO: alpha = -0.04 x idio vol x score, factor risk model, net-vega neutral;
* MVO factor-neutral: the same with |factor exposure| <= 5% of the budget.

The backtester trades each session on the previous session's score, so no
book sees the close it trades at. Each book is run gross and net of the full
half-spread on every fill plus the reference path's roll and hedge costs.
Names outside the point-in-time universe or in a wrong-company symbol-year
are dropped.

    uv run python iv_zscore/study.py
"""

import datetime as dt
import time
from pathlib import Path

import matplotlib
import ml_data_access
import polars as pl
from malatium.backtester import Backtester
from malatium.optimizer import (
    MVO,
    FactorNeutral,
    GrossCap,
    MaxUtility,
    NetVegaNeutral,
    TurnoverPenalty,
)
from malatium.providers import PanelProvider, TradingCalendar
from malatium.results import BacktestResults
from malatium.risk_model import FactorRiskModelConstructor
from malatium.schemas import AlphasSchema
from malatium.strategy import OptimizationStrategy, QuantileSpreadStrategy

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

STUDY_DIR = Path(__file__).resolve().parent
RESULTS_DIR = STUDY_DIR / "results"
FIGURES_DIR = STUDY_DIR / "figures"

SIGNAL = "iv_zscore"
START, END = dt.date(2018, 7, 2), dt.date(2025, 6, 30)
GROSS_VEGA = 20_000.0
QUANTILE = 0.10
IC = 0.04
RISK_AVERSION = 1e-5 * GROSS_VEGA  # 1e-5 per dollar vega, quoted on budget fractions
TURNOVER_COST = 0.10
NET_VEGA_TOLERANCE = 500.0 / GROSS_VEGA
FACTOR_EPSILON = 1_000.0 / GROSS_VEGA
HORIZON = 60

COLORS = {"quantile": "#2a78d6", "mvo": "#eb6834", "mvo_factor_neutral": "#1baf7a"}
LABELS = {"quantile": "quantile spread", "mvo": "MVO", "mvo_factor_neutral": "MVO, factor neutral"}


def load_inputs(db):
    usable_df = ml_data_access.usable_symbol_years(db)
    scores_df = (
        ml_data_access.load_signals(db, SIGNAL, START, END)
        .with_columns(pl.col("date").dt.year().cast(pl.Int32).alias("year"))
        .join(usable_df, on=["symbol", "year"], how="semi")
        .drop("year")
    )
    return dict(
        scores_df=scores_df,
        reference_df=ml_data_access.load_reference_returns(db, START, END),
        universe=PanelProvider(ml_data_access.load_universe(db, START, END)),
        calendar=TradingCalendar(ml_data_access.load_sessions(db)),
        factor_returns_df=ml_data_access.load_factor_returns(db, START, END),
        loadings_df=ml_data_access.load_factor_loadings(db, START, END),
        covariances_df=ml_data_access.load_factor_covariances(db, START, END),
        idio_df=ml_data_access.load_idio_vol(db, START, END),
    )


def build_strategies(inputs: dict, scores_df: pl.DataFrame) -> dict:
    scores = PanelProvider(scores_df)
    alphas_df = AlphasSchema.validate(
        scores_df.join(inputs["idio_df"], on=["date", "symbol"])
        .with_columns((-IC * pl.col("idio_vol") * pl.col("score")).alias("alpha"))
        .select("date", "symbol", "alpha"),
        cast=True,
    )
    risk_model = FactorRiskModelConstructor(
        PanelProvider(inputs["loadings_df"]),
        PanelProvider(inputs["covariances_df"]),
        PanelProvider(inputs["idio_df"]),
    )

    def mvo(constraints):
        optimizer = MVO([MaxUtility(RISK_AVERSION), TurnoverPenalty(TURNOVER_COST)], constraints)
        return OptimizationStrategy(PanelProvider(alphas_df), risk_model, optimizer)

    return {
        "quantile": lambda: QuantileSpreadStrategy(scores, inputs["universe"], quantile=QUANTILE),
        "mvo": lambda: mvo([NetVegaNeutral(NET_VEGA_TOLERANCE), GrossCap(1.0)]),
        "mvo_factor_neutral": lambda: mvo(
            [NetVegaNeutral(NET_VEGA_TOLERANCE), GrossCap(1.0), FactorNeutral(FACTOR_EPSILON)]
        ),
    }


def run_book(inputs: dict, make_strategy, cost_fraction: float) -> BacktestResults:
    records_df = Backtester().run(
        inputs["calendar"],
        PanelProvider(inputs["reference_df"]),
        make_strategy(),
        START,
        END,
        gross_vega=GROSS_VEGA,
        rebalance_frequency="weekly",
        cost_fraction=cost_fraction,
    )
    return records_df


def summarize(name: str, cost_fraction: float, records_df: pl.DataFrame, factor_returns_df) -> dict:
    results = BacktestResults(records_df)
    summary = results.summary()
    regression_df = results.factor_regression(factor_returns_df)
    market = regression_df.filter(pl.col("regressor") == "market")
    const = regression_df.filter(pl.col("regressor") == "const")
    return {
        "book": name,
        "costs": "net" if cost_fraction else "gross",
        "positions": summary["mean_positions"],
        "gross_vega": summary["mean_gross_vega"],
        "total_pnl": summary["total_net_pnl"],
        "sharpe": summary["sharpe"],
        "annual_pnl_per_gross_vega": summary["annual_net_pnl_per_gross_vega"],
        "max_drawdown": summary["max_drawdown"],
        "annual_turnover": summary["annual_turnover_over_gross_vega"],
        "total_cost": summary["total_cost"],
        "market_loading": float(market["coefficient"][0]),
        "market_t": float(market["t_stat"][0]),
        "intercept_t": float(const["t_stat"][0]),
    }


def plot_equity(book_series: dict[str, pl.DataFrame]) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    for name, book_df in book_series.items():
        cumulative = (book_df["net_pnl"].cum_sum() / 1e3).to_list()
        label = LABELS[name]
        ax.plot(book_df["date"].to_list(), cumulative, color=COLORS[name], linewidth=2, label=label)
        ax.annotate(
            label,
            (book_df["date"][-1], cumulative[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
            color="#444444",
        )
    ax.axhline(0, color="#888888", linewidth=0.6)
    ax.set_ylabel("cumulative gross P&L, $k")
    ax.set_title("IV z-score books, $20k gross vega, weekly, no costs")
    ax.grid(axis="y", color="#e5e5e5", linewidth=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(frameon=False, loc="upper left")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "equity_curves.png", dpi=150)
    plt.close(fig)


def plot_deciles(decile_df: pl.DataFrame) -> None:
    """Gross only: net of the half-spread is -18 to -26 in every decile and flattens the shape."""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    x = decile_df["decile"].to_numpy()
    values = decile_df["gross_fwd_pnl_per_vega"].to_numpy()
    ax.bar(x, values, 0.6, color="#2a78d6")
    for xi, value, t in zip(x, values, decile_df["gross_t_stat"].to_numpy(), strict=True):
        ax.annotate(
            f"t {t:.1f}",
            (xi, value),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            color="#444444",
        )
    ax.axhline(0, color="#888888", linewidth=0.6)
    ax.set_xticks(x)
    ax.set_xlabel("IV z-score decile (10 = rich for itself)")
    ax.set_ylabel(f"forward {HORIZON}-session P&L per $ vega, long straddle")
    ax.set_title("Forward reference-straddle P&L by score decile, gross")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "deciles.png", dpi=150)
    plt.close(fig)


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    FIGURES_DIR.mkdir(exist_ok=True)
    pl.Config.set_tbl_width_chars(200)
    pl.Config.set_tbl_cols(20)
    pl.Config.set_float_precision(3)
    started = time.time()
    db = ml_data_access.connect()
    inputs = load_inputs(db)
    print(
        f"inputs: {inputs['scores_df'].height:,} scores,"
        f" {inputs['reference_df'].height:,} reference rows ({time.time() - started:.0f}s)"
    )

    rows, book_series = [], {}
    strategies = build_strategies(inputs, inputs["scores_df"])
    for name, make_strategy in strategies.items():
        for cost_fraction in (0.0, 1.0):
            records_df = run_book(inputs, make_strategy, cost_fraction)
            rows.append(summarize(name, cost_fraction, records_df, inputs["factor_returns_df"]))
            if cost_fraction == 0.0:
                book_series[name] = BacktestResults(records_df).book_df
            print(f"  {name} {'net' if cost_fraction else 'gross'} ({time.time() - started:.0f}s)")
    summary_df = pl.DataFrame(rows)
    summary_df.write_csv(RESULTS_DIR / "books.csv")
    series_df = pl.concat(
        [
            df.select("date", "net_pnl", "gross_vega", "net_vega", "positions").with_columns(
                pl.lit(name).alias("book")
            )
            for name, df in book_series.items()
        ]
    )
    series_df.write_csv(RESULTS_DIR / "book_series.csv")

    decile_df = BacktestResults.decile_table(
        inputs["scores_df"], inputs["reference_df"], horizon_days=HORIZON
    )
    decile_df.write_csv(RESULTS_DIR / "deciles.csv")
    annual_df = (
        series_df.group_by("book", pl.col("date").dt.year().alias("year"))
        .agg(pl.col("net_pnl").sum().alias("pnl"))
        .pivot(index="year", on="book", values="pnl")
        .sort("year")
    )
    annual_df.write_csv(RESULTS_DIR / "annual.csv")

    plot_equity(book_series)
    plot_deciles(decile_df)
    print(summary_df)
    print(decile_df)
    print(annual_df)
    print(
        f"done in {time.time() - started:.0f}s; results in {RESULTS_DIR}, figures in {FIGURES_DIR}"
    )


if __name__ == "__main__":
    main()
