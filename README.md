# Profiling Progress Dashboard

Streamlit dashboard with tombstone tiles, each linked to an Oracle SQL query.

## Setup
```
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in
streamlit run app.py
```

## Using it
- Sidebar: connection link (`host:port/service_name`), email, login, password. Prefilled from secrets.toml.
- Edit tiles tab: rename tiles, pick category (Totals, Nulls, Invalids), paste SQL, add or delete tiles. Saved to `tiles.json`, which you can also hand edit.
- Each query must be a SELECT or WITH returning one row, number in the first column.
- Refresh all tiles runs the queries. Values are stored in `history.json`.
  - Since start: current value minus the first value ever recorded for that tile (Reset baseline in the editor changes this).
  - Since last refresh: current value minus the previous refresh.
  - Colors: Totals going up is green. Nulls and Invalids going down is green.
