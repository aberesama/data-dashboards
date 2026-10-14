import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import altair as alt
import oracledb
import pandas as pd
import streamlit as st

DASH_DIR = Path("dashboards")
CATEGORIES = ["Totals", "Nulls", "Invalids"]
UNITS = ["count", "percent"]
# Totals going up is good; nulls and invalids going down is good.
HIGHER_IS_BETTER = {"Totals": True, "Nulls": False, "Invalids": False}

st.set_page_config(page_title="Profiling Progress", layout="wide")

st.markdown(
    """
    <style>
    .tile {border:2px solid #1f2937; border-radius:6px; padding:14px 16px;
           background:#f9fafb; margin-bottom:6px; min-height:190px;}
    .tile .name {font-size:.75rem; letter-spacing:.08em; text-transform:uppercase;
                 color:#4b5563; font-weight:600; border-bottom:1px solid #d1d5db;
                 padding-bottom:6px; margin-bottom:8px;}
    .tile .num {font-size:2.4rem; font-weight:700; color:#111827; line-height:1.1;}
    .tile .delta {font-size:.85rem; margin-top:6px; color:#374151;}
    .good {color:#047857; font-weight:600;} .bad {color:#b91c1c; font-weight:600;}
    .flat {color:#6b7280;} .err {color:#b91c1c; font-size:.8rem; margin-top:8px;}
    @media (prefers-color-scheme: dark) {
      .tile {background:#111827; border-color:#6b7280;}
      .tile .name {color:#9ca3af; border-color:#374151;}
      .tile .num {color:#f9fafb;} .tile .delta {color:#d1d5db;}
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------- storage ----------
def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def list_dashboards():
    return sorted(p.name for p in DASH_DIR.glob("*") if (p / "tiles.json").exists())


def create_dashboard(title, copy_from=None):
    slug = slugify(title)
    if not slug:
        return None, "Give the dashboard a name."
    if (DASH_DIR / slug).exists():
        return None, f"A dashboard called '{slug}' already exists."
    if copy_from:
        cfg = load_json(DASH_DIR / copy_from / "tiles.json", {"tiles": []})
        for t in cfg["tiles"]:
            t["id"] = uuid.uuid4().hex[:8]
    else:
        cfg = {"columns": 4, "tiles": []}
    cfg["title"] = title
    save_json(DASH_DIR / slug / "tiles.json", cfg)  # history is never copied
    return slug, None


def load_config(slug):
    cfg = load_json(DASH_DIR / slug / "tiles.json", {})
    cfg.setdefault("title", slug)
    cfg.setdefault("columns", 4)
    cfg.setdefault("timeout_seconds", 300)
    cfg.setdefault("tiles", [])
    return cfg


# ---------- database ----------
def get_credentials(slug):
    key = "oracle_" + slug.replace("-", "_")
    try:
        sec = st.secrets.get(key) or st.secrets.get("oracle", {})
        return dict(sec)
    except Exception:  # no secrets.toml yet
        return {}


def normalize_url(url):
    """Accept a pasted JDBC style URL and reduce it to host:port/service."""
    url = url.strip()
    url = re.sub(r"^jdbc:oracle:thin:@", "", url, flags=re.IGNORECASE)
    return url.lstrip("/")


READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
TIMEOUT_CODES = ("DPI-1067", "DPI-1080", "DPY-4024")
HINTS = {
    "ORA-01017": "Wrong username or password. Do not keep retrying: repeated failures can lock the account.",
    "ORA-28000": "The account is locked. Ask your DBA to unlock it.",
    "ORA-12514": "The listener does not know this service name. Check the URL with your DBA.",
    "ORA-12541": "Nothing is listening on that host and port.",
    "ORA-12170": "Connection timed out. Check you are on the bank network or VPN.",
    "DPY-6005": "Could not reach the database. Check the URL, port and that you are on the bank network or VPN.",
    "DPY-3010": "Database version is too old for the default driver mode.",
}


def describe_error(e, timeout_s):
    """Turn a driver exception into a short message that keeps the Oracle error code."""
    text = " ".join(str(e).split())
    codes = re.findall(r"(?:ORA|DPI|DPY)-\d+", text)
    if any(c in TIMEOUT_CODES for c in codes):
        return f"Timed out after {timeout_s:g}s ({codes[0]}). Query is too slow or blocked."
    hint = next((HINTS[c] for c in codes if c in HINTS), "")
    return (text[:500] + (" " + hint if hint else "")).strip()


def connect(url, user, password, timeout_s):
    conn = oracledb.connect(user=user, password=password, dsn=normalize_url(url), tcp_connect_timeout=15)
    conn.call_timeout = int(timeout_s * 1000)  # milliseconds, per database round trip
    return conn


def is_healthy(conn):
    try:
        return conn.is_healthy()
    except Exception:
        return False


def close_quietly(conn):
    try:
        conn.close()
    except Exception:
        pass


def run_scalar(conn, sql):
    sql = sql.strip().rstrip(";").strip()
    if not READ_ONLY.match(sql):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    if row is None or row[0] is None:
        return 0
    return float(row[0])


def append_log(slug, rows):
    """Append one JSON line per successful tile refresh. Never rewritten, so history is kept."""
    if not rows:
        return
    path = DASH_DIR / slug / "log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def load_log(slug, tile_id=None):
    path = DASH_DIR / slug / "log.jsonl"
    out = []
    try:
        with path.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if tile_id is None or r.get("tile") == tile_id:
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


def refresh_tiles(slug, tiles, history, creds, timeout_s, on_progress):
    """Run tiles one at a time, saving after each. Returns an abort message or None."""
    conn = connect(*creds, timeout_s)
    logged = {r.get("tile") for r in load_log(slug)}
    try:
        for i, t in enumerate(tiles):
            on_progress(i, len(tiles), t["name"])
            h = history.setdefault(t["id"], {})
            started = time.monotonic()
            try:
                value = run_scalar(conn, t["sql"])
            except Exception as e:  # one bad query must not kill the rest
                h["error"] = describe_error(e, timeout_s)
                h["elapsed"] = round(time.monotonic() - started, 1)
                if not is_healthy(conn):  # a timeout can invalidate the connection
                    close_quietly(conn)
                    try:
                        conn = connect(*creds, timeout_s)
                    except Exception as e2:
                        save_json(DASH_DIR / slug / "history.json", history)
                        return "Lost the database connection and could not reconnect: " + describe_error(e2, timeout_s)
            else:
                now = datetime.now().isoformat(timespec="seconds")
                h.pop("error", None)
                h["elapsed"] = round(time.monotonic() - started, 1)
                if "baseline" not in h:
                    h["baseline"] = {"value": value, "ts": now}
                if h.get("last") and t["id"] not in logged:  # dashboards created before the log existed
                    append_log(slug, [{"ts": h["last"]["ts"], "tile": t["id"], "value": h["last"]["value"]}])
                h["previous"] = h.get("last")
                h["last"] = {"value": value, "ts": now}
                append_log(slug, [{"ts": now, "tile": t["id"], "value": value}])
                logged.add(t["id"])
            save_json(DASH_DIR / slug / "history.json", history)  # keep progress if interrupted
    finally:
        close_quietly(conn)  # also runs if Streamlit aborts the script, which ends the session
    return None


def lock_baselines(slug, history):
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for h in history.values():
        if h.get("last"):
            h["baseline"] = {"value": h["last"]["value"], "ts": now}
            n += 1
    save_json(DASH_DIR / slug / "history.json", history)
    return n


# ---------- rendering ----------
def fmt(n, unit="count"):
    if unit == "percent":
        return f"{n:,.2f}%"
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"


def delta_html(label, delta, higher_is_better, pct=None, unit="count"):
    if delta is None:
        return f'<div class="delta flat">{label}: n/a</div>'
    if round(delta, 6) == 0:
        return f'<div class="delta flat">{label}: no change</div>'
    cls = "good" if (delta > 0) == higher_is_better else "bad"
    arrow = "▲" if delta > 0 else "▼"
    if unit == "percent":  # change of a percentage is shown in percentage points
        text = f"{abs(delta):,.2f} pp"
    else:
        text = fmt(abs(delta)) + (f" ({pct:+.1f}%)" if pct is not None else "")
    return f'<div class="delta">{label}: <span class="{cls}">{arrow} {text}</span></div>'


def tile_html(t, h):
    h = h or {}
    last = h.get("last")
    hib = HIGHER_IS_BETTER.get(t.get("category"), True)
    unit = t.get("unit", "count")
    name = t["name"].replace("<", "&lt;")
    if not last:
        body = '<div class="num">-</div><div class="delta flat">Not refreshed yet</div>'
    else:
        v = last["value"]
        base = h.get("baseline")
        d_start = v - base["value"] if base else None
        pct = d_start / base["value"] * 100 if base and base["value"] else None
        prev = h.get("previous")
        d_last = v - prev["value"] if prev else None
        took = f' (took {h["elapsed"]:g}s)' if h.get("elapsed") is not None and not h.get("error") else ""
        body = (
            f'<div class="num">{fmt(v, unit)}</div>'
            + delta_html("Since last refresh", d_last, hib, unit=unit)
            + delta_html("Since start", d_start, hib, pct, unit)
            + (f'<div class="delta flat">Baseline {fmt(base["value"], unit)} ({base["ts"][:10]})</div>' if base else "")
            + f'<div class="delta flat">As at {last["ts"].replace("T", " ")}{took}</div>'
        )
    if h.get("error"):
        err = h["error"].replace("<", "&lt;")
        lead = "Showing last good value. " if last else ""
        body += f'<div class="err">Last refresh failed. {lead}{err}</div>'
    return f'<div class="tile"><div class="name">{name}</div>{body}</div>'


def request_refresh(ids):
    """Button callback. ids=None means every tile."""
    st.session_state["refresh_ids"] = ids if ids is not None else "all"


@st.dialog("Progress", width="large")
def show_trend(slug, t, history):
    unit = t.get("unit", "count")
    st.subheader(t["name"])
    rows = load_log(slug, t["id"])
    if not rows:
        st.info("No refresh history yet for this tile. Refresh it at least once.")
        return
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").set_index("ts")
    base = (history.get(t["id"]) or {}).get("baseline")
    axis = "Percent" if unit == "percent" else "Count"
    line = alt.Chart(df.reset_index()).mark_line(point=True).encode(
        x=alt.X("ts:T", title=None),
        y=alt.Y("value:Q", title=axis, scale=alt.Scale(zero=False)),  # zero=False so real movement is visible
        tooltip=[alt.Tooltip("ts:T", title="When"), alt.Tooltip("value:Q", title=axis, format=",.2f")],
    )
    layers = [line]
    if base:
        layers.append(
            alt.Chart(pd.DataFrame({"y": [base["value"]]})).mark_rule(strokeDash=[6, 4], color="#9ca3af").encode(y="y:Q")
        )
    st.altair_chart(alt.layer(*layers).properties(height=320, width="container"))
    if base:
        st.caption(f"Dashed line is the baseline ({fmt(base['value'], unit)}).")
    hib = HIGHER_IS_BETTER.get(t.get("category"), True)
    best = df["value"].max() if hib else df["value"].min()
    latest = df["value"].iloc[-1]
    c1, c2, c3 = st.columns(3)
    c1.metric("Latest", fmt(latest, unit))
    c2.metric("Best so far", fmt(best, unit))
    c3.metric("First recorded", fmt(df["value"].iloc[0], unit))
    with st.expander("All refreshes"):
        table = df[["value"]].rename(columns={"value": "Value"}).sort_index(ascending=False)
        st.dataframe(table)


def render_dashboard(slug, cfg, history):
    tiles = cfg["tiles"]
    if not tiles:
        st.info("No tiles yet. Add some in the Edit tiles tab.")
        return
    ncols = max(1, int(cfg["columns"]))
    others = [c for c in dict.fromkeys(t.get("category") for t in tiles) if c not in CATEGORIES]
    for cat in CATEGORIES + others:
        group = [t for t in tiles if t.get("category") == cat]
        if not group:
            continue
        st.subheader(cat)
        for i in range(0, len(group), ncols):
            cols = st.columns(ncols)
            for col, t in zip(cols, group[i : i + ncols]):
                col.markdown(tile_html(t, history.get(t["id"])), unsafe_allow_html=True)
                b1, b2 = col.columns(2)
                b1.button("↻ Refresh", key=f"r_{slug}_{t['id']}", on_click=request_refresh, args=([t["id"]],))
                if b2.button("Trend", key=f"v_{slug}_{t['id']}"):
                    show_trend(slug, t, history)


def render_editor(slug, cfg, history):
    st.caption("Rename tiles, set their category, paste SQL. Each query must return one row with the number in the first column.")
    cfg["title"] = st.text_input("Dashboard title", cfg["title"], key=f"title_{slug}")
    c1, c2 = st.columns(2)
    cfg["columns"] = c1.slider("Tiles per row", 1, 6, int(cfg["columns"]), key=f"cols_{slug}")
    cfg["timeout_seconds"] = c2.number_input(
        "Query timeout (seconds, per tile)", min_value=10, max_value=3600, step=30,
        value=int(cfg["timeout_seconds"]), key=f"timeout_{slug}",
        help="A query that runs longer is abandoned and the tile keeps its last good value.",
    )
    to_delete = None
    for t in cfg["tiles"]:
        k = f'{slug}_{t["id"]}'
        with st.expander(f'{t["name"]}  ({t.get("category", "")})'):
            t["name"] = st.text_input("Tile name", t["name"], key=f"n_{k}")
            t["category"] = st.selectbox(
                "Category", CATEGORIES,
                index=CATEGORIES.index(t["category"]) if t.get("category") in CATEGORIES else 0,
                key=f"c_{k}",
            )
            t["unit"] = st.selectbox(
                "Unit", UNITS, index=UNITS.index(t["unit"]) if t.get("unit") in UNITS else 0, key=f"u_{k}",
                help="count: the query returns a number. percent: the query returns the percentage itself, "
                     "e.g. 100 * SUM(CASE WHEN bad THEN 1 ELSE 0 END) / COUNT(*).",
            )
            t["sql"] = st.text_area("SQL", t["sql"], height=120, key=f"q_{k}")
            if st.button("Delete tile", key=f"d_{k}"):
                to_delete = t["id"]
    if to_delete:
        cfg["tiles"] = [t for t in cfg["tiles"] if t["id"] != to_delete]
        history.pop(to_delete, None)
        save_json(DASH_DIR / slug / "tiles.json", cfg)
        save_json(DASH_DIR / slug / "history.json", history)
        st.rerun()
    b1, b2 = st.columns(2)
    if b1.button("Add tile", key=f"add_{slug}"):
        cfg["tiles"].append({"id": uuid.uuid4().hex[:8], "name": "New tile", "category": "Totals", "sql": "SELECT COUNT(*) FROM dual"})
        save_json(DASH_DIR / slug / "tiles.json", cfg)
        st.rerun()
    if b2.button("Save changes", type="primary", key=f"save_{slug}"):
        save_json(DASH_DIR / slug / "tiles.json", cfg)
        st.success(f"Saved to dashboards/{slug}/tiles.json")


# ---------- main ----------
if not list_dashboards():
    create_dashboard("Profiling Progress")
dashboards = list_dashboards()


def on_create():
    src = st.session_state.get("new_src", "Blank")
    new_slug, err = create_dashboard(st.session_state.get("new_title", ""), None if src == "Blank" else src)
    if err:
        st.session_state["create_err"] = err
    else:
        st.session_state["current_dashboard"] = new_slug


with st.sidebar:
    st.header("Dashboard")
    slug = st.selectbox("Open dashboard", dashboards, key="current_dashboard")
    with st.expander("New dashboard"):
        st.text_input("Name", key="new_title")
        st.selectbox("Start from", ["Blank"] + dashboards, index=dashboards.index(slug) + 1, key="new_src")
        st.button("Create", on_click=on_create)
        if st.session_state.get("create_err"):
            st.error(st.session_state.pop("create_err"))

    st.header("Connection")
    sec = get_credentials(slug)
    url = st.text_input("Database URL (host:port/service)", sec.get("url", ""), key=f"url_{slug}")
    user = st.text_input("Username", sec.get("user", ""), key=f"user_{slug}")
    password = st.text_input("Password", sec.get("password", ""), type="password", key=f"pw_{slug}")
    st.caption("Prefilled from .streamlit/secrets.toml if present.")
    st.button("Refresh all tiles", type="primary", use_container_width=True, on_click=request_refresh, args=(None,))

cfg = load_config(slug)
history = load_json(DASH_DIR / slug / "history.json", {})

st.title(cfg["title"])
tab_dash, tab_edit = st.tabs(["Dashboard", "Edit tiles"])

with tab_dash:
    wanted = st.session_state.pop("refresh_ids", None)
    if wanted:
        to_run = cfg["tiles"] if wanted == "all" else [t for t in cfg["tiles"] if t["id"] in wanted]
        if not (url and user and password):
            st.error("URL, username and password are required.")
        elif to_run:
            timeout_s = float(cfg["timeout_seconds"])
            bar = st.progress(0.0)
            status = st.empty()

            def on_progress(i, n, name):
                bar.progress(i / n)
                status.info(f"Running {i + 1} of {n}: {name} (timeout {timeout_s:g}s per tile)")

            try:
                abort = refresh_tiles(slug, to_run, history, (url, user, password), timeout_s, on_progress)
            except Exception as e:
                abort = "Connection failed: " + describe_error(e, timeout_s)
            bar.empty()
            status.empty()
            failed = sum(1 for t in to_run if history.get(t["id"], {}).get("error"))
            if abort:
                st.error(abort)
            elif failed:
                st.warning(f"Refreshed {len(to_run) - failed} of {len(to_run)} tiles. {failed} failed, see the red notes on those tiles.")
            else:
                st.success(f"Refreshed {len(to_run)} tile(s).")
    render_dashboard(slug, cfg, history)

    with st.expander("Lock baseline"):
        st.write("Sets every tile's starting point to its latest refreshed value. 'Since start' and the percentage are measured from here.")
        confirm = st.checkbox("I understand this overwrites all current baselines", key=f"lockok_{slug}")
        if st.button("Lock baseline for all tiles", disabled=not confirm, key=f"lock_{slug}"):
            n = lock_baselines(slug, history)
            st.success(f"Locked {n} tile baselines." if n else "Nothing to lock yet. Refresh first.")
            st.rerun()

with tab_edit:
    render_editor(slug, cfg, history)
