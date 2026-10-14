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

import oracle_client

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


def refresh_tiles(slug, tiles, history, session, creds, timeout_s, on_progress):
    """Run tiles one at a time on the shared session, saving after each. Returns an abort message or None."""
    logged = {r.get("tile") for r in load_log(slug)}
    session.open(creds, timeout_s)
    for i, t in enumerate(tiles):
        on_progress(i, len(tiles), t["name"])
        h = history.setdefault(t["id"], {})
        started = time.monotonic()
        try:
            value = run_scalar(session.conn, t["sql"])
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
                abort = refresh_tiles(slug, tiles, histories[slug], session, j["creds"], j["timeout_s"], progress)
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

st.title(cfg["title"])
tab_dash, tab_edit = st.tabs(["Dashboard", "Edit tiles"])

with tab_dash:
    wanted = st.session_state.pop("refresh_ids", None)
    if wanted:
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
                jobs.append({"slug": dash, "tiles": c["tiles"], "creds": creds_for(dash), "timeout_s": float(c["timeout_seconds"])})
        else:
            to_run = cfg["tiles"] if wanted == "all" else [t for t in cfg["tiles"] if t["id"] in wanted]
            histories = {slug: history}
            jobs = [{"slug": slug, "tiles": to_run, "creds": typed, "timeout_s": float(cfg["timeout_seconds"])}]

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
