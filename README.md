# ml-research

Volatility research on the malatium stack: signals, books and risk on S&P 500 single-name options.

The stack, in the order data flows:

| repo | role |
| --- | --- |
| [ml-data-pipelines](https://github.com/Atium-Research/ml-data-pipelines) | pulls chains, stocks and reference data into a bear-lake store and builds the derived panels: reference straddle returns, the factor risk model, the IV surface, realized vol and its forecast, stock features, signals |
| [ml-data](https://github.com/Atium-Research/ml-data) | one `load_*` per table |
| [malatium](https://github.com/Atium-Research/malatium) | the engine: strategies in dollar vega, the backtester over the reference-return panel, the optimizer, results |

A study is a folder with a script and a `REPORT.md`, and starts like this:

```python
import datetime as dt

import ml_data
from malatium.backtester import Backtester
from malatium.providers import PanelProvider, TradingCalendar
from malatium.results import BacktestResults
from malatium.strategy import QuantileSpreadStrategy

db = ml_data.connect()
start, end = dt.date(2018, 7, 2), dt.date(2025, 6, 30)
reference_df = ml_data.load_reference_returns(db, start, end)
scores_df = ml_data.load_signals(db, "vrp", start, end)

strategy = QuantileSpreadStrategy(PanelProvider(scores_df), PanelProvider(ml_data.load_universe(db, start, end)))
records_df = Backtester().run(
    TradingCalendar(ml_data.load_sessions(db)), PanelProvider(reference_df), strategy, start, end, gross_vega=20_000.0
)
print(BacktestResults(records_df).summary())
```

`reading_list.md` is the literature the signals come from.

```bash
uv sync
```
