import base64
import json
import re
import time
import uuid
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

import altair as alt
import oracledb
import pandas as pd
import streamlit as st

import oracle_client

DASH_DIR = Path("dashboards")
CATEGORIES = ["Totals", "Nulls", "Invalids"]
UNITS = ["count", "percent"]
ORG_NAME = "Family Bank Limited"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"  # optional: put your official logo here as logo.png, logo.jpg or logo.svg
FILTER_TYPES = ["date_range", "choice", "text"]
FILTER_LABELS = {"date_range": "Date range", "choice": "Dropdown loaded from the database", "text": "Free text"}
NAME_OK = re.compile(r"[a-z][a-z0-9_]{0,19}")
RESERVED = {"date", "number", "user", "level", "size", "comment", "from", "to", "select", "where", "group", "order",
            "table", "start", "end", "uid", "rowid", "rownum", "sysdate", "session", "access", "null", "and", "or", "not"}  # partial list
# Totals going up is good; nulls and invalids going down is good.
HIGHER_IS_BETTER = {"Totals": True, "Nulls": False, "Invalids": False}

st.set_page_config(page_title="Profiling Progress", layout="wide")

st.markdown(
    """
    <style>
    /* Family Bank palette. Blue #2A5BA8 sampled from the logo, cyan #2AABE4 from the bank's site stylesheet and logo.
       The navy is a darker shade of the blue, not an official colour. Green and red keep their meaning (better and worse). */
    :root {--fb-blue:#2A5BA8; --fb-cyan:#2AABE4; --fb-navy:#173A73;}
    .stApp h1 {color:var(--fb-blue) !important;}
    .tile {border:1px solid #cbd5e1; border-top:6px solid var(--fb-blue); border-radius:4px; padding:14px 16px;
           background:#ffffff; margin-bottom:6px; min-height:190px; box-shadow:0 1px 3px rgba(23,58,115,.12);}
    .tile .name {font-size:.75rem; letter-spacing:.08em; text-transform:uppercase;
                 color:var(--fb-blue); font-weight:700; border-bottom:1px solid #e2e8f0;
                 padding-bottom:6px; margin-bottom:8px;}
    .tile .num {font-size:2.4rem; font-weight:700; color:var(--fb-navy); line-height:1.1;}
    .tile .delta {font-size:.85rem; margin-top:6px; color:#334155;}
    .good {color:#047857; font-weight:600;} .bad {color:#b91c1c; font-weight:600;}
    .flat {color:#64748b;} .err {color:#b91c1c; font-size:.8rem; margin-top:8px;}
    .tile.filtered {border-top-color:var(--fb-cyan);}
    .tile.unaffected {opacity:.5;}
    .fb-bar {background:var(--fb-blue); color:#ffffff; padding:10px 18px; border-radius:4px; border-bottom:4px solid var(--fb-cyan);
             display:flex; align-items:center; gap:14px; margin-bottom:8px;}
    .fb-bar img {height:34px;}
    .fb-bar .fb-name {font-weight:700; letter-spacing:.14em; font-size:.95rem;}
    .fb-bar .fb-sub {opacity:.85; font-size:.85rem; margin-left:auto;}
    @media (prefers-color-scheme: dark) {
      .stApp h1 {color:var(--fb-cyan) !important;}
      .tile {background:#0f1b33; border-color:#2b3f66; border-top-color:var(--fb-cyan); box-shadow:none;}
      .tile .name {color:var(--fb-cyan); border-color:#2b3f66;}
      .tile .num {color:#ffffff;} .tile .delta {color:#cbd5e1;}
      .good {color:#34d399;} .bad {color:#f87171;} .flat {color:#94a3b8;} .err {color:#f87171;}
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
    cfg.setdefault("filters", [])
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
    "DPY-3010": "This database is older than 12.1, so the default (thin) driver mode cannot connect. "
                "Open 'Older database? Set up Oracle Client' in the sidebar and click Download and set up.",
    "ORA-01017": "Wrong username or password. Do not keep retrying: repeated failures can lock the account.",
    "ORA-28000": "The account is locked. Ask your DBA to unlock it.",
    "ORA-12514": "The listener does not know this service name. Check the URL with your DBA.",
    "ORA-12541": "Nothing is listening on that host and port.",
    "ORA-12170": "Connection timed out. Check you are on the bank network or VPN.",
    "DPY-6005": "Could not reach the database. Check the URL, port and that you are on the bank network or VPN.",
}  # dict order is priority: the most specific cause comes first


def describe_error(e, timeout_s):
    """Turn a driver exception into a short message that keeps the Oracle error code."""
    text = " ".join(str(e).split())
    codes = re.findall(r"(?:ORA|DPI|DPY)-\d+", text)
    if any(c in TIMEOUT_CODES for c in codes):
        return f"Timed out after {timeout_s:g}s ({codes[0]}). Query is too slow or blocked."
    hint = next((h for code, h in HINTS.items() if code in codes), "")
    return (text[:500] + (" " + hint if hint else "")).strip()


TIMEOUT_STATE = {"ok": True}


def connect(url, user, password, timeout_s):
    kwargs = {"tcp_connect_timeout": 15} if oracledb.is_thin_mode() else {}
    conn = oracledb.connect(user=user, password=password, dsn=normalize_url(url), **kwargs)
    set_timeout(conn, timeout_s)
    try:
        conn.module = "Profiling Dashboard"  # shows in V$SESSION.MODULE so the DBA can identify this app
    except Exception:
        pass
    return conn


def set_timeout(conn, timeout_s):
    try:
        conn.call_timeout = int(timeout_s * 1000)  # milliseconds, per database round trip
    except Exception:  # thick mode with an Oracle Client older than 18c
        TIMEOUT_STATE["ok"] = False


@st.cache_resource
def enable_thick_mode(lib_dir):
    """Must run before the first connection. Raises on failure, so a failure is not cached."""
    oracledb.init_oracle_client(lib_dir=lib_dir)
    return lib_dir


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


BIND_RE = re.compile(r"(?<![\w:]):([A-Za-z][A-Za-z0-9_]*)")


def filter_bind_names(f):
    return [f["name"] + "_from", f["name"] + "_to"] if f["type"] == "date_range" else [f["name"]]


def tile_uses_filters(t, filters):
    mine = {n.lower() for n in BIND_RE.findall(t["sql"])}
    return bool(mine & {b for f in filters for b in filter_bind_names(f)})


def bind_plan(filters, values=None):
    """Bind values for every filter. With no values everything is None, meaning 'no restriction'."""
    values, binds, dates = values or {}, {}, set()
    for f in filters:
        v = values.get(f["id"])
        if f["type"] == "date_range":
            v = v or {}
            lo, hi = v.get("from"), v.get("to")
            binds[f["name"] + "_from"] = datetime.combine(lo, dtime.min) if lo else None
            binds[f["name"] + "_to"] = datetime.combine(hi, dtime(23, 59, 59)) if hi else None  # whole end day included
            dates |= {f["name"] + "_from", f["name"] + "_to"}
        elif f["type"] == "choice":
            if isinstance(v, (list, tuple)):
                v = ",".join(v)
            binds[f["name"]] = v or None
        else:
            binds[f["name"]] = (v or "").strip() or None
    return binds, dates


def run_scalar(conn, sql, binds=None, dates=()):
    sql = sql.strip().rstrip(";").strip()
    if not READ_ONLY.match(sql):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    # Only pass binds the statement actually mentions, because Oracle rejects extra ones.
    use = {n: binds[n.lower()] for n in BIND_RE.findall(sql) if binds and n.lower() in binds}
    with conn.cursor() as cur:
        if use:
            date_keys = {n: oracledb.DB_TYPE_DATE for n in use if n.lower() in dates}
            if date_keys:
                cur.setinputsizes(**date_keys)
            cur.execute(sql, use)
        else:
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


class DbSession:
    """Holds at most ONE open database connection, reused for every tile and every dashboard."""

    def __init__(self):
        self.conn = None
        self.creds = None

    def open(self, creds, timeout_s):
        if self.conn is not None and self.creds == creds and is_healthy(self.conn):
            set_timeout(self.conn, timeout_s)
            return self.conn
        self.close()  # never two sessions at once: close the old one before opening another
        self.conn = connect(*creds, timeout_s)
        self.creds = creds
        return self.conn

    def reset(self, timeout_s):
        creds = self.creds
        self.close()
        return self.open(creds, timeout_s)

    def close(self):
        if self.conn is not None:
            close_quietly(self.conn)
        self.conn = None


def refresh_tiles(slug, tiles, history, session, creds, timeout_s, on_progress, unset=None):
    """Run tiles one at a time on the shared session, saving after each. Returns an abort message or None."""
    logged = {r.get("tile") for r in load_log(slug)}
    session.open(creds, timeout_s)
    for i, t in enumerate(tiles):
        on_progress(i, len(tiles), t["name"])
        h = history.setdefault(t["id"], {})
        started = time.monotonic()
        try:
            value = run_scalar(session.conn, t["sql"], *(unset or ({}, set())))
        except Exception as e:  # one bad query must not kill the rest
            h["error"] = describe_error(e, timeout_s)
            h["elapsed"] = round(time.monotonic() - started, 1)
            if not is_healthy(session.conn):  # a timeout can invalidate the connection
                try:
                    session.reset(timeout_s)
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
    return None


def run_jobs(jobs, histories, on_progress):
    """Run one or many dashboards on ONE database session, one tile at a time.

    jobs: list of dicts with slug, tiles, creds (url, user, password) and timeout_s.
    Returns (rows, error). rows has one summary dict per dashboard.
    """
    if not oracle_client.REFRESH_LOCK.acquire(blocking=False):
        return [], "Another refresh is already running in this app. Wait for it to finish, then try again."
    TIMEOUT_STATE["ok"] = True
    session = DbSession()
    rows, bad_logins, done = [], set(), 0
    total = sum(len(j["tiles"]) for j in jobs)
    try:
        for j in jobs:
            slug, tiles = j["slug"], j["tiles"]
            row = {"Dashboard": slug, "Tiles": len(tiles), "Failed": 0, "Note": ""}
            rows.append(row)
            if not all(j["creds"]):
                row["Note"] = "Skipped: URL, username or password missing"
                done += len(tiles)
                continue
            if j["creds"] in bad_logins:  # never retry a login that just failed: repeated failures lock accounts
                row["Note"] = "Skipped: the same login already failed"
                done += len(tiles)
                continue

            def progress(i, n, name, slug=slug, base=done):
                on_progress(base + i, total, slug, name)

            try:
                abort = refresh_tiles(slug, tiles, histories[slug], session, j["creds"], j["timeout_s"], progress, j.get("unset"))
            except Exception as e:  # could not connect at all, so no tile of this dashboard was refreshed
                abort = "Connection failed: " + describe_error(e, j["timeout_s"])
                bad_logins.add(j["creds"])
                row["Failed"] = len(tiles)
            else:
                row["Failed"] = sum(1 for t in tiles if histories[slug].get(t["id"], {}).get("error"))
            row["Note"] = abort or ""
            done += len(tiles)
    finally:
        session.close()  # also runs if Streamlit stops the script, which ends the database session
        oracle_client.REFRESH_LOCK.release()
    return rows, None


def run_with_session(creds, timeout_s, work):
    """Run work(session) on one short lived session under the app wide lock. Returns (result, error)."""
    if not oracle_client.REFRESH_LOCK.acquire(blocking=False):
        return None, "Another refresh is already running in this app. Wait for it to finish, then try again."
    TIMEOUT_STATE["ok"] = True
    session = DbSession()
    try:
        session.open(creds, timeout_s)
        return work(session), None
    except Exception as e:
        return None, "Could not run: " + describe_error(e, timeout_s)
    finally:
        session.close()
        oracle_client.REFRESH_LOCK.release()


def run_filtered(session, tiles, binds, dates, timeout_s, on_progress):
    """Run tiles with filter values. Results are returned for display only and never saved."""
    out = {}
    for i, t in enumerate(tiles):
        on_progress(i, len(tiles), t["name"])
        started = time.monotonic()
        try:
            out[t["id"]] = {"value": run_scalar(session.conn, t["sql"], binds, dates)}
        except Exception as e:
            out[t["id"]] = {"error": describe_error(e, timeout_s)}
            if not is_healthy(session.conn):
                try:
                    session.reset(timeout_s)
                except Exception as e2:
                    for rest in tiles[i + 1:]:
                        out[rest["id"]] = {"error": "Lost the database connection: " + describe_error(e2, timeout_s)}
                    out[t["id"]]["elapsed"] = round(time.monotonic() - started, 1)
                    for r in out.values():
                        r.setdefault("elapsed", 0.0)
                    return out
        out[t["id"]]["elapsed"] = round(time.monotonic() - started, 1)
    return out


def load_options(conn, sql, cap=500):
    sql = sql.strip().rstrip(";").strip()
    if not READ_ONLY.match(sql):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchmany(cap + 1)
    return {"values": [str(r[0]) for r in rows[:cap] if r[0] is not None], "truncated": len(rows) > cap}


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


def tile_html(t, h, mode=None, fr=None):
    h = h or {}
    last = h.get("last")
    hib = HIGHER_IS_BETTER.get(t.get("category"), True)
    unit = t.get("unit", "count")
    name = t["name"].replace("<", "&lt;")
    if mode == "filtered":
        if fr.get("error"):
            body = '<div class="num">-</div><div class="err">' + fr["error"].replace("<", "&lt;") + "</div>"
        else:
            body = (f'<div class="num">{fmt(fr["value"], unit)}</div>'
                    '<div class="delta flat">Filtered view, not saved</div>'
                    f'<div class="delta flat">Took {fr.get("elapsed", 0):g}s</div>')
        return f'<div class="tile filtered"><div class="name">{name}</div>{body}</div>'
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
    if mode == "unaffected":
        body += '<div class="delta flat">Not affected by the filters</div>'
    return f'<div class="{"tile unaffected" if mode == "unaffected" else "tile"}"><div class="name">{name}</div>{body}</div>'


def request_refresh(ids):
    """Button callback. ids=None means every tile of the open dashboard; "everything" means every dashboard."""
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
    line = alt.Chart(df.reset_index()).mark_line(point=alt.OverlayMarkDef(color="#2A5BA8"), color="#2A5BA8").encode(
        x=alt.X("ts:T", title=None),
        y=alt.Y("value:Q", title=axis, scale=alt.Scale(zero=False)),  # zero=False so real movement is visible
        tooltip=[alt.Tooltip("ts:T", title="When"), alt.Tooltip("value:Q", title=axis, format=",.2f")],
    )
    layers = [line]
    if base:
        layers.append(
            alt.Chart(pd.DataFrame({"y": [base["value"]]})).mark_rule(strokeDash=[6, 4], color="#2AABE4").encode(y="y:Q")
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


def render_dashboard(slug, cfg, history, fview=None):
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
                mode, fr = None, None
                if fview:
                    fr = fview["tiles"].get(t["id"])
                    mode = "filtered" if fr and tile_uses_filters(t, cfg["filters"]) else "unaffected"
                col.markdown(tile_html(t, history.get(t["id"]), mode, fr), unsafe_allow_html=True)
                b1, b2 = col.columns(2)
                b1.button("↻ Refresh", key=f"r_{slug}_{t['id']}", on_click=request_refresh, args=([t["id"]],))
                if b2.button("Trend", key=f"v_{slug}_{t['id']}"):
                    show_trend(slug, t, history)


def filter_summary(filters, values):
    parts = []
    for f in filters:
        v = values.get(f["id"])
        if f["type"] == "date_range":
            lo, hi = (v or {}).get("from"), (v or {}).get("to")
            if lo or hi:
                parts.append(f'{f["label"]}: {lo or "any"} to {hi or "any"}')
        elif v and (not isinstance(v, str) or v.strip()):
            parts.append(f'{f["label"]}: {", ".join(v) if isinstance(v, list) else v.strip()}')
    return "; ".join(parts) or "no filter values set"


def clear_filters(slug, filters):
    st.session_state.pop(f"fview_{slug}", None)
    for f in filters:
        key = f"ff_{slug}_{f['id']}"
        if f["type"] == "date_range":
            st.session_state[key + "_from"] = None
            st.session_state[key + "_to"] = None
        elif f["type"] == "choice":
            st.session_state[key] = [] if f.get("multi") else "All"
        else:
            st.session_state[key] = ""


def render_filters(slug, cfg, creds):
    """Filter panel. Returns the active filtered view (or None). Queries the database only when Apply is clicked."""
    filters = cfg["filters"]
    if not filters:
        return None
    fview = st.session_state.get(f"fview_{slug}")
    timeout_s = float(cfg["timeout_seconds"])
    with st.expander("Filters", expanded=bool(fview)):
        st.caption("Pick values, then click Apply. Only tiles whose SQL uses these filters are queried. "
                   "Filtered numbers are a view only: nothing is saved, and baselines and Trend are not affected.")
        values = {}
        cols = st.columns(min(3, len(filters)))
        for i, f in enumerate(filters):
            key = f"ff_{slug}_{f['id']}"
            with cols[i % len(cols)]:
                if f["type"] == "date_range":
                    st.markdown(f"**{f['label']}**")
                    a, b = st.columns(2)
                    values[f["id"]] = {"from": a.date_input("From", value=None, key=key + "_from"),
                                       "to": b.date_input("To", value=None, key=key + "_to")}
                elif f["type"] == "choice":
                    opts_key = f"fopts_{slug}_{f['id']}"
                    if st.button(f"Load {f['label']} values", key=key + "_load", disabled=not f.get("options_sql")):
                        if not all(creds):
                            st.error("Fill in URL, username and password in the sidebar first.")
                        else:
                            res, err = run_with_session(creds, timeout_s, lambda sess, f=f: load_options(sess.conn, f["options_sql"]))
                            if err:
                                st.error(err)
                            else:
                                st.session_state[opts_key] = res["values"]
                                if res["truncated"]:
                                    st.warning("Only the first 500 values are shown. Narrow the options query.")
                    opts = st.session_state.get(opts_key, [])
                    if f.get("multi"):
                        values[f["id"]] = st.multiselect(f["label"], opts, key=key)
                    else:
                        picked = st.selectbox(f["label"], ["All"] + opts, key=key)
                        values[f["id"]] = None if picked == "All" else picked
                else:
                    values[f["id"]] = st.text_input(f["label"], key=key)
        c1, c2 = st.columns(2)
        apply = c1.button("Apply filters (queries the database)", type="primary", key=f"fapply_{slug}")
        c2.button("Clear filters", key=f"fclear_{slug}", on_click=clear_filters, args=(slug, filters))
        if apply:
            binds, dates = bind_plan(filters, values)
            uses = [t for t in cfg["tiles"] if tile_uses_filters(t, filters)]
            if not uses:
                st.warning("No tile uses these filters yet. Add the placeholders to a tile's SQL in Edit tiles.")
            elif not all(creds):
                st.error("URL, username and password are required.")
            else:
                bar, status = st.progress(0.0), st.empty()

                def prog(i, n, name):
                    bar.progress(i / n)
                    status.info(f"Filtering: {name} (tile {i + 1} of {n}, one database session)")

                res, err = run_with_session(creds, timeout_s, lambda sess: run_filtered(sess, uses, binds, dates, timeout_s, prog))
                bar.empty()
                status.empty()
                if err:
                    st.warning(err)
                else:
                    fview = {"summary": filter_summary(filters, values), "tiles": res}
                    st.session_state[f"fview_{slug}"] = fview
    if fview:
        st.info(f"Filtered view ({fview['summary']}). Numbers are not saved, and baselines and Trend are unchanged. "
                "Tiles that do not use the filters are dimmed. Click Clear filters to go back.")
    return fview


def validate_filter(f, filters):
    name = f["name"]
    if not NAME_OK.fullmatch(name):
        return "Bind name must be lowercase letters, digits and underscores, start with a letter, and be at most 20 characters."
    if name in RESERVED:
        return f"'{name}' is an Oracle reserved word and cannot be a bind name. Pick another."
    taken = [b for other in filters if other is not f for b in filter_bind_names(other)]
    if any(b in taken for b in filter_bind_names(f)):
        return "Another filter already uses this bind name."
    return ""


def sql_hint(f):
    n = f["name"]
    if f["type"] == "date_range":
        return (f"AND (:{n}_from IS NULL OR your_date_column >= :{n}_from)\n"
                f"AND (:{n}_to IS NULL OR your_date_column <= :{n}_to)")
    if f["type"] == "choice" and f.get("multi"):
        return f"AND (:{n} IS NULL OR INSTR(',' || :{n} || ',', ',' || your_column || ',') > 0)"
    return f"AND (:{n} IS NULL OR your_column = :{n})"


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
    fnames = {b for f in cfg["filters"] for b in filter_bind_names(f)}
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
            used = [n for n in dict.fromkeys(x.lower() for x in BIND_RE.findall(t["sql"])) if n in fnames]
            if used:
                st.caption("Uses filters: " + ", ".join(":" + n for n in used))
            if st.button("Delete tile", key=f"d_{k}"):
                to_delete = t["id"]
    if to_delete:
        cfg["tiles"] = [t for t in cfg["tiles"] if t["id"] != to_delete]
        history.pop(to_delete, None)
        save_json(DASH_DIR / slug / "tiles.json", cfg)
        save_json(DASH_DIR / slug / "history.json", history)
        st.rerun()
    st.subheader("Filters")
    st.caption("A filter is a placeholder you write into a tile's SQL. Tiles without the placeholder ignore the filter. "
               "Leaving a filter empty means no restriction, so use the (:name IS NULL OR ...) pattern shown under each filter.")
    del_f = None
    for f in cfg["filters"]:
        fk = f"{slug}_{f['id']}"
        with st.expander(f"{f['label']}  ({FILTER_LABELS.get(f['type'], f['type'])})"):
            f["label"] = st.text_input("Label shown on the dashboard", f["label"], key=f"fl_{fk}")
            f["name"] = st.text_input("Bind name used in SQL", f["name"], key=f"fn_{fk}")
            f["type"] = st.selectbox("Type", FILTER_TYPES, index=FILTER_TYPES.index(f["type"]) if f["type"] in FILTER_TYPES else 2,
                                     format_func=lambda x: FILTER_LABELS[x], key=f"ft_{fk}")
            if f["type"] == "choice":
                f["options_sql"] = st.text_area("Options query (first column, first 500 rows)", f.get("options_sql", ""), height=80, key=f"fo_{fk}",
                                                help="For example: SELECT DISTINCT branch_name FROM branches ORDER BY 1")
                f["multi"] = st.checkbox("Allow several values (joined with commas, so values must not contain commas)",
                                         f.get("multi", False), key=f"fm_{fk}")
            problem = validate_filter(f, cfg["filters"])
            if problem:
                st.error(problem)
            st.caption("Add this to the tile SQL (dates: the end date includes the whole day):")
            st.code(sql_hint(f), language="sql")
            if st.button("Delete filter", key=f"fd_{fk}"):
                del_f = f["id"]
    if del_f:
        cfg["filters"] = [f for f in cfg["filters"] if f["id"] != del_f]
        save_json(DASH_DIR / slug / "tiles.json", cfg)
        st.rerun()
    if st.button("Add filter", key=f"addf_{slug}"):
        cfg["filters"].append({"id": uuid.uuid4().hex[:8], "label": "New filter", "name": f"filter{len(cfg['filters']) + 1}", "type": "text"})
        save_json(DASH_DIR / slug / "tiles.json", cfg)
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
    # Thick mode is process wide, so the folder comes from [oracle] or from ./oracle_client, never per dashboard.
    try:
        lib_dir = get_credentials("").get("instant_client_dir") or oracle_client.find_client()
    except RuntimeError:  # unsupported platform for the automatic setup
        lib_dir = None
    if lib_dir:
        try:
            enable_thick_mode(lib_dir)
        except Exception as e:
            st.error("Could not load Oracle Instant Client, staying in thin mode: " + " ".join(str(e).split())[:300])
    st.caption("Driver mode: " + ("thin" if oracledb.is_thin_mode() else "thick (Instant Client)"))
    if oracledb.is_thin_mode() and not lib_dir:
        with st.expander("Older database? Set up Oracle Client"):
            st.write("Databases older than 12.1 (error DPY-3010) need Oracle Instant Client 19c. "
                     "This downloads it (about 40 MB) from download.oracle.com into the app's oracle_client folder.")
            if st.button("Download and set up", key="setup_client"):
                try:
                    with st.status("Setting up Oracle Client...", expanded=True) as box:
                        found = oracle_client.install(log=box.write)
                        try:
                            enable_thick_mode(found)
                        except Exception:
                            box.update(label="Downloaded. Stop and restart the app once to finish.", state="complete")
                            st.warning("Downloaded, but thick mode can only be switched on at startup. Restart the app once.")
                        else:
                            box.update(label="Oracle Client ready.", state="complete")
                            st.rerun()
                except Exception as e:
                    st.error(" ".join(str(e).split())[:500])
    url = st.text_input("Database URL (host:port/service)", sec.get("url", ""), key=f"url_{slug}")
    user = st.text_input("Username", sec.get("user", ""), key=f"user_{slug}")
    password = st.text_input("Password", sec.get("password", ""), type="password", key=f"pw_{slug}")
    st.caption("Prefilled from .streamlit/secrets.toml if present.")
    st.button("Refresh all tiles", type="primary", use_container_width=True, on_click=request_refresh, args=(None,))
    st.button(f"Refresh ALL dashboards ({len(dashboards)})", use_container_width=True, on_click=request_refresh, args=("everything",),
              help="Runs every tile of every dashboard one after another on a single database session.")

cfg = load_config(slug)
history = load_json(DASH_DIR / slug / "history.json", {})

def logo_html():
    for ext, mime in (("png", "image/png"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg"), ("svg", "image/svg+xml")):
        p = ASSETS_DIR / f"logo.{ext}"
        if p.exists():
            return f'<img src="data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}" alt="{ORG_NAME}">'
    return ""


_logo = logo_html()
st.markdown(f'<div class="fb-bar">{_logo}{"" if _logo else f"<span class=fb-name>{ORG_NAME.upper()}</span>"}'
            f'<span class="fb-sub">Data profiling</span></div>', unsafe_allow_html=True)
st.title(cfg["title"])
tab_dash, tab_edit = st.tabs(["Dashboard", "Edit tiles"])

with tab_dash:
    wanted = st.session_state.pop("refresh_ids", None)
    if wanted:
        for dash in (dashboards if wanted == "everything" else [slug]):
            st.session_state.pop(f"fview_{dash}", None)  # a normal refresh changes the real numbers, so drop any filtered view
        typed = (url, user, password)  # what is in the sidebar right now, for the open dashboard

        def creds_for(dash):
            if dash == slug:
                return typed
            sec_d = get_credentials(dash)  # other dashboards: their secrets, else whatever is typed in the sidebar
            return (sec_d.get("url"), sec_d.get("user"), sec_d.get("password")) if all(
                sec_d.get(k) for k in ("url", "user", "password")) else typed

        if wanted == "everything":
            histories, jobs = {}, []
            for dash in dashboards:
                c = cfg if dash == slug else load_config(dash)
                histories[dash] = history if dash == slug else load_json(DASH_DIR / dash / "history.json", {})
                jobs.append({"slug": dash, "tiles": c["tiles"], "creds": creds_for(dash), "timeout_s": float(c["timeout_seconds"]),
                             "unset": bind_plan(c["filters"])})
        else:
            to_run = cfg["tiles"] if wanted == "all" else [t for t in cfg["tiles"] if t["id"] in wanted]
            histories = {slug: history}
            jobs = [{"slug": slug, "tiles": to_run, "creds": typed, "timeout_s": float(cfg["timeout_seconds"]),
                     "unset": bind_plan(cfg["filters"])}]

        total = sum(len(j["tiles"]) for j in jobs)
        if total:
            started = time.monotonic()
            bar = st.progress(0.0)
            status = st.empty()

            def on_progress(i, n, dash, name):
                bar.progress(i / n)
                status.info(f"{dash}: {name} (tile {i + 1} of {n}, one database session, do not click anything until finished)")

            rows, err = run_jobs(jobs, histories, on_progress)
            bar.empty()
            status.empty()
            if err:
                st.warning(err)
            else:
                if not TIMEOUT_STATE["ok"]:
                    st.warning("Your Oracle Client is older than 18c, so the query timeout is NOT enforced. Use Instant Client 19c.")
                failed = sum(r["Failed"] for r in rows)
                notes = [r for r in rows if r["Note"]]
                took = f" in {int(time.monotonic() - started) // 60}m {int(time.monotonic() - started) % 60}s"
                if len(rows) > 1:
                    st.dataframe(pd.DataFrame(rows), hide_index=True)
                for r in notes:
                    (st.error if len(rows) == 1 else st.warning)(f"{r['Dashboard']}: {r['Note']}")
                if failed and not notes:
                    st.warning(f"Refreshed {total - failed} of {total} tiles{took}. {failed} failed, see the red notes on those tiles.")
                elif not notes:
                    st.success(f"Refreshed {total} tile(s){took}.")
    fview = render_filters(slug, cfg, (url, user, password))
    render_dashboard(slug, cfg, history, fview)

    with st.expander("Lock baseline"):
        st.write("Sets every tile's starting point to its latest refreshed value. 'Since start' and the percentage are measured from here.")
        confirm = st.checkbox("I understand this overwrites all current baselines", key=f"lockok_{slug}")
        if st.button("Lock baseline for all tiles", disabled=not confirm, key=f"lock_{slug}"):
            n = lock_baselines(slug, history)
            st.success(f"Locked {n} tile baselines." if n else "Nothing to lock yet. Refresh first.")
            st.rerun()

with tab_edit:
    render_editor(slug, cfg, history)
