# Aksjeopplysningne

Lite personlig prosjekt for momentum-screening av aksjer.

## Hva det gjør

- Leser tickere fra `watchlist.csv`
- Henter intradag-data via `yfinance` (pris, volum, høy/lav, pre/after-hours når tilgjengelig)
- Regner ut momentum-score (0-100-ish) basert på enkle regler
- Deler tickere i:
  - `A-list` (score > 70)
  - `B-list` (score 45-70)
  - `C-list` (score < 45)
- Lager både CSV-rapport og kort Markdown-rapport

> Merk: `yfinance` er uoffisielt og kan bli rate-limitet eller endre seg hvis Yahoo endrer API/dataformat.

## Kom i gang

```bash
pip install -r requirements.txt
python screener.py --input watchlist.csv --outdir .
```

Eksempel på output-filer:

- `momentum_report_YYYYMMDD_HHMM.csv`
- `momentum_report_YYYYMMDD_HHMM.md`

## Watchlist-format

`watchlist.csv` må minst ha kolonnen `ticker`.

Støttede kolonner:

- `ticker` (påkrevd)
- `category` (valgfri)
- `news` (valgfri bool: true/false)
- `sector_strength` (valgfri bool: true/false)

Eksempel:

```csv
ticker,category,news,sector_strength
FTNT,cyber,false,true
DDOG,cloud,true,true
```
