"""Dashboard wide filters ("scope").

A dashboard names one table (the filter table) and each filter names a column of it. When filters are applied,
every tile query that reads that table is rewritten so the table is replaced by a filtered inline view:

    FROM catm_check_book b   ->   FROM (SELECT * FROM catm_check_book WHERE branch = :sf_branch_0 AND ...) b

The tile SQL itself is never edited by hand. Filter values are always bind variables. Only the table name and
column names, which the app checks against a strict identifier pattern, are put into the SQL text.

Important meaning: the filter narrows the rows BEFORE the tile query runs. Counts, nulls and percentages are
correct for the chosen scope. A tile that compares rows against each other (for example overlapping cheque books
per account) only compares rows inside the scope.

No Streamlit imports here, so this module can be tested on its own.
"""
import re
from datetime import datetime, time as dtime

_ID = r"[A-Za-z][A-Za-z0-9_$#]{0,29}"
IDENT_RE = re.compile(_ID)
TABLE_RE = re.compile(rf"(?:{_ID}\.)?{_ID}")

# Text that is not SQL code: strings, quoted names and comments. Blanked out before searching for the table.
_LITERAL_RE = re.compile(
    r"""[nN]?[qQ]'(?:\[.*?\]|\(.*?\)|\{.*?\}|<.*?>|(\S).*?\1)'"""  # q'[...]' style
    r"""|'(?:[^']|'')*'"""                                            # 'string'
    r'''|"[^"]*"'''                                                   # "Quoted"
    r"""|--[^\n]*"""                                                  # -- comment
    r"""|/\*.*?\*/""",                                                # /* comment */
    re.DOTALL,
)

# Words that can follow a table name when it has no alias.
_NO_ALIAS = {
    "WHERE", "GROUP", "ORDER", "HAVING", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "ON",
    "USING", "UNION", "INTERSECT", "MINUS", "CONNECT", "START", "FETCH", "OFFSET", "FOR", "MODEL", "SAMPLE",
    "PARTITION", "WINDOW", "OUTER", "LATERAL", "PIVOT", "UNPIVOT", "FROM", "SELECT", "SET",
}


def valid_ident(s):
    return bool(s) and bool(IDENT_RE.fullmatch(s))


def valid_table(s):
    return bool(s) and bool(TABLE_RE.fullmatch(s))


def split_table(table):
    """'SCHEMA.TABLE' -> ('SCHEMA', 'TABLE'); 'TABLE' -> (None, 'TABLE')."""
    parts = table.split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])


def is_auto(f, table):
    """True when the filter is applied automatically (it names a column and the dashboard names a table)."""
    return bool(table) and valid_table(table) and valid_ident(f.get("column", ""))


def mask_sql(sql):
    """Same length as sql, with strings, quoted names and comments replaced by spaces."""
    return _LITERAL_RE.sub(lambda m: " " * len(m.group(0)), sql)


def wrap_sql(sql, table, where):
    """Replace every read of `table` in sql by a filtered inline view. Returns (new_sql, number_of_replacements).

    A table with no alias gets its own name as the alias, so references such as table.column keep working.
    """
    if not where or not valid_table(table):
        return sql, 0
    schema, base = split_table(table)
    mask = mask_sql(sql)
    pattern = re.compile(rf"(?<![\w$#.])((?:{_ID}\.)?){re.escape(base)}(?![\w$#])(?!\s*\.)", re.IGNORECASE)
    out, pos, n = [], 0, 0
    for m in pattern.finditer(mask):
        prefix = m.group(1)[:-1] if m.group(1) else ""
        if schema and prefix and prefix.upper() != schema.upper():
            continue  # same table name in a different schema: not ours
        word = re.match(r"\s*([A-Za-z_][\w$#]*)?", mask[m.end():]).group(1)
        needs_alias = word is None or word.upper() in _NO_ALIAS
        original = sql[m.start():m.end()]
        view = f"(SELECT * FROM {original} WHERE {where})" + (f" {base}" if needs_alias else "")
        out.append(sql[pos:m.start()])
        out.append(view)
        pos = m.end()
        n += 1
    out.append(sql[pos:])
    return "".join(out), n


def _like_escape(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_scope(filters, values, table):
    """WHERE text and bind values for the automatic filters that have a value. Empty filters add nothing.

    Returns (where, binds, date_bind_names). binds is {name: value}; names are lower case.
    """
    parts, binds, dates = [], {}, set()
    for f in filters:
        if not is_auto(f, table):
            continue
        col, name, v = f["column"], f["name"], values.get(f["id"])
        if f["type"] == "date_range":
            lo, hi = (v or {}).get("from"), (v or {}).get("to")
            if lo:
                b = f"sf_{name}_from"
                binds[b] = datetime.combine(lo, dtime.min)
                dates.add(b)
                parts.append(f"{col} >= :{b}")
            if hi:
                b = f"sf_{name}_to"
                binds[b] = datetime.combine(hi, dtime.min)
                dates.add(b)
                parts.append(f"{col} < :{b} + 1")  # the whole end day, including times and fractions of a second
        elif f["type"] == "choice":
            picked = list(v) if isinstance(v, (list, tuple)) else ([v] if v else [])
            picked = [p for p in picked if p is not None and str(p) != ""]
            if not picked:
                continue
            if len(picked) > 900:
                raise ValueError(f"{f['label']}: too many values selected (limit 900).")
            names = []
            for i, p in enumerate(picked):
                b = f"sf_{name}_{i}"
                binds[b] = str(p)
                names.append(":" + b)
            parts.append(f"{col} = {names[0]}" if len(names) == 1 else f"{col} IN ({', '.join(names)})")
        else:  # text: contains, ignoring case
            text = (v or "").strip()
            if not text:
                continue
            b = f"sf_{name}"
            binds[b] = _like_escape(text)
            parts.append(f"UPPER({col}) LIKE '%' || UPPER(:{b}) || '%' ESCAPE '\\'")
    return " AND ".join(parts), binds, dates


def sample_values(filters):
    """Made up values for every filter, to preview the rewritten SQL."""
    from datetime import date
    out = {}
    for f in filters:
        if f["type"] == "date_range":
            out[f["id"]] = {"from": date(2024, 1, 1), "to": date(2024, 12, 31)}
        elif f["type"] == "choice":
            out[f["id"]] = ["A", "B"] if f.get("multi") else "A"
        else:
            out[f["id"]] = "x"
    return out


def default_options_sql(f, table):
    """Query that lists the values of the filter's column, used when no options query is typed."""
    if is_auto(f, table):
        return f"SELECT DISTINCT {f['column']} FROM {table} WHERE {f['column']} IS NOT NULL ORDER BY 1"
    return ""
