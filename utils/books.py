"""The books every signal study runs, so a study is only the score.

`run_study(scores_by_signal, ...)` takes one `(date, symbol, score)` frame
per signal (positive = rich) and, for each, runs three weekly books at
`GROSS_VEGA` on the panel engine, gross and net of the full half-spread:

* quantile spread: long the cheapest decile, short the richest, equal weight;
* MVO: alpha = -IC x idio vol x score against the stored factor risk model,
  net-vega neutral;
* MVO factor-neutral: the same with |factor exposure| <= FACTOR_EPSILON.

It writes `books.csv`, `deciles.csv`, `annual.csv` and `book_series.csv`
under the study's `results/`, and an equity-curve and a decile figure per
signal under `figures/`. Scores are restricted to usable symbol-years; the
quantile book also restricts to the point-in-time universe.
"""

import datetime as dt
import time
from collections.abc import Callable
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
from malatium.schemas import AlphasSchema, ScoresSchema
from malatium.strategy import OptimizationStrategy, QuantileSpreadStrategy

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

START, END = dt.date(2018, 7, 2), dt.date(2025, 6, 30)
GROSS_VEGA = 20_000.0
QUANTILE = 0.10
IC = 0.04
RISK_AVERSION = 1e-5 * GROSS_VEGA  # 1e-5 per dollar vega, quoted on budget fractions
TURNOVER_COST = 0.10
NET_VEGA_TOLERANCE = 500.0 / GROSS_VEGA
FACTOR_EPSILON = 1_000.0 / GROSS_VEGA
HORIZON = 60
WINSOR = 3.0

BOOKS = ("quantile", "mvo", "mvo_factor_neutral")
COLORS = {"quantile": "#2a78d6", "mvo": "#eb6834", "mvo_factor_neutral": "#1baf7a"}
LABELS = {"quantile": "quantile spread", "mvo": "MVO", "mvo_factor_neutral": "MVO, factor neutral"}


def load_common(db) -> dict:
    """Everything a book needs that does not depend on the signal."""
    return dict(
        usable_df=ml_data_access.usable_symbol_years(db),
        reference_df=ml_data_access.load_reference_returns(db, START, END),
        universe=PanelProvider(ml_data_access.load_universe(db, START, END)),
        calendar=TradingCalendar(ml_data_access.load_sessions(db)),
        factor_returns_df=ml_data_access.load_factor_returns(db, START, END),
        loadings_df=ml_data_access.load_factor_loadings(db, START, END),
        covariances_df=ml_data_access.load_factor_covariances(db, START, END),
        idio_df=ml_data_access.load_idio_vol(db, START, END),
    )


def screen_scores(common: dict, scores_df: pl.DataFrame) -> pl.DataFrame:
    """Window, usable symbol-years, schema."""
    screened_df = (
        scores_df.filter(pl.col("date").is_between(START, END))
        .with_columns(pl.col("date").dt.year().cast(pl.Int32).alias("year"))
        .join(common["usable_df"], on=["symbol", "year"], how="semi")
        .select("date", "symbol", "score")
        .drop_nulls("score")
    )
    return ScoresSchema.validate(screened_df, cast=True)


def zscore_daily(frame_df: pl.DataFrame, column: str) -> pl.DataFrame:
    """`(date, symbol, score)`: `column` standardised across names each day, clipped."""
    z = (pl.col(column) - pl.col(column).mean().over("date")) / pl.col(column).std().over("date")
    return frame_df.drop_nulls(column).select(
        "date", "symbol", z.clip(-WINSOR, WINSOR).alias("score")
    )


def build_strategies(common: dict, scores_df: pl.DataFrame) -> dict[str, Callable]:
    scores = PanelProvider(scores_df)
    alphas_df = AlphasSchema.validate(
        scores_df.join(common["idio_df"], on=["date", "symbol"])
        .with_columns((-IC * pl.col("idio_vol") * pl.col("score")).alias("alpha"))
        .select("date", "symbol", "alpha"),
        cast=True,
    )
    risk_model = FactorRiskModelConstructor(
        PanelProvider(common["loadings_df"]),
        PanelProvider(common["covariances_df"]),
        PanelProvider(common["idio_df"]),
    )

    def mvo(constraints):
        optimizer = MVO([MaxUtility(RISK_AVERSION), TurnoverPenalty(TURNOVER_COST)], constraints)
        return OptimizationStrategy(PanelProvider(alphas_df), risk_model, optimizer)

    return {
        "quantile": lambda: QuantileSpreadStrategy(scores, common["universe"], quantile=QUANTILE),
        "mvo": lambda: mvo([NetVegaNeutral(NET_VEGA_TOLERANCE), GrossCap(1.0)]),
        "mvo_factor_neutral": lambda: mvo(
            [NetVegaNeutral(NET_VEGA_TOLERANCE), GrossCap(1.0), FactorNeutral(FACTOR_EPSILON)]
        ),
    }


def run_book(common: dict, make_strategy: Callable, cost_fraction: float) -> pl.DataFrame:
    return Backtester().run(
        common["calendar"],
        PanelProvider(common["reference_df"]),
        make_strategy(),
        START,
        END,
        gross_vega=GROSS_VEGA,
        rebalance_frequency="weekly",
        cost_fraction=cost_fraction,
    )


def summarize(signal: str, book: str, cost_fraction: float, records_df, factor_returns_df) -> dict:
    results = BacktestResults(records_df)
    summary = results.summary()
    regression_df = results.factor_regression(factor_returns_df)
    market = regression_df.filter(pl.col("regressor") == "market")
    const = regression_df.filter(pl.col("regressor") == "const")
    return {
        "signal": signal,
        "book": book,
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


def plot_equity(book_series: dict[str, pl.DataFrame], title: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    for name, book_df in book_series.items():
        cumulative = (book_df["net_pnl"].cum_sum() / 1e3).to_list()
        ax.plot(
            book_df["date"].to_list(),
            cumulative,
            color=COLORS[name],
            linewidth=2,
            label=LABELS[name],
        )
        ax.annotate(
            LABELS[name],
            (book_df["date"][-1], cumulative[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
            color="#444444",
        )
    ax.axhline(0, color="#888888", linewidth=0.6)
    ax.set_ylabel("cumulative gross P&L, $k")
    ax.set_title(title)
    ax.grid(axis="y", color="#e5e5e5", linewidth=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(frameon=False, loc="upper left")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_deciles(decile_df: pl.DataFrame, title: str, path: Path) -> None:
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
    ax.set_xlabel("score decile (10 = rich)")
    ax.set_ylabel(f"forward {HORIZON}-session P&L per $ vega, long straddle")
    ax.set_title(title)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def run_study(
    scores_by_signal: dict[str, pl.DataFrame], common: dict, study_dir: Path, titles: dict[str, str]
) -> pl.DataFrame:
    results_dir, figures_dir = study_dir / "results", study_dir / "figures"
    results_dir.mkdir(exist_ok=True)
    figures_dir.mkdir(exist_ok=True)
    started = time.time()
    rows, series, deciles, annuals = [], [], [], []
    for signal, scores_df in scores_by_signal.items():
        scores_df = screen_scores(common, scores_df)
        book_series = {}
        for book, make_strategy in build_strategies(common, scores_df).items():
            for cost_fraction in (0.0, 1.0):
                records_df = run_book(common, make_strategy, cost_fraction)
                rows.append(
                    summarize(signal, book, cost_fraction, records_df, common["factor_returns_df"])
                )
                if cost_fraction == 0.0:
                    book_series[book] = BacktestResults(records_df).book_df
                print(
                    f"  {signal} {book} {'net' if cost_fraction else 'gross'}"
                    f" ({time.time() - started:.0f}s)",
                    flush=True,
                )
        series.extend(
            df.select("date", "net_pnl", "gross_vega", "net_vega", "positions").with_columns(
                pl.lit(signal).alias("signal"), pl.lit(book).alias("book")
            )
            for book, df in book_series.items()
        )
        decile_df = BacktestResults.decile_table(
            scores_df, common["reference_df"], horizon_days=HORIZON
        )
        deciles.append(decile_df.with_columns(pl.lit(signal).alias("signal")))
        annuals.append(
            pl.concat(
                [df.with_columns(pl.lit(book).alias("book")) for book, df in book_series.items()]
            )
            .group_by("book", pl.col("date").dt.year().alias("year"))
            .agg(pl.col("net_pnl").sum().alias("pnl"))
            .with_columns(pl.lit(signal).alias("signal"))
        )
        plot_equity(book_series, titles[signal], figures_dir / f"equity_{signal}.png")
        plot_deciles(decile_df, titles[signal], figures_dir / f"deciles_{signal}.png")

    summary_df = pl.DataFrame(rows)
    summary_df.write_csv(results_dir / "books.csv")
    pl.concat(series).write_csv(results_dir / "book_series.csv")
    pl.concat(deciles).write_csv(results_dir / "deciles.csv")
    pl.concat(annuals).pivot(index=["signal", "year"], on="book", values="pnl").sort(
        "signal", "year"
    ).write_csv(results_dir / "annual.csv")
    pl.Config.set_tbl_width_chars(220)
    pl.Config.set_tbl_cols(20)
    pl.Config.set_tbl_rows(40)
    pl.Config.set_float_precision(3)
    print(summary_df)
    print(pl.concat(deciles).pivot(index="decile", on="signal", values="gross_fwd_pnl_per_vega"))
    print(f"done in {time.time() - started:.0f}s; results in {results_dir}")
    return summary_df
