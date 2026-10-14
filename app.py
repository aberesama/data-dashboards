import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import oracledb
import streamlit as st

DASH_DIR = Path("dashboards")
CATEGORIES = ["Totals", "Nulls", "Invalids"]
# Totals going up is good; nulls and invalids going down is good.
HIGHER_IS_BETTER = {"Totals": True, "Nulls": False, "Invalids": False}

st.set_page_config(page_title="Profiling Progress", layout="wide")

st.markdown(
    """
    <style>
    .tile {border:2px solid #1f2937; border-radius:6px; padding:14px 16px;
           background:#f9fafb; margin-bottom:16px; min-height:190px;}
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


def refresh_all(conn, slug, tiles, history):
    now = datetime.now().isoformat(timespec="seconds")
    for t in tiles:
        h = history.setdefault(t["id"], {})
        try:
            value = run_scalar(conn, t["sql"])
        except Exception as e:  # one bad query must not kill the rest
            h["error"] = str(e)[:300]
            continue
        h.pop("error", None)
        if "baseline" not in h:
            h["baseline"] = {"value": value, "ts": now}
        h["previous"] = h.get("last")
        h["last"] = {"value": value, "ts": now}
    save_json(DASH_DIR / slug / "history.json", history)


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
def fmt(n):
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"


def delta_html(label, delta, higher_is_better, pct=None):
    if delta is None:
        return f'<div class="delta flat">{label}: n/a</div>'
    if delta == 0:
        return f'<div class="delta flat">{label}: no change</div>'
    cls = "good" if (delta > 0) == higher_is_better else "bad"
    arrow = "▲" if delta > 0 else "▼"
    extra = f" ({pct:+.1f}%)" if pct is not None else ""
    return f'<div class="delta">{label}: <span class="{cls}">{arrow} {fmt(abs(delta))}{extra}</span></div>'


def tile_html(t, h):
    last = (h or {}).get("last")
    hib = HIGHER_IS_BETTER.get(t.get("category"), True)
    name = t["name"].replace("<", "&lt;")
    if h and h.get("error"):
        err = h["error"].replace("<", "&lt;")
        num = fmt(last["value"]) if last else "-"
        body = f'<div class="num">{num}</div><div class="err">Query error: {err}</div>'
    elif not last:
        body = '<div class="num">-</div><div class="delta flat">Not refreshed yet</div>'
    else:
        v = last["value"]
        base = h.get("baseline")
        d_start = v - base["value"] if base else None
        pct = d_start / base["value"] * 100 if base and base["value"] else None
        prev = h.get("previous")
        d_last = v - prev["value"] if prev else None
        body = (
            f'<div class="num">{fmt(v)}</div>'
            + delta_html("Since last refresh", d_last, hib)
            + delta_html("Since start", d_start, hib, pct)
            + f'<div class="delta flat">Baseline {fmt(base["value"])} ({base["ts"][:10]})</div>'
            + f'<div class="delta flat">As at {last["ts"].replace("T", " ")}</div>'
        )
    return f'<div class="tile"><div class="name">{name}</div>{body}</div>'


def render_dashboard(cfg, history):
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


def render_editor(slug, cfg, history):
    st.caption("Rename tiles, set their category, paste SQL. Each query must return one row with the number in the first column.")
    cfg["title"] = st.text_input("Dashboard title", cfg["title"], key=f"title_{slug}")
    cfg["columns"] = st.slider("Tiles per row", 1, 6, int(cfg["columns"]), key=f"cols_{slug}")
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
            t["sql"] = st.text_area("SQL", t["sql"], height=120, key=f"q_{k}")
            if st.button("Delete tile", key=f"d_{k}"):
                to_delete = t["id"]
    if to_delete:
        cfg["tiles"] = [t for t in cfg["tiles"] if t["id"] != to_delete]
        history.pop(to_delete, None)
        save_json(DASH_DIR / slug / "tiles.json", cfg)
        save_json(DASH_DIR / slug / "history.json", history)
        st.rerun()
    c1, c2 = st.columns(2)
    if c1.button("Add tile", key=f"add_{slug}"):
        cfg["tiles"].append({"id": uuid.uuid4().hex[:8], "name": "New tile", "category": "Totals", "sql": "SELECT COUNT(*) FROM dual"})
        save_json(DASH_DIR / slug / "tiles.json", cfg)
        st.rerun()
    if c2.button("Save changes", type="primary", key=f"save_{slug}"):
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
        new_title = st.text_input("Name", key="new_title")
        copy_src = st.selectbox("Start from", ["Blank"] + dashboards, index=dashboards.index(slug) + 1, key="new_src")
        st.button("Create", on_click=on_create)
        if st.session_state.get("create_err"):
            st.error(st.session_state.pop("create_err"))

    st.header("Connection")
    sec = get_credentials(slug)
    url = st.text_input("Database URL (host:port/service)", sec.get("url", ""), key=f"url_{slug}")
    user = st.text_input("Username", sec.get("user", ""), key=f"user_{slug}")
    password = st.text_input("Password", sec.get("password", ""), type="password", key=f"pw_{slug}")
    st.caption("Prefilled from .streamlit/secrets.toml if present.")
    do_refresh = st.button("Refresh all tiles", type="primary", use_container_width=True)

cfg = load_config(slug)
history = load_json(DASH_DIR / slug / "history.json", {})

st.title(cfg["title"])
tab_dash, tab_edit = st.tabs(["Dashboard", "Edit tiles"])

with tab_dash:
    if do_refresh:
        if not (url and user and password):
            st.error("URL, username and password are required.")
        else:
            try:
                with st.spinner("Running queries..."):
                    conn = oracledb.connect(user=user, password=password, dsn=normalize_url(url))
                    try:
                        refresh_all(conn, slug, cfg["tiles"], history)
                    finally:
                        conn.close()
            except Exception as e:
                st.error(f"Connection failed: {e}")
    render_dashboard(cfg, history)

    with st.expander("Lock baseline"):
        st.write("Sets every tile's starting point to its latest refreshed value. 'Since start' and the percentage are measured from here.")
        confirm = st.checkbox("I understand this overwrites all current baselines", key=f"lockok_{slug}")
        if st.button("Lock baseline for all tiles", disabled=not confirm, key=f"lock_{slug}"):
            n = lock_baselines(slug, history)
            st.success(f"Locked {n} tile baselines." if n else "Nothing to lock yet. Refresh first.")
            st.rerun()

with tab_edit:
    render_editor(slug, cfg, history)
