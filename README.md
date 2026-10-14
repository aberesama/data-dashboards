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
- Unit: a tile is either a count or a percent. For percent, the query returns the percentage itself, for example `SELECT 100 * SUM(CASE WHEN email NOT LIKE '%@%' THEN 1 ELSE 0 END) / COUNT(*) FROM t`. Changes in percent tiles are shown in percentage points (pp).
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

## History and the Trend button
- Every successful refresh is appended to `dashboards/<name>/log.jsonl` (local, gitignored, never rewritten). Back this file up along with `history.json`.
- Each tile has a Trend button that opens a chart of its values over time, with the baseline as a dashed line and the best value reached so far.
- Dashboards that existed before the log was added get their last known value copied into the log on the next refresh.

## Network lock
`.streamlit/config.toml` makes the app listen on localhost only, so other machines on the network cannot open it. Do not remove it while credentials are saved in secrets.toml.

## Older Oracle databases (error DPY-3010)
If the app says "connections to this database server version are not supported by python-oracledb in thin mode", your database is older than 12.1. Use thick mode:
1. Download Oracle Instant Client 19c for your system. It must match your Python (64 bit Python needs 64 bit Instant Client). Basic or Basic Light is enough.
2. Unzip it to a folder you can write to. No installer or admin rights are normally needed.
3. In `.streamlit/secrets.toml`, under `[oracle]`, add `instant_client_dir = 'C:\path\to\instantclient_19_21'` (single quotes).
4. Restart the app. The sidebar should say "Driver mode: thick (Instant Client)".
Notes: thick mode is set once per app process, so restart after changing it. A query timeout needs Oracle Client 18c or newer.
