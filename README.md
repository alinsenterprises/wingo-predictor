# WinGo Predictor

A command-line script that collects WinGo history and performs heuristic statistical analysis of past rounds.

## Important

This project is for education and data analysis. WinGo outcomes are random or pseudorandom; historical patterns do not reliably predict future results. The script's confidence and backtest figures are heuristic outputs, not guaranteed odds. Do not use them to make real-money gambling decisions.

## Source and license

The script is based on `wingo_predictor_v2.py` from [ronimahabub2021/wingo-predictor](https://github.com/ronimahabub2021/wingo-predictor). Its MIT license is included in [LICENSE](LICENSE).

## Requirements

- Python 3.8 or newer
- Internet access to retrieve the configured WinGo history endpoint
- `pandas` and `requests`

Install dependencies and start the script:

```bash
python -m pip install -r requirements.txt
python wingo_predictor_v2.py
```

The script polls for new results and stores them in `wingo_data.csv`, which is excluded from Git. Review the data source's terms and availability before running it.
