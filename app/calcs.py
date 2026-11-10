"""Traduction d'un sous-ensemble du langage de calcul Tableau vers SQL Hyper.

Principe : on ne devine pas. Tout ce qui sort du sous-ensemble connu lève
`CalcError` et la feuille affiche un avertissement au lieu de chiffres faux.

Sous-ensemble couvert
- références `[Champ]`, paramètres `[Parameters].[X]`
- arithmétique, comparaisons, AND/OR/NOT, concaténation `+` sur des chaînes
- `IF … THEN … ELSEIF … THEN … ELSE … END` et `CASE … WHEN … THEN … END`
- agrégats (SUM, AVG, COUNT, COUNTD, MEDIAN, STDEV…)
- fonctions scalaires courantes (voir FUNCS / CASTS / SPECIAL)
- LOD `{FIXED …}`, `{INCLUDE …}`, `{EXCLUDE …}` : extraits par `extract_lods`
  et calculés en amont par le lecteur (fenêtres SQL)
- calculs de table (RUNNING_*, WINDOW_*, RANK, INDEX, LOOKUP, TOTAL…) :
  extraits par `extract_table_calcs` et calculés après agrégation
"""
from __future__ import annotations

import hashlib
import re

__all__ = [
    "CalcError", "translate", "has_aggregate", "referenced_fields",
    "extract_lods", "extract_table_calcs", "window_sql", "has_table_calc",
]


class CalcError(Exception):
    pass


PARAM_RE = re.compile(r"\[Parameters\]\.\[([^\]]+)\]")

TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<param>\x00[^\x00]*\x00)
    | (?P<field>\[[^\]]*\])
    | (?P<str>"(?:[^"]|"")*"|'(?:[^']|'')*')
    | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op><=|>=|<>|!=|[=<>+\-*/^%&])
    | (?P<punct>[(),.])
    """,
    re.X,
)

AGG_FUNCS = {"SUM", "AVG", "COUNT", "COUNTD", "MEDIAN", "STDEV", "STDEVP",
             "VAR", "VARP", "ATTR", "COLLECT", "MIN", "MAX", "PERCENTILE"}

TABLE_FUNCS = {
    "RUNNING_SUM", "RUNNING_AVG", "RUNNING_COUNT", "RUNNING_MIN", "RUNNING_MAX",
    "WINDOW_SUM", "WINDOW_AVG", "WINDOW_COUNT", "WINDOW_MIN", "WINDOW_MAX",
    "RANK", "RANK_DENSE", "RANK_MODIFIED", "RANK_UNIQUE", "INDEX", "LOOKUP",
    "TOTAL", "FIRST", "LAST", "SIZE",
}

KEYWORDS = {"IF", "THEN", "ELSEIF", "ELSE", "END", "CASE", "WHEN", "AND", "OR",
            "NOT", "IN", "IS", "NULL", "TRUE", "FALSE", "BETWEEN", "LIKE"}

FUNCS = {
    "SUM": "SUM", "AVG": "AVG", "COUNT": "COUNT", "MEDIAN": "MEDIAN",
    "STDEV": "STDDEV_SAMP", "STDEVP": "STDDEV_POP", "VAR": "VAR_SAMP",
    "VARP": "VAR_POP", "MIN": "MIN", "MAX": "MAX", "ATTR": "MAX",
    "ABS": "ABS", "CEILING": "CEILING", "FLOOR": "FLOOR", "ROUND": "ROUND",
    "POWER": "POWER", "SQRT": "SQRT", "EXP": "EXP", "LN": "LN", "LOG": "LOG",
    "SIGN": "SIGN",
    "UPPER": "UPPER", "LOWER": "LOWER", "TRIM": "TRIM", "LTRIM": "LTRIM",
    "RTRIM": "RTRIM", "REPLACE": "REPLACE", "LEFT": "LEFT", "RIGHT": "RIGHT",
    "LEN": "LENGTH", "LENGTH": "LENGTH", "MID": "SUBSTRING",
    "YEAR": "YEAR", "MONTH": "MONTH", "DAY": "DAY",
    "PROPER": "INITCAP", "ASCII": "ASCII", "CHAR": "CHR",
    "REPEAT": "REPEAT",
}

CASTS = {"STR": "VARCHAR", "INT": "INT", "FLOAT": "DOUBLE PRECISION",
         "DATE": "DATE", "DATETIME": "TIMESTAMP", "BOOL": "BOOLEAN"}

STR_RESULT = {"STR", "LEFT", "RIGHT", "MID", "UPPER", "LOWER", "TRIM",
              "LTRIM", "RTRIM", "REPLACE", "SUBSTRING", "LENGTH", "VARCHAR",
              "INITCAP", "CHR", "REPEAT", "SPLIT_PART"}
BOOL_RESULT = {"ISNULL", "IFNULL", "CONTAINS", "STARTSWITH", "ENDSWITH", "COALESCE",
               "REGEXP_MATCHES"}


def tokenize(formula: str) -> list[tuple[str, str]]:
    s = PARAM_RE.sub(lambda m: "\x00" + m.group(1) + "\x00", formula)
    toks, pos = [], 0
    while pos < len(s):
        m = TOKEN_RE.match(s, pos)
        if not m:
            raise CalcError(f"caractère inattendu {s[pos]!r}")
        pos = m.end()
        if m.lastgroup != "ws":
            toks.append((m.lastgroup, m.group()))
    return toks


def _find_close(toks, i, end):
    depth = 0
    for j in range(i, end):
        if toks[j] == ("punct", "("):
            depth += 1
        elif toks[j] == ("punct", ")"):
            depth -= 1
            if depth == 0:
                return j
    raise CalcError("parenthèse non fermée")


def _split_args(toks, start, end):
    args, depth, cur = [], 0, start
    for j in range(start, end):
        k, v = toks[j]
        if k == "punct" and v == "(":
            depth += 1
        elif k == "punct" and v == ")":
            depth -= 1
        elif k == "punct" and v == "," and depth == 0:
            args.append((cur, j))
            cur = j + 1
    if cur < end or args:
        args.append((cur, end))
    return args


DATE_UNITS = {"year", "quarter", "month", "week", "day", "weekday",
              "dayofyear", "hour", "minute", "second",
              "iso-year", "iso-quarter", "iso-week", "iso-weekday"}


def _unit(arg: str) -> str:
    unit = arg.strip("'").strip('"').lower()
    if unit not in DATE_UNITS:
        raise CalcError(f"unité de date non supportée : {unit!r}")
    return unit


def _dateparse_format(lit: str) -> str:
    """Format .NET de Tableau -> motif accepté par to_timestamp de Hyper."""
    s = lit.strip("'").strip('"')
    s = s.replace("HH", "HH24").replace("hh", "HH12")
    s = s.replace("mm", "MI").replace("ss", "SS")
    return "'" + s + "'"


INTERVAL_UNITS = {"year", "month", "day", "hour", "minute", "second", "week"}

DATENAME_FORMAT = {
    "year": "YYYY", "quarter": "Q", "month": "Month", "day": "DD",
    "weekday": "Day", "week": "IW", "hour": "HH24", "minute": "MI",
    "second": "SS",
}


def _apply(name: str, args: list[str]) -> tuple[str, str]:
    n = len(args)
    if name == "COUNTD" and n == 1:
        return f"COUNT(DISTINCT {args[0]})", "num"
    if name == "ISNULL" and n == 1:
        return f"({args[0]} IS NULL)", "bool"
    if name == "IFNULL" and n == 2:
        return f"COALESCE({args[0]}, {args[1]})", "any"
    if name == "ZN" and n == 1:
        return f"COALESCE({args[0]}, 0)", "num"
    if name == "IIF" and n == 3:
        return f"(CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END)", "any"
    if name in CASTS and n == 1:
        cast = CASTS[name]
        return f"CAST({args[0]} AS {cast})", ("str" if cast == "VARCHAR" else "num")
    if name == "DATEPART" and n == 2:
        unit = _unit(args[0])
        if unit == "iso-week":
            unit = "week"
        return f"EXTRACT({unit} FROM {args[1]})", "num"
    if name == "DATETRUNC" and n == 2:
        unit = _unit(args[0]).replace("iso-", "")
        return f"date_trunc('{unit}', {args[1]})", "any"
    if name == "DATEPARSE" and n == 2:
        fmt = _dateparse_format(args[0])
        src = args[1]
        # Tableau accepte 24:00 comme minuit du lendemain ; Hyper le refuse.
        return (f"(CASE WHEN {src} LIKE '% 24:%' "
                f"THEN to_timestamp(REPLACE({src}, ' 24:', ' 00:'), {fmt}) "
                f"+ INTERVAL '1' DAY ELSE to_timestamp({src}, {fmt}) END)"), "any"
    if name == "DATEDIFF" and n == 3:
        return f"datediff('{_unit(args[0])}', {args[1]}, {args[2]})", "num"
    if name == "DATEADD" and n == 3:
        unit = _unit(args[0])
        if unit not in INTERVAL_UNITS:
            raise CalcError(f"DATEADD : unité non supportée {unit!r}")
        return f"({args[2]} + ({args[1]}) * INTERVAL '1' {unit.upper()})", "any"
    if name == "DATENAME" and n == 2:
        unit = _unit(args[0])
        fmt = DATENAME_FORMAT.get(unit)
        if not fmt:
            raise CalcError(f"DATENAME : unité non supportée {unit!r}")
        if fmt == "Q":
            return f"('T' || CAST(EXTRACT(quarter FROM {args[1]}) AS VARCHAR))", "str"
        return f"to_char({args[1]}, '{fmt}')", "str"
    if name == "MAKEDATE" and n == 3:
        return f"make_date({args[0]}, {args[1]}, {args[2]})", "any"
    if name == "MAKEDATETIME" and n == 2:
        return f"({args[0]} + {args[1]})", "any"
    if name == "NOW" and n == 0:
        return "now()", "any"
    if name == "TODAY" and n == 0:
        return "current_date", "any"
    if name == "PI" and n == 0:
        return "pi()", "num"
    if name == "RANDOM" and n == 0:
        return "random()", "num"
    if name == "CONTAINS" and n == 2:
        return f"POSITION({args[1]} IN {args[0]}) > 0", "bool"
    if name == "STARTSWITH" and n == 2:
        return f"LEFT({args[0]}, LENGTH({args[1]})) = {args[1]}", "bool"
    if name == "ENDSWITH" and n == 2:
        return f"RIGHT({args[0]}, LENGTH({args[1]})) = {args[1]}", "bool"
    if name == "FIND" and n in (2, 3):
        if n == 2:
            return f"POSITION({args[1]} IN {args[0]})", "num"
        return (f"CASE WHEN POSITION({args[1]} IN SUBSTRING({args[0]} FROM {args[2]})) = 0 "
                f"THEN 0 ELSE POSITION({args[1]} IN SUBSTRING({args[0]} FROM {args[2]})) "
                f"+ {args[2]} - 1 END"), "num"
    if name == "SPLIT" and n == 3:
        return f"SPLIT_PART({args[0]}, {args[1]}, {args[2]})", "str"
    if name == "REGEXP_MATCH" and n == 2:
        return f"REGEXP_MATCHES({args[0]}, {args[1]})", "bool"
    if name == "REGEXP_REPLACE" and n == 3:
        return f"REGEXP_REPLACE({args[0]}, {args[1]}, {args[2]})", "str"
    if name == "REGEXP_EXTRACT" and n == 3:
        return f"REGEXP_EXTRACT({args[0]}, {args[1]}, {args[2]})", "str"
    if name == "REGEXP_EXTRACT_NTH" and n == 3:
        return f"REGEXP_EXTRACT({args[0]}, {args[1]}, {args[2]})", "str"
    if name == "SPACE" and n == 1:
        return f"REPEAT(' ', CAST({args[0]} AS INT))", "str"
    if name == "MIN" and n == 2:
        return f"LEAST({args[0]}, {args[1]})", "any"
    if name == "MAX" and n == 2:
        return f"GREATEST({args[0]}, {args[1]})", "any"
    if name == "PERCENTILE" and n == 2:
        return (f"PERCENTILE_CONT({args[1]}) WITHIN GROUP (ORDER BY {args[0]})"), "num"
    if name == "CORR" and n == 2:
        return f"CORR({args[0]}, {args[1]})", "num"
    if name in FUNCS:
        return f"{FUNCS[name]}({', '.join(args)})", ("str" if name in STR_RESULT else "num")
    raise CalcError(f"fonction non supportée : {name}")


def _render(toks, i, end, resolve, params) -> list[tuple[str, str, str]]:
    """Retourne des items (nature, texte, type) ; nature ∈ operand/op/kw/punct."""
    items: list[tuple[str, str, str]] = []
    while i < end:
        kind, val = toks[i]
        if kind == "param":
            name = val.strip("\x00")
            if name not in params:
                raise CalcError(f"paramètre inconnu : {name}")
            lit = params[name]
            typ = ("str" if lit.startswith("'")
                   else "bool" if lit in ("TRUE", "FALSE")
                   else "any" if lit.startswith("CAST(") else "num")
            items.append(("operand", lit, typ))
            i += 1
        elif kind == "field":
            sql, typ = resolve(val[1:-1])
            items.append(("operand", sql, typ))
            i += 1
        elif kind == "str":
            quote_char = val[0]
            body = val[1:-1].replace(quote_char * 2, quote_char)
            items.append(("operand", "'" + body.replace("'", "''") + "'", "str"))
            i += 1
        elif kind == "num":
            items.append(("operand", val, "num"))
            i += 1
        elif kind == "ident":
            up = val.upper()
            if up in KEYWORDS:
                text = {"IF": "CASE WHEN", "ELSEIF": "WHEN"}.get(up, val)
                items.append(("kw", text, "any"))
                i += 1
            elif i + 1 < end and toks[i + 1] == ("punct", "("):
                close = _find_close(toks, i + 1, end)
                args = [_join(_render(toks, a, b, resolve, params))
                        for a, b in _split_args(toks, i + 2, close)]
                sql, typ = _apply(up, args)
                items.append(("operand", sql, typ))
                i = close + 1
            else:
                items.append(("operand", val, "any"))
                i += 1
        elif kind == "op":
            items.append(("op", val, "any"))
            i += 1
        else:
            items.append(("punct", val, "any"))
            i += 1
    return _collapse(items)


def _collapse(items: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """`^` et `%` binaires -> POWER(a,b) / MOD(a,b)."""
    out = list(items)
    for sym, fn in (("^", "POWER"), ("%", "MOD")):
        i = 0
        while i < len(out):
            kind, text, _ = out[i]
            if kind == "op" and text == sym:
                left = _neighbor(out, i, -1)
                right = _neighbor(out, i, +1)
                if left is None or right is None:
                    raise CalcError(f"opérateur {sym} mal formé")
                out[left] = ("operand", f"{fn}({out[left][1]}, {out[right][1]})", "num")
                del out[i:i + 2]
                i = max(0, left)
                continue
            i += 1
    return out


def _neighbor(items, idx, step):
    j = idx + step
    while 0 <= j < len(items):
        if items[j][0] == "operand":
            return j
        j += step
    return None


def _join(items: list[tuple[str, str, str]]) -> str:
    out: list[str] = []
    for idx, (kind, text, _typ) in enumerate(items):
        if kind == "op" and text == "+":
            left = _neighbor(items, idx, -1)
            right = _neighbor(items, idx, +1)
            lt = items[left][2] if left is not None else "any"
            rt = items[right][2] if right is not None else "any"
            out.append("||" if "str" in (lt, rt) else "+")
        elif kind == "op" and text == "/":
            # Tableau divise toujours en flottant ; SQL non
            left = _neighbor(items, idx, -1)
            if left is not None and items[left][2] == "num" and out[left] is not None:
                out[left] = f"CAST({out[left]} AS DOUBLE PRECISION)"
            out.append("/")
        else:
            out.append(text)
    return " ".join(out)


def translate(formula: str, resolve, params: dict[str, str]) -> str:
    """formula : texte Tableau ; resolve(nom) -> (SQL, type) ; params : nom -> littéral SQL."""
    toks = tokenize(formula)
    if not toks:
        raise CalcError("formule vide")
    return _join(_render(toks, 0, len(toks), resolve, params))


def has_aggregate(formula: str) -> bool:
    try:
        toks = tokenize(formula)
    except CalcError:
        return False
    for i, (k, v) in enumerate(toks):
        if k == "ident" and v.upper() in AGG_FUNCS and i + 1 < len(toks) and toks[i + 1] == ("punct", "("):
            return True
    return False


def referenced_fields(formula: str) -> list[str]:
    return [m for m in re.findall(r"\[([^\]]+)\]", formula) if m != "Parameters"]


# --------------------------------------------------------------------------- #
# LOD
# --------------------------------------------------------------------------- #
def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def extract_lods(formula: str) -> tuple[str, list[dict]]:
    """Remplace `{FIXED … : …}` par `[__lod_xxx]` et renvoie les définitions."""
    specs: list[dict] = []

    def repl(m):
        body = m.group(1)
        mm = re.match(r"\s*(FIXED|INCLUDE|EXCLUDE)\b(.*?):(.*)$", body, re.S | re.I)
        if not mm:
            raise CalcError(f"LOD non reconnu : {{{body}}}")
        kind = mm.group(1).upper()
        dims = re.findall(r"\[([^\]]+)\]", mm.group(2))
        expr = mm.group(3).strip()
        alias = "__lod_" + _hash(f"{kind}|{'|'.join(dims)}|{expr}")
        specs.append({"alias": alias, "kind": kind, "dims": dims, "expr": expr})
        return f"[{alias}]"

    out = re.sub(r"\{([^{}]*)\}", repl, formula)
    return out, specs


# --------------------------------------------------------------------------- #
# calculs de table
# --------------------------------------------------------------------------- #
def _match_paren(s: str, open_i: int) -> int:
    depth, quote = 0, None
    for i in range(open_i, len(s)):
        ch = s[i]
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    raise CalcError("parenthèse non fermée")


def extract_table_calcs(formula: str) -> tuple[str, list[dict]]:
    """Remplace `RUNNING_SUM(SUM([X]))` par `[__tc_xxx]`."""
    specs: list[dict] = []
    out, pos = "", 0
    while pos < len(formula):
        m = re.search(r"\b(" + "|".join(sorted(TABLE_FUNCS)) + r")\s*\(", formula[pos:])
        if not m:
            out += formula[pos:]
            break
        start = pos + m.start()
        open_i = pos + m.end() - 1
        close = _match_paren(formula, open_i)
        raw = formula[start:close + 1]
        name = m.group(1)
        args = formula[open_i + 1:close]
        alias = "__tc_" + _hash(raw)
        specs.append({"alias": alias, "func": name, "args": args, "raw": raw})
        out += formula[pos:start] + f"[{alias}]"
        pos = close + 1
    return out, specs


def has_table_calc(formula: str) -> bool:
    return bool(re.search(r"\b(" + "|".join(sorted(TABLE_FUNCS)) + r")\s*\(", formula))


def _rows_between(start: int, end: int) -> str:
    def bound(n, before):
        if n == 0:
            return "CURRENT ROW"
        return f"{abs(n)} {'PRECEDING' if before else 'FOLLOWING'}"
    a = bound(start, start >= 0)
    b = bound(end, end < 0)
    return f"ROWS BETWEEN {a} AND {b}"


def window_sql(func: str, args: list[str], order: str, desc: bool = False) -> str:
    f = func.upper()
    order = order or "1"
    direction = "DESC" if desc else "ASC"

    def int_arg(value: str) -> int:
        # translate() espace les signes : « -1 » devient « - 1 »
        return int(re.sub(r"\s+", "", value))
    if f == "TOTAL":
        return f"SUM({args[0]}) OVER ()"
    if f == "RUNNING_SUM":
        return f"SUM({args[0]}) OVER (ORDER BY {order} ROWS UNBOUNDED PRECEDING)"
    if f == "RUNNING_AVG":
        return f"AVG({args[0]}) OVER (ORDER BY {order} ROWS UNBOUNDED PRECEDING)"
    if f == "RUNNING_COUNT":
        return f"COUNT({args[0]}) OVER (ORDER BY {order} ROWS UNBOUNDED PRECEDING)"
    if f == "RUNNING_MIN":
        return f"MIN({args[0]}) OVER (ORDER BY {order} ROWS UNBOUNDED PRECEDING)"
    if f == "RUNNING_MAX":
        return f"MAX({args[0]}) OVER (ORDER BY {order} ROWS UNBOUNDED PRECEDING)"
    if f in ("WINDOW_SUM", "WINDOW_AVG", "WINDOW_COUNT", "WINDOW_MIN", "WINDOW_MAX"):
        fn = {"WINDOW_SUM": "SUM", "WINDOW_AVG": "AVG", "WINDOW_COUNT": "COUNT",
              "WINDOW_MIN": "MIN", "WINDOW_MAX": "MAX"}[f]
        if len(args) >= 3:
            try:
                start, end = int_arg(args[1]), int_arg(args[2])
            except ValueError as exc:
                raise CalcError("WINDOW_* : bornes non entières") from exc
            frame = _rows_between(start, end)
        else:
            frame = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
        return f"{fn}({args[0]}) OVER (ORDER BY {order} {frame})"
    if f in ("RANK", "RANK_DENSE", "RANK_MODIFIED", "RANK_UNIQUE"):
        fn = {"RANK": "RANK", "RANK_DENSE": "DENSE_RANK",
              "RANK_MODIFIED": "RANK", "RANK_UNIQUE": "ROW_NUMBER"}[f]
        return f"{fn}() OVER (ORDER BY {args[0] if args else order} {direction})"
    if f == "INDEX":
        return f"ROW_NUMBER() OVER (ORDER BY {order})"
    if f == "LOOKUP":
        if len(args) < 2:
            raise CalcError("LOOKUP attend 2 arguments")
        try:
            offset = int_arg(args[1])
        except ValueError as exc:
            raise CalcError("LOOKUP : décalage non entier") from exc
        if offset > 0:
            return f"LAG({args[0]}, {offset}) OVER (ORDER BY {order})"
        if offset < 0:
            return f"LEAD({args[0]}, {-offset}) OVER (ORDER BY {order})"
        return f"{args[0]}"
    if f == "FIRST":
        return f"(ROW_NUMBER() OVER (ORDER BY {order}) - 1)"
    if f == "LAST":
        return f"(COUNT(*) OVER () - ROW_NUMBER() OVER (ORDER BY {order}))"
    if f == "SIZE":
        return "COUNT(*) OVER ()"
    raise CalcError(f"calcul de table non supporté : {func}")
