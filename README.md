# Profiling Progress Dashboards

Streamlit dashboards with tombstone tiles, each tile driven by an Oracle SQL query.

## Setup
```
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in url, user, password
streamlit run app.py
```

## Using it
- Sidebar: pick a dashboard, enter database URL (`host:port/service_name`), username and password. Prefilled from secrets.toml.
- Edit tiles tab: rename tiles, pick category (Totals, Nulls, Invalids), paste SQL, add or delete tiles.
- Each query must be a SELECT or WITH returning one row, number in the first column.
- Refresh all tiles runs the queries. Each tile shows: the number, change since last refresh, change since start (number and %), and the baseline.
  - Baseline defaults to the first value ever recorded. Use Lock baseline (Dashboard tab) to reset all tiles to their latest value.
  - Totals going up is green. Nulls and Invalids going down is green.

## New dashboards (the template)
Sidebar, New dashboard, choose Blank or an existing dashboard to copy. A copy takes tiles and SQL but not history or baselines.
Each dashboard is a folder: `dashboards/<name>/tiles.json` (committed) and `history.json` (local, gitignored).
For a different database per dashboard, add an `[oracle_<name>]` section to secrets.toml (see the example file).

## Slow queries and errors
- Each query has a timeout (Edit tiles tab, default 300 seconds, saved per dashboard). A query that runs longer is abandoned and the tile keeps its last good value with a red note.
- Tiles run one at a time. Results are saved after every tile, so an interrupted or crashed refresh loses nothing already finished.
- Every tile has its own Refresh button, so one slow tile does not hold up the rest.
- Errors keep the Oracle code (ORA-, DPI-, DPY-) and add a hint for common ones. A bad query only affects its own tile. A lost connection is re-established once per failing tile; if that fails, the refresh stops and says so.
- Clicking elsewhere in the app while a refresh runs restarts the page. Finished tiles are kept, the connection closes, and you can refresh again.
- The timeout is client side. Ask your DBA about server side limits (profiles or Resource Manager) if you need a hard cap on the database itself.
