"""Time-series IV z-score: buy names whose ATM vol is cheap for itself, sell those rich.

The score is `iv_zscore` from the store: each name's log 60-day ATM implied
vol against its own trailing 250-session mean and std (at least 120
sessions), positive when rich, clipped at +/-3. Three weekly books on the
panel engine (see `utils.books`).

    uv run python -m iv_zscore.study
"""

from pathlib import Path

import ml_data_access

from utils import books

STUDY_DIR = Path(__file__).resolve().parent


def main() -> None:
    db = ml_data_access.connect()
    common = books.load_common(db)
    scores = {"iv_zscore": ml_data_access.load_signals(db, "iv_zscore", books.START, books.END)}
    titles = {"iv_zscore": "IV z-score books, $20k gross vega, weekly, no costs"}
    books.run_study(scores, common, STUDY_DIR, titles)


if __name__ == "__main__":
    main()
