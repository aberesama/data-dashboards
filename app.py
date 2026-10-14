import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import oracledb
import streamlit as st

TILES_FILE = Path("tiles.json")
HISTORY_FILE = Path("history.json")
CATEGORIES = ["Totals", "Nulls", "Invalids"]
# Totals going up is good; nulls and invalids going down is good.
HIGHER_IS_BETTER = {"Totals": True, "Nulls": False, "Invalids": False}

st.set_page_config(page_title="Profiling Progress", layout="wide")

st.markdown(
    """
    <style>
    .tile {border:2px solid #1f2937; border-radius:6px; padding:14px 16px;
           background:#f9fafb; margin-bottom:16px; min-height:170px;}
    .tile .name {font-size:.75rem; letter-spacing:.08em; text-transform:uppercase;
                 color:#4b5563; font-weight:600; border-bottom:1px solid #d1d5db;
                 padding-bottom:6px; margin-bottom:8px;}
    .tile .num {font-size:2.4rem; font-weight:700; color:#111827; line-height:1.1;}
    .tile .delta {font-size:.85rem; margin-top:6px; color:#374151;}
    .good {color:#047857; font-weight:600;} .bad {color:#b91c1c; font-weight:600;}
    .flat {color:#6b7280;} .err {color:#b91c1c; font-size:.8rem;}
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
    path.write_text(json.dumps(data, indent=2))


def load_config():
    cfg = load_json(TILES_FILE, {"columns": 4, "tiles": []})
    cfg.setdefault("columns", 4)
    cfg.setdefault("tiles", [])
    return cfg


# ---------- database ----------
def get_credentials():
    try:
        return dict(st.secrets.get("oracle", {}))
    except Exception:  # no secrets.toml yet
        return {}


def connect(dsn, user, password):
    return oracledb.connect(user=user, password=password, dsn=dsn)


READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


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


def refresh_all(conn, tiles, history):
    now = datetime.now().isoformat(timespec="seconds")
    for t in tiles:
        h = history.setdefault(t["id"], {})
        try:
            value = run_scalar(conn, t["sql"])
        except Exception as e:  # keep going so one bad query doesn't kill the rest
            h["error"] = str(e)[:300]
            continue
        h.pop("error", None)
        prev = h.get("last")
        if "baseline" not in h:
            h["baseline"] = {"value": value, "ts": now}
        h["previous"] = prev
        h["last"] = {"value": value, "ts": now}
    save_json(HISTORY_FILE, history)


# ---------- rendering ----------
def fmt(n):
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"


def delta_html(label, delta, higher_is_better):
    if delta is None:
        return f'<div class="delta flat">{label}: n/a</div>'
    if delta == 0:
        return f'<div class="delta flat">{label}: no change</div>'
    good = (delta > 0) == higher_is_better
    arrow = "▲" if delta > 0 else "▼"
    cls = "good" if good else "bad"
    return f'<div class="delta">{label}: <span class="{cls}">{arrow} {fmt(abs(delta))}</span></div>'


def tile_html(t, h):
    last = (h or {}).get("last")
    hib = HIGHER_IS_BETTER.get(t.get("category"), True)
    name = t["name"].replace("<", "&lt;")
    if h and h.get("error"):
        err = h["error"].replace("<", "&lt;")
        body = f'<div class="num">{fmt(last["value"]) if last else "-"}</div><div class="err">Query error: {err}</div>'
    elif not last:
        body = '<div class="num">-</div><div class="delta flat">Not refreshed yet</div>'
    else:
        v = last["value"]
        d_start = v - h["baseline"]["value"] if h.get("baseline") else None
        prev = h.get("previous")
        d_last = v - prev["value"] if prev else None
        body = (
            f'<div class="num">{fmt(v)}</div>'
            + delta_html("Since start", d_start, hib)
            + delta_html("Since last refresh", d_last, hib)
            + f'<div class="delta flat">As at {last["ts"].replace("T", " ")}</div>'
        )
    return f'<div class="tile"><div class="name">{name}</div>{body}</div>'


def render_dashboard(cfg, history):
    tiles = cfg["tiles"]
    if not tiles:
        st.info("No tiles yet. Add some in the Edit tiles tab.")
        return
    ncols = max(1, int(cfg["columns"]))
    # Totals first, then Nulls, then Invalids; unknown categories last.
    for cat in CATEGORIES + [c for c in {t.get("category") for t in tiles} if c not in CATEGORIES]:
        group = [t for t in tiles if t.get("category") == cat]
        if not group:
            continue
        st.subheader(cat)
        for i in range(0, len(group), ncols):
            cols = st.columns(ncols)
            for col, t in zip(cols, group[i : i + ncols]):
                col.markdown(tile_html(t, history.get(t["id"])), unsafe_allow_html=True)


def render_editor(cfg, history):
    st.caption("Rename tiles, set their category, paste SQL. The query must return one row with the number in the first column.")
    cfg["columns"] = st.slider("Tiles per row", 1, 6, int(cfg["columns"]))
    to_delete = None
    for t in cfg["tiles"]:
        with st.expander(f'{t["name"]}  ({t.get("category", "")})'):
            t["name"] = st.text_input("Tile name", t["name"], key=f'n_{t["id"]}')
            t["category"] = st.selectbox(
                "Category", CATEGORIES,
                index=CATEGORIES.index(t["category"]) if t.get("category") in CATEGORIES else 0,
                key=f'c_{t["id"]}',
            )
            t["sql"] = st.text_area("SQL", t["sql"], height=120, key=f'q_{t["id"]}')
            c1, c2 = st.columns(2)
            if c1.button("Reset baseline", key=f'rb_{t["id"]}'):
                history.get(t["id"], {}).pop("baseline", None)
                if history.get(t["id"], {}).get("last"):
                    history[t["id"]]["baseline"] = history[t["id"]]["last"]
                save_json(HISTORY_FILE, history)
                st.success("Baseline reset to the latest value.")
            if c2.button("Delete tile", key=f'd_{t["id"]}'):
                to_delete = t["id"]
    if to_delete:
        cfg["tiles"] = [t for t in cfg["tiles"] if t["id"] != to_delete]
        history.pop(to_delete, None)
        save_json(TILES_FILE, cfg)
        save_json(HISTORY_FILE, history)
        st.rerun()
    c1, c2 = st.columns(2)
    if c1.button("Add tile"):
        cfg["tiles"].append({"id": uuid.uuid4().hex[:8], "name": "New tile", "category": "Totals", "sql": "SELECT COUNT(*) FROM dual"})
        save_json(TILES_FILE, cfg)
        st.rerun()
    if c2.button("Save changes", type="primary"):
        save_json(TILES_FILE, cfg)
        st.success("Saved to tiles.json")


# ---------- main ----------
cfg = load_config()
history = load_json(HISTORY_FILE, {})
secrets = get_credentials()

with st.sidebar:
    st.header("Connection")
    dsn = st.text_input("Connection link (host:port/service)", secrets.get("dsn", ""))
    email = st.text_input("Email", secrets.get("email", ""))
    user = st.text_input("Login", secrets.get("user", ""), help="Leave blank to use the email as the username.")
    password = st.text_input("Password", secrets.get("password", ""), type="password")
    st.caption("Prefilled from .streamlit/secrets.toml if present.")
    do_refresh = st.button("Refresh all tiles", type="primary", use_container_width=True)

st.title("Profiling Progress")

tab_dash, tab_edit = st.tabs(["Dashboard", "Edit tiles"])

with tab_dash:
    if do_refresh:
        username = user or email
        if not (dsn and username and password):
            st.error("Connection link, login (or email) and password are required.")
        else:
            try:
                with st.spinner("Running queries..."):
                    conn = connect(dsn, username, password)
                    try:
                        refresh_all(conn, cfg["tiles"], history)
                    finally:
                        conn.close()
            except Exception as e:
                st.error(f"Connection failed: {e}")
    render_dashboard(cfg, history)

with tab_edit:
    render_editor(cfg, history)
