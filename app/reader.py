"""Cœur du lecteur Tableau : déballage, parsing du .twb, lecture Hyper, rendu.

Sécurité : .twbx / .twb / .hyper viennent de l'utilisateur.
- zip : chemins validés, taille décompressée plafonnée
- XML : entités externes et réseau désactivés (XXE / billion laughs)
"""
from __future__ import annotations

import atexit
import datetime as _dt
import decimal as _decimal
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from statistics import NormalDist
import zipfile
from dataclasses import dataclass, field, replace

from lxml import etree
from tableauhyperapi import Connection, HyperProcess, Telemetry

from calcs import (CalcError, extract_lods, extract_table_calcs, has_aggregate,
                   has_table_calc, referenced_fields, translate, window_sql)

MAX_UNCOMPRESSED = 2 * 1024 ** 3
MAX_ENTRIES = 20_000
MAX_ROWS = 50_000
CROSSTAB_MAX = 20_000
MAP_MAX = 20_000
QUERY_TIMEOUT = 30.0

_hyper = None
_hyper_lock = threading.Lock()


def hyper_process() -> HyperProcess:
    global _hyper
    with _hyper_lock:
        if _hyper is None:
            log_dir = os.path.join(tempfile.gettempdir(), "tableau-reader-logs")
            os.makedirs(log_dir, exist_ok=True)
            _hyper = HyperProcess(
                telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
                parameters={"log_dir": log_dir})
            atexit.register(_close_hyper)
        return _hyper


def _is_conn_error(exc: BaseException) -> bool:
    text = str(exc)
    return ("Could not connect to the Hyper server" in text
            or "Hyper server closed the connection" in text
            or "connection unexpectedly" in text)


def _close_hyper() -> None:
    global _hyper
    with _hyper_lock:
        if _hyper is not None:
            try:
                _hyper.close()
            except Exception:
                pass
            _hyper = None


class TWBXError(Exception):
    pass


# --------------------------------------------------------------------------- #
# ouverture
# --------------------------------------------------------------------------- #
def safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
    total = 0
    for i, info in enumerate(zf.infolist()):
        if i >= MAX_ENTRIES:
            raise TWBXError("archive : trop d'entrées")
        name = info.filename.replace("\\", "/")
        if os.path.isabs(name) or ".." in name.split("/"):
            raise TWBXError(f"archive : chemin interdit {name!r}")
        total += info.file_size
        if total > MAX_UNCOMPRESSED:
            raise TWBXError("archive : taille décompressée trop grande")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise TWBXError("archive : lien symbolique interdit")
    parent = os.path.dirname(os.path.abspath(dest)) or "."
    try:
        free = shutil.disk_usage(parent).free
    except OSError:
        free = None
    if free is not None and total + 64 * 1024 ** 2 > free:
        raise TWBXError("archive : espace disque insuffisant")
    zf.extractall(dest)


def scan_dependencies(twb_path: str, workdir: str | None = None) -> dict:
    """Liste les fichiers externes dont un .twb a besoin, et lesquels manquent."""
    workdir = workdir or os.path.dirname(os.path.abspath(twb_path))
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)
    root = etree.parse(twb_path, parser).getroot()

    available = {f.lower() for f in (os.path.basename(p) for p in
                 (os.path.join(dp, f) for dp, _, fs in os.walk(workdir) for f in fs))}

    sheets_by_ds: dict[str, list[str]] = {}
    for ws in root.findall("worksheets/worksheet"):
        vd = ws.find("table/view/datasources/datasource")
        if vd is not None:
            sheets_by_ds.setdefault(vd.get("name"), []).append(ws.get("name"))

    sources = []
    for ds in root.findall("datasources/datasource"):
        name = ds.get("name")
        if not name or name == "Parameters":
            continue
        caption = ds.get("caption") or name
        kind, required = None, None
        classes = {c.get("class") for c in ds.iter("connection")}

        found: dict[str, str] = {}
        for conn in ds.iter("connection"):
            cls = conn.get("class") or ""
            db = conn.get("dbname") or conn.get("filename")
            if cls in ("hyper", "textscan", "excel-direct") and db and cls not in found:
                found[cls] = os.path.basename(db.replace("\\", "/"))
        for cls, label in (("hyper", "hyper"), ("textscan", "texte"), ("excel-direct", "excel")):
            if cls in found:
                kind, required = label, found[cls]
                break

        if kind is None:
            if "sqlproxy" in classes:
                kind, required = "tdsx", f"{caption}.tdsx"
            elif classes & {"sqlserver", "postgres", "mysql", "oracle", "snowflake",
                            "bigquery", "redshift", "teradata", "exasol", "vertica"}:
                kind = "live"
            elif "federated" in classes:
                kind = "live"
            else:
                kind = "inconnu"

        present = bool(required) and required.lower() in available
        sources.append({
            "name": name,
            "caption": caption,
            "kind": kind,
            "required": required,
            "present": present,
            "sheets": sheets_by_ds.get(name, []),
            "status": ("ok" if present else
                       "live" if kind in ("live", "inconnu") else "manquant"),
        })

    missing = [s["required"] for s in sources if s["status"] == "manquant"]
    return {
        "twb": os.path.basename(twb_path),
        "sources": sources,
        "missing": missing,
        "sheets": len(root.findall("worksheets/worksheet")),
        "ok": not missing,
    }


def open_workbook(path: str, workdir: str) -> "Workbook":
    """Accepte un .twbx (zip) ou un .twb posé à côté de ses .hyper."""
    os.makedirs(workdir, exist_ok=True)

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            safe_extract(zf, workdir)
        twbs = [os.path.join(dp, f) for dp, _, fs in os.walk(workdir)
                for f in fs if f.lower().endswith(".twb")]
        if not twbs:
            raise TWBXError("aucun .twb dans l'archive")
        twbs.sort(key=lambda p: (os.path.dirname(p).count(os.sep), len(p)))
        twb = twbs[0]
    elif path.lower().endswith(".twb"):
        twb = os.path.join(workdir, os.path.basename(path))
        if os.path.abspath(twb) != os.path.abspath(path):
            with open(path, "rb") as src, open(twb, "wb") as dst:
                dst.write(src.read())
    else:
        raise TWBXError("fichier non reconnu : attendu .twbx ou .twb")

    for name in os.listdir(workdir):
        if name.lower().endswith(".tdsx"):
            dest = os.path.join(workdir, "_tdsx", os.path.splitext(name)[0])
            if not os.path.isdir(dest):
                with zipfile.ZipFile(os.path.join(workdir, name)) as zf:
                    safe_extract(zf, dest)

    return Workbook(twb, workdir)


# --------------------------------------------------------------------------- #
# modèle
# --------------------------------------------------------------------------- #
@dataclass
class Field:
    name: str
    caption: str
    datatype: str
    role: str
    formula: str | None = None
    number_format: str | None = None
    group: dict | None = None
    bin: dict | None = None
    aliases: dict = field(default_factory=dict)


@dataclass
class ShelfItem:
    ds: str
    agg: str
    field: str
    kind: str


@dataclass
class Source:
    name: str
    caption: str
    fields: dict[str, Field] = field(default_factory=dict)
    extract: str | None = None
    joins: list[dict] = field(default_factory=list)
    sets: dict[str, dict] = field(default_factory=dict)
    drill_paths: list[list[str]] = field(default_factory=list)
    hierarchy_names: list[str] = field(default_factory=list)
    # nom logique Tableau -> nom physique dans l'extrait Hyper
    remotes: dict[str, str] = field(default_factory=dict)


_SUFFIX = re.compile(r"\s*\([^()]*\)$")


def _remote_name(src: Source, name: str) -> str:
    """Nom physique de l'extrait pour un nom logique du .twb."""
    hit = src.remotes.get(name)
    if hit:
        return hit
    key = _SUFFIX.sub("", name)
    hit = src.remotes.get(key)
    if hit:
        return hit
    # champs copiés d'une source « resume_activites » vers un extrait qui
    # expose les mêmes colonnes sous leur nom court (intitule/valeur/unite)
    if key.endswith("_indicateur"):
        hit = src.remotes.get(key[:-len("_indicateur")])
        if hit:
            return hit
    return name


def _field_label(src: Source | None, name: str) -> str:
    """Libellé lisible : caption Tableau, sinon nom sans suffixe d'extrait."""
    f = src.fields.get(name) if src else None
    if f and f.caption and f.caption != f.name:
        return f.caption
    base = _SUFFIX.sub("", name)
    if base != name:
        bf = src.fields.get(base) if src else None
        if bf and bf.caption and bf.caption != bf.name:
            return bf.caption
        name = base
    return name.replace("_", " ").strip()


@dataclass
class Sheet:
    name: str
    rows: list[ShelfItem] = field(default_factory=list)
    cols: list[ShelfItem] = field(default_factory=list)
    mark: str = "Automatic"
    encodings: dict[str, list[ShelfItem]] = field(default_factory=dict)
    panes: list[dict] = field(default_factory=list)
    filters: list[dict] = field(default_factory=list)
    sorts: list[dict] = field(default_factory=list)
    ref_lines: list[dict] = field(default_factory=list)
    ref_bands: list[dict] = field(default_factory=list)
    labels: bool = False
    datasource: str | None = None
    title: str | None = None
    title_template: str | None = None
    tab_color: str | None = None
    tooltip: str | None = None
    totals: dict = field(default_factory=dict)
    manual: dict = field(default_factory=dict)
    dual: bool = False
    trend: bool = False
    size_field: ShelfItem | None = None
    style: dict = field(default_factory=dict)
    drill: list[list[str]] = field(default_factory=list)
    hidden: bool = False
    measure_filter: list[str] = field(default_factory=list)


@dataclass
class Dashboard:
    name: str
    zones: list[dict] = field(default_factory=list)
    width: int = 1000
    height: int = 800
    title: str | None = None
    background: str | None = None
    story: list[dict] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    device_layouts: dict = field(default_factory=dict)
    viewpoints: dict = field(default_factory=dict)
    sizing: str = "automatic"
    tab_color: str | None = None


@dataclass
class Workbook:
    twb_path: str
    workdir: str
    sheets: list[Sheet] = field(default_factory=list)
    dashboards: list[Dashboard] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    sources: dict[str, Source] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    param_meta: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _tables: dict[str, list[tuple[str, list[str]]]] = field(default_factory=dict, repr=False)
    _distinct: dict[tuple, list] = field(default_factory=dict, repr=False)
    _lock: object = field(default_factory=threading.RLock, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._parse()

    # -- parsing .twb -------------------------------------------------------- #
    def _parse(self) -> None:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)
        root = etree.parse(self.twb_path, parser).getroot()

        tab_colors: dict[str, str] = {}
        for win in root.findall("windows/window"):
            name = win.get("name")
            color = win.get("tab-color")
            if name and color:
                tab_colors[name] = color

        hypers = {os.path.basename(p).lower(): p
                  for p in (os.path.join(dp, f) for dp, _, fs in os.walk(self.workdir) for f in fs)
                  if p.lower().endswith(".hyper")}

        pds = root.find("datasources/datasource[@name='Parameters']")
        if pds is not None:
            for col in pds.findall("column"):
                pname = (col.get("name") or "").strip("[]")
                raw = col.get("value")
                self.params[pname] = _param_literal(raw, col.get("datatype"))
                self.param_meta.append({
                    "name": pname,
                    "caption": col.get("caption") or pname,
                    "datatype": col.get("datatype") or "string",
                    "value": raw,
                    "members": [m.get("value") for m in col.findall("members/member")],
                    "member_labels": {_unquote(a.get("key")): _unquote(a.get("value"))
                                      for a in col.findall("aliases/alias")
                                      if a.get("key") is not None},
                    "domain": col.get("param-domain-type"),
                })

        for ds in root.findall("datasources/datasource"):
            name = ds.get("name")
            if not name or name == "Parameters":
                continue
            src = Source(name=name, caption=ds.get("caption") or name)

            for col in ds.findall("column"):
                f = _make_field(col)
                if f:
                    src.fields[f.name] = f

            for rec in ds.findall(".//metadata-record"):
                if rec.get("class") != "column":
                    continue
                local = (rec.findtext("local-name") or "").strip().strip("[]")
                remote = (rec.findtext("remote-name") or "").strip().strip("[]")
                if local and remote:
                    src.remotes[local] = remote

            for g in ds.findall("group"):
                gname = (g.get("name") or "").strip("[]")
                parsed = _parse_set(g)
                if gname and parsed:
                    src.sets[gname] = parsed

            for dp in ds.findall("drill-paths/drill-path"):
                levels = [(x.text or "").strip().strip("[]") for x in dp.findall("field")]
                levels = [x for x in levels if x]
                if levels:
                    src.drill_paths.append(levels)
                    src.hierarchy_names.append(dp.get("name") or " › ".join(levels))

            for conn in ds.iter("connection"):
                cls = conn.get("class") or ""
                db = conn.get("dbname") or conn.get("filename")
                if cls in ("hyper", "textscan") and db:
                    src.extract = self._resolve(db, hypers)
                    if src.extract:
                        break

            for rel in ds.findall(".//relationships/relationship"):
                exprs = rel.findall("expression")
                ends = rel.findall("first-end-point") + rel.findall("second-end-point")
                if not exprs or len(ends) != 2:
                    continue
                conds = _join_conditions(exprs[0])
                if conds:
                    src.joins.append({"left": ends[0].get("object-id"),
                                      "right": ends[1].get("object-id"),
                                      "on": conds})

            self.sources[name] = src

        tds = self._tds_files()
        for src in self.sources.values():
            if not src.extract:
                path = tds.get(src.caption.lower())
                if path:
                    self._apply_tds(src, path)

        if not any(s.extract for s in self.sources.values()):
            raise TWBXError("aucun extrait .hyper exploitable — connexion live non supportée")

        sheet_names = set()
        for ws in root.findall("worksheets/worksheet"):
            sheet = self._parse_sheet(ws)
            if sheet is None:
                continue
            sheet.tab_color = tab_colors.get(sheet.name)
            sheet_names.add(sheet.name)
            self.sheets.append(sheet)

        for db in root.findall("dashboards/dashboard"):
            dash = self._parse_dashboard(db, sheet_names)
            if dash.zones or dash.story:
                dash.tab_color = tab_colors.get(dash.name)
                self.dashboards.append(dash)

        for act in root.findall("actions/action"):
            parsed = _parse_action(act)
            if parsed:
                self.actions.append(parsed)

        # mode des cartes de filtre dans la vue feuille (liste / dropdown / slider…)
        for win in root.findall("windows/window"):
            if win.get("class") != "worksheet":
                continue
            sheet = next((x for x in self.sheets if x.name == win.get("name")), None)
            if sheet is None:
                continue
            modes = {}
            for card in win.findall(".//card[@type='filter']"):
                field = _zone_field(card.get("param"))
                if field:
                    modes[field] = {"mode": card.get("mode"), "values": card.get("values")}
            for f in sheet.filters:
                m = modes.get(f.get("field"))
                if m:
                    f["mode"] = m["mode"]
                    f["values_mode"] = m["values"]

        # zoom des feuilles dans les dashboards : <zoom type='entire-view'/>
        for win in root.findall("windows/window"):
            if win.get("class") != "dashboard":
                continue
            dash = next((d for d in self.dashboards if d.name == win.get("name")), None)
            if dash is None:
                continue
            for vp in win.findall("viewpoints/viewpoint"):
                name = vp.get("name")
                if not name:
                    continue
                zoom = vp.find("zoom")
                dash.viewpoints[name] = zoom.get("type") if zoom is not None else "standard"

    def _text_ref(self, ds: str, inner: str) -> str:
        """Remplace une référence `[source].[champ]` par son libellé affiché."""
        if ds in ("Parameters", "Paramètres"):
            name = _zone_param(inner) or inner.strip("[]")
            meta = next((p for p in self.param_meta if p["name"] == name), None)
            value = self.params.get(name, meta["caption"] if meta else name)
            return _unquote(str(value))
        parsed = instance_parts(inner)
        field = parsed[1] if parsed else inner.strip("[]")
        src = self.sources.get(ds)
        f = src.fields.get(field) if src else None
        if f is None:
            for other in self.sources.values():
                f = other.fields.get(field)
                if f:
                    src = other
                    break
        return _field_label(src, field)

    def _title_for(self, s: Sheet) -> str | None:
        """Titre dynamique : valeurs de paramètres et membres de filtres sélectionnés."""
        text = s.title_template or s.title
        if not text:
            return text
        selected: dict[str, str] = {}
        for f in s.filters:
            field = f.get("field")
            if not field:
                continue
            base = _base_field(field).lower()
            members = f.get("members") or []
            if f.get("none"):
                selected[base] = "Aucun"
            elif len(members) == 1 and not f.get("exclude"):
                selected[base] = str(members[0])
            elif len(members) > 1 and not f.get("exclude"):
                more = "…" if len(members) > 3 else ""
                selected[base] = ", ".join(str(m) for m in members[:3]) + more
            rng = f.get("range") or {}
            if rng.get("min") is not None and rng.get("min") == rng.get("max"):
                selected[base] = str(rng["min"])
        own = self.sources.get(s.datasource or "")

        def repl(ds: str, inner: str) -> str:
            if ds in ("Parameters", "Paramètres"):
                name = _zone_param(inner) or inner.strip("[]")
                meta = next((p for p in self.param_meta if p["name"] == name), None)
                value = self.params.get(name, meta["caption"] if meta else name)
                return _unquote(str(value))
            parsed = instance_parts(inner)
            field = parsed[1] if parsed else inner.strip("[]")
            keys = (field.lower(), _base_field(field).lower())
            value = next((selected[k] for k in keys if k in selected), "Tous")
            src = self.sources.get(ds) or own
            f = src.fields.get(field) if src else None
            if f and f.aliases and value in f.aliases:
                value = f.aliases[value]
            return _unquote(value)

        text = _REF.sub(lambda m: repl(m.group(1), m.group(2)),
                        _TITLE_REF.sub(lambda m: repl(m.group(1), m.group(2)), text))
        return re.sub(r"<\s*Sheet Name\s*>", s.name, text, flags=re.I)

    def _parse_sheet(self, ws) -> Sheet | None:
        sheet = Sheet(name=ws.get("name") or "Feuille")
        table = ws.find("table")
        if table is None:
            return None

        title = ws.find("layout-options/title/formatted-text")
        if title is not None:
            sheet.title_template = _runs_text(title)
            sheet.title = _runs_text(title, self._text_ref)

        sheet.rows = parse_shelf(table.findtext("rows"))
        sheet.cols = parse_shelf(table.findtext("cols"))
        vd = table.find("view/datasources/datasource")
        if vd is not None:
            sheet.datasource = vd.get("name")

        for deps in table.findall("view/datasource-dependencies"):
            dep_src = self.sources.get(deps.get("datasource"))
            if dep_src is None:
                continue
            for col in deps.findall("column"):
                f = _make_field(col)
                if f and f.name not in dep_src.fields:
                    dep_src.fields[f.name] = f

        for pane in table.findall("panes/pane"):
            mark_el = pane.find("mark")
            mark = (mark_el.get("class") if mark_el is not None else None) or "Automatic"
            enc = {}
            enc_el = pane.find("encodings")
            if enc_el is not None:
                for e in enc_el:
                    items = parse_shelf(e.get("column"))
                    if items:
                        enc.setdefault(e.tag, []).extend(items)
                        sheet.encodings.setdefault(e.tag, []).extend(items)
            sheet.panes.append({"mark": mark, "encodings": enc})
            if len(sheet.panes) == 1:
                sheet.mark = mark

        if len(sheet.panes) >= 2:
            sheet.dual = True

        for f in table.findall("view/filter"):
            pf = parse_filter(f)
            if pf.get("measure_names"):
                sheet.measure_filter.extend(pf["measure_names"])
                continue
            if pf["field"] or pf.get("class") in ("quantitative", "relative-date", "top", "wildcard"):
                sheet.filters.append(pf)

        for srt in table.findall(".//sort"):
            col = parse_shelf(srt.get("column"))
            using = parse_shelf(srt.get("using"))
            if not col:
                continue
            entry = {"column": col[0], "using": using[0] if using else None,
                     "direction": _direction(srt.get("direction"), "ASC"),
                     "class": srt.get("class")}
            if srt.get("class") == "manual":
                members = []
                for m in srt.iter("member"):
                    v = m.get("value")
                    if v is None:
                        v = (m.text or "").strip()
                    if v:
                        members.append(v.strip('"'))
                entry["members"] = members
                if members:
                    sheet.manual[col[0].field] = _alias_members(self, sheet, col[0].field, members)
            sheet.sorts.append(entry)

        for ms in table.findall(".//manual-sort"):
            col = parse_shelf(ms.get("column"))
            if not col:
                continue
            members = [_unquote(b.text) for b in ms.findall(".//bucket") if b.text]
            if members:
                sheet.manual[col[0].field] = _alias_members(self, sheet, col[0].field, members)

        for fmt in table.findall(".//style-rule[@element='mark']/format[@attr='mark-labels-show']"):
            if fmt.get("value") == "true":
                sheet.labels = True

        for rl in table.findall(".//reference-line"):
            axis = parse_shelf(rl.get("axis-column"))
            val = parse_shelf(rl.get("value-column"))
            if not axis or not val:
                continue
            pcts = [_numf(v.get("percentage"), 0)
                    for v in rl.findall("reference-line-value")]
            pcts = [p for p in pcts if p > 0]
            entry = {"axis": axis[0], "value": val[0],
                     "formula": rl.get("formula") or "average",
                     "constant": rl.get("value"),
                     "probability": _numf(rl.get("probability"), 95),
                     "percentages": pcts}
            if rl.get("percentage-bands") == "true" or rl.get("fill-above") or rl.get("fill-below"):
                sheet.ref_bands.append(entry)
            else:
                sheet.ref_lines.append(entry)

        tooltip = table.find(".//customized-tooltip/formatted-text")
        if tooltip is None:
            tooltip = table.find(".//tooltip/formatted-text")
        if tooltip is not None:
            text = _tooltip_text(tooltip)
            if text:
                sheet.tooltip = text

        if table.find(".//trend-lines") is not None or table.find(".//trend-line") is not None:
            sheet.trend = True

        sheet.style = _parse_style(table)
        if sheet.style.get("cell", {}).get("background-color"):
            pass

        # totaux (formats variables selon les versions)
        for el in ws.iter():
            tag = etree.QName(el).localname
            if tag in ("row-total", "column-total", "grand-total", "totals"):
                enabled = el.get("value") not in ("false", "0", None) or el.get("show") == "true"
                if tag == "row-total":
                    sheet.totals["row"] = enabled
                elif tag == "column-total":
                    sheet.totals["col"] = enabled
                elif tag == "grand-total":
                    sheet.totals["grand"] = enabled
                elif tag == "totals" and el.get("show") == "true":
                    sheet.totals["row"] = sheet.totals["col"] = True
        for attr in ("show-row-totals", "show-column-totals"):
            if table.get(attr) == "true":
                sheet.totals["row" if "row" in attr else "col"] = True

        # hiérarchies : le premier niveau de chaque chemin sert d'indice
        src = self.sources.get(sheet.datasource or "")
        if src is not None:
            sheet.drill = src.drill_paths

        return sheet

    def _parse_dashboard(self, db, sheet_names: set[str]) -> Dashboard:
        dash = Dashboard(name=db.get("name") or "Tableau de bord")
        size = db.find("size")
        if size is not None:
            dash.width = int(size.get("maxwidth") or size.get("width") or 1000)
            dash.height = int(size.get("maxheight") or size.get("height") or 800)
            dash.sizing = size.get("sizing-mode") or "automatic"

        title = db.find("layout-options/title/formatted-text")
        if title is not None:
            dash.title = re.sub(r"<\s*Sheet Name\s*>", dash.name,
                                _runs_text(title, self._text_ref), flags=re.I)

        style = db.find("style")
        if style is not None:
            for fmt in style.findall(".//style-rule[@element='table']/format[@attr='background-color']"):
                dash.background = fmt.get("value")

        for dl in db.findall("devicelayouts/devicelayout"):
            name = dl.get("name")
            if not name:
                continue
            dash.devices.append(name)
            dsize = dl.find("size")
            dw = int(dsize.get("maxwidth") or dsize.get("width") or 1000) if dsize is not None else 1000
            dh = int(dsize.get("maxheight") or dsize.get("height") or 1400) if dsize is not None else 1400
            zones_el = dl.find("zones")
            dash.device_layouts[name] = {
                "width": dw, "height": dh,
                "zones": _parse_zones(zones_el, sheet_names, self._text_ref) if zones_el is not None else [],
            }

        zones_el = db.find("zones")
        if zones_el is not None:
            dash.zones = _parse_zones(zones_el, sheet_names, self._text_ref)

        # story points (dans la zone flipboard) : filtres capturés par étape
        for sp in db.findall(".//story-point"):
            deltas: dict[str, list[dict]] = {}
            for ws in sp.findall(".//capturedDeltas/worksheet"):
                wname = ws.get("name")
                if not wname:
                    continue
                fs = []
                for f in ws.findall("filter"):
                    pf = parse_filter(f)
                    if pf.get("field") or pf.get("expression"):
                        fs.append(pf)
                if fs:
                    deltas[wname] = fs
            dash.story.append({"caption": sp.get("caption") or "Étape",
                               "sheet": sp.get("captured-sheet") or "",
                               "filters": deltas})
        return dash

    def _tds_files(self) -> dict[str, str]:
        out = {}
        root = os.path.join(self.workdir, "_tdsx")
        for dp, _, fs in os.walk(root):
            for f in fs:
                if f.lower().endswith(".tds"):
                    out[os.path.splitext(f)[0].lower()] = os.path.join(dp, f)
        return out

    def _apply_tds(self, src: Source, path: str) -> None:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)
        root = etree.parse(path, parser).getroot()
        for col in root.findall("column"):
            f = _make_field(col)
            if f and f.name not in src.fields:
                src.fields[f.name] = f
        for conn in root.iter("connection"):
            if conn.get("class") == "hyper" and conn.get("dbname"):
                cand = os.path.join(os.path.dirname(path), conn.get("dbname").replace("\\", "/"))
                if os.path.exists(cand):
                    src.extract = cand
                    break

    def _resolve(self, db: str, hypers: dict[str, str]) -> str | None:
        rel = db.replace("\\", "/")
        root = os.path.realpath(self.workdir)
        cand = os.path.realpath(os.path.join(os.path.dirname(self.twb_path), rel))
        try:
            if os.path.commonpath([root, cand]) != root:
                return None
        except ValueError:
            return None
        if os.path.isfile(cand) and cand.lower().endswith(".hyper"):
            return cand
        fallback = hypers.get(os.path.basename(rel).lower())
        if fallback:
            fallback = os.path.realpath(fallback)
            try:
                if (os.path.commonpath([root, fallback]) == root
                        and fallback.lower().endswith(".hyper")):
                    return fallback
            except ValueError:
                pass
        return None

    # -- accès données ------------------------------------------------------- #
    def _read_catalog(self, extract: str) -> list[tuple[str, list[str]]]:
        out = []
        with Connection(endpoint=hyper_process().endpoint, database=extract) as c:
            for schema in c.catalog.get_schema_names():
                for t in c.catalog.get_table_names(schema):
                    cols = [col.name.unescaped
                            for col in c.catalog.get_table_definition(t).columns]
                    out.append((str(t), cols))
        return out

    def _catalog(self, extract: str) -> list[tuple[str, list[str]]]:
        if extract not in self._tables:
            try:
                out = self._read_catalog(extract)
            except Exception as exc:
                if not _is_conn_error(exc):
                    raise
                _close_hyper()
                out = self._read_catalog(extract)
            self._tables[extract] = out
        return self._tables[extract]

    def _table(self, extract: str, wanted: set[str] | None = None) -> str:
        tables = self._catalog(extract)
        if not tables:
            raise TWBXError("extrait vide")
        if wanted:
            scored = [(len({c.lower() for c in cols} & wanted), i, name)
                      for i, (name, cols) in enumerate(tables)]
            score, _, name = max(scored)
            if score:
                return name
        for name, _ in tables:
            if name.rsplit(".", 1)[-1].strip('"').lower() == "extract":
                return name
        return tables[0][0]

    def _columns(self, extract: str, table: str | None = None) -> list[str]:
        tables = self._catalog(extract)
        if table:
            for name, cols in tables:
                if name == table:
                    return cols
        return tables[0][1] if tables else []

    def _execute(self, extract: str, sql: str, timeout: float = QUERY_TIMEOUT,
                 _retry: bool = True) -> list[list]:
        # Exécution synchrone : le serveur Hyper n'est pas conçu pour qu'un autre
        # thread annule une connexion en cours (corruption mémoire constatée).
        def run() -> list[list]:
            with Connection(endpoint=hyper_process().endpoint, database=extract) as conn:
                with conn.execute_query(sql) as res:
                    return [[_jsonable(v) for v in row] for row in res]

        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            _close_hyper()

        timer = threading.Timer(timeout, expire) if timeout and timeout > 0 else None
        if timer is not None:
            timer.daemon = True
            timer.start()
        try:
            return run()
        except Exception as exc:
            if timed_out.is_set():
                raise TWBXError(f"requête trop longue (>{timeout:g} s)") from exc
            if _retry and _is_conn_error(exc):
                _close_hyper()
                return run()
            raise
        finally:
            if timer is not None:
                timer.cancel()

    def _table_for_object(self, extract: str, object_id: str | None) -> str | None:
        if not object_id:
            return None
        for name, _ in self._catalog(extract):
            if object_id.lower() in name.lower():
                return name
        return None

    def _plan(self, src: Source, wanted: set[str]) -> tuple[str, list[str]]:
        # les champs du .twb portent un nom logique ; l'extrait utilise remote-name
        wanted = {_remote_name(src, w).lower() for w in wanted}
        catalog = self._catalog(src.extract)
        if not catalog:
            raise TWBXError("extrait vide")
        if len(catalog) == 1 or not src.joins:
            t = self._table(src.extract, wanted)
            return t, [t]
        needed: list[str] = []
        remaining = set(wanted)
        while remaining:
            best, cover = None, set()
            for name, cols in catalog:
                hit = {c.lower() for c in cols} & remaining
                if len(hit) > len(cover):
                    best, cover = name, hit
            if not cover:
                break
            needed.append(best)
            remaining -= cover
        if len(needed) <= 1:
            t = needed[0] if needed else self._table(src.extract, wanted)
            return t, [t]

        parts, joined, pending = [needed[0]], [needed[0]], needed[1:]
        while pending:
            progress = False
            for name in list(pending):
                for rel in src.joins:
                    lt = self._table_for_object(src.extract, rel["left"])
                    rt = self._table_for_object(src.extract, rel["right"])
                    if lt in joined and rt == name:
                        cond = self._join_on(src, rel, lt, rt)
                    elif rt in joined and lt == name:
                        cond = self._join_on(src, rel, rt, lt)
                    else:
                        continue
                    if cond:
                        parts.append(f"JOIN {name} ON {cond}")
                        joined.append(name)
                        pending.remove(name)
                        progress = True
                    break
                if progress:
                    break
            if not progress:
                break
        return " ".join(parts), joined

    def _join_on(self, src: Source, rel: dict, left_table: str, right_table: str) -> str:
        swapped = self._table_for_object(src.extract, rel["left"]) != left_table
        conds = []
        for a, b in rel["on"]:
            if swapped:
                a, b = b, a
            la, ta = self._join_expr(src, a, left_table)
            rb, tb = self._join_expr(src, b, right_table)
            if not la or not rb:
                continue
            if ta == "str" and tb == "str":
                la, rb = f'({la}) COLLATE "C"', f'({rb}) COLLATE "C"'
            conds.append(f"{la} = {rb}")
        return " AND ".join(conds)

    def _join_expr(self, src: Source, expr: str, table: str) -> tuple[str | None, str]:
        expr = (expr or "").strip()

        def qual(name: str) -> str:
            f = src.fields.get(name)
            name = f.name if f else name
            return f"{table}.{quote(_remote_name(src, name))}"

        m = re.fullmatch(r"\[([^\]]+)\]", expr)
        try:
            if m:
                f = src.fields.get(m.group(1))
                return qual(m.group(1)), _field_type(f)
            sql = translate(
                expr,
                lambda n: (qual(n), _field_type(src.fields.get(n))),
                self.params)
            return sql, "str"
        except CalcError:
            return None, ""

    def _resolver(self, src: Source, tables: list[str] | None = None,
                  lod_alias: str | None = None, tc_alias: str | None = None):
        if not tables:
            tables = [self._table(src.extract)]
        lower: dict[str, str] = {}
        for t in tables:
            for c in self._columns(src.extract, t):
                ref = (f'{quote(lod_alias)}.{quote(c)}' if lod_alias
                       else f"{t}.{quote(c)}")
                lower.setdefault(c.lower(), ref)

        def col(field_name: str) -> str | None:
            f = src.fields.get(field_name)
            name = f.name if f else field_name
            if name.startswith("__lod_"):
                return f'{quote(lod_alias)}.{quote(name)}' if lod_alias else None
            if name.startswith("__tc_"):
                return f'{quote(tc_alias)}.{quote(name)}' if tc_alias else None
            c = lower.get(name.lower())
            if c is None:
                remote = _remote_name(src, name)
                if remote != name:
                    c = lower.get(remote.lower())
            return c

        def sql_for(name: str, stack: tuple = ()) -> tuple[str, str]:
            if name in stack:
                raise CalcError("référence circulaire")
            f = src.fields.get(name)
            if f is not None and f.formula:
                formula = _rewrite_sets(f.formula, src.sets)
                try:
                    formula, _ = extract_lods(formula)
                except CalcError:
                    pass
                if tc_alias:
                    try:
                        formula, _ = extract_table_calcs(formula)
                    except CalcError:
                        pass
                sql = translate(formula, lambda n: sql_for(n, stack + (name,)), self.params)
                return sql, _field_type(f)
            if f is not None and f.group:
                base, btype = sql_for(f.group["field"], stack + (name,))
                whens = []
                for val, grp in f.group["members"].items():
                    whens.append(f"WHEN {base} = {_literal(val, _datatype_of(src, f.group['field']))} "
                                 f"THEN {_str_literal(grp)}")
                return f"(CASE {' '.join(whens)} ELSE 'Autre' END)", "str"
            if f is not None and f.bin:
                base, _ = sql_for(f.bin["field"], stack + (name,))
                size, peg = f.bin["size"], f.bin["peg"]
                return f"(FLOOR(({base} - {peg}) / {size}) * {size} + {peg})", "num"
            if name in src.sets:
                st = src.sets[name]
                base, _ = sql_for(st["field"], stack + (name,))
                dt = _datatype_of(src, st["field"])
                vals = ", ".join(_literal(v, dt) for v in st["members"])
                return f"({base} IN ({vals}))", "bool"
            c = col(name)
            if not c:
                raise CalcError(f"colonne absente de l'extrait : {name}")
            return c, _field_type(f)

        def is_agg(name: str, stack: tuple = ()) -> bool:
            f = src.fields.get(name)
            if f is None or not f.formula or name in stack:
                return False
            if has_aggregate(f.formula) or has_table_calc(f.formula) or "{" in f.formula:
                return True
            return any(is_agg(r, stack + (name,)) for r in referenced_fields(f.formula))

        return sql_for, is_agg, lower

    # -- LOD / table calcs --------------------------------------------------- #
    def _collect_lods(self, src: Source, items: list[ShelfItem], view_dims: list[str]) -> list[dict]:
        specs: dict[str, dict] = {}

        def walk(name: str, stack: frozenset) -> None:
            f = src.fields.get(name)
            if f is None or not f.formula or name in stack:
                return
            try:
                clean, lods = extract_lods(f.formula)
            except CalcError:
                return
            for sp in lods:
                if sp["alias"] in specs:
                    continue
                sp = dict(sp)
                if sp["kind"] == "INCLUDE":
                    sp["dims"] = list(dict.fromkeys(view_dims + sp["dims"]))
                elif sp["kind"] == "EXCLUDE":
                    sp["dims"] = [d for d in view_dims if d not in sp["dims"]]
                specs[sp["alias"]] = sp
            for ref in referenced_fields(clean):
                walk(ref, stack | {name})

        for it in items:
            walk(it.field, frozenset())
        return list(specs.values())

    def _wrap_lods(self, src: Source, from_sql: str, tables: list[str], specs: list[dict]) -> str:
        sql_for, _, _ = self._resolver(src, tables)
        parts = []
        for sp in specs:
            try:
                expr = translate(sp["expr"], sql_for, self.params)
                if not has_aggregate(sp["expr"]):
                    expr = f"SUM({expr})"
                dims = [sql_for(d)[0] for d in sp["dims"]]
            except CalcError as exc:
                self.warnings.append(f"LOD {sp['alias']} : {exc}")
                continue
            part = f"PARTITION BY {', '.join(dims)}" if dims else ""
            parts.append(f"{expr} OVER ({part}) AS {quote(sp['alias'])}")
        if not parts:
            return from_sql
        return f"(SELECT *, {', '.join(parts)} FROM {from_sql}) AS {quote('_lod')}"

    def _has_table_calc(self, src: Source, name: str, stack: frozenset = frozenset()) -> bool:
        f = src.fields.get(name)
        if f is None or not f.formula or name in stack:
            return False
        if has_table_calc(f.formula):
            return True
        return any(self._has_table_calc(src, r, stack | {name})
                   for r in referenced_fields(f.formula))

    def _collect_table_calcs(self, src: Source, items: list[ShelfItem],
                             sql_for) -> tuple[dict[str, dict], dict[str, str]]:
        specs: dict[str, dict] = {}
        hidden: dict[str, str] = {}

        def walk(name: str, stack: frozenset) -> None:
            f = src.fields.get(name)
            if f is None or not f.formula or name in stack:
                return
            try:
                clean, calls = extract_table_calcs(f.formula)
            except CalcError:
                return
            for sp in calls:
                if sp["alias"] in specs:
                    continue
                try:
                    args = [translate(a, sql_for, self.params)
                            for a in split_args_text(sp["args"])]
                except CalcError as exc:
                    self.warnings.append(f"calcul de table {sp['func']} : {exc}")
                    continue
                aliased = []
                for i, a in enumerate(args):
                    if i == 0:
                        ha = "__tcarg_" + _hash(a)
                        hidden[ha] = a
                        aliased.append(ha)
                    else:
                        aliased.append(a)
                specs[sp["alias"]] = {"func": sp["func"], "args": aliased, "alias": sp["alias"]}
            for ref in referenced_fields(clean):
                walk(ref, stack | {name})

        for it in items:
            walk(it.field, frozenset())
        return specs, hidden

    # -- requêtes ------------------------------------------------------------ #
    def reference_stats(self, src: Source, item: ShelfItem, filters: list[dict],
                        from_sql: str | None = None,
                        tables: list[str] | None = None,
                        formula: str = "average",
                        constant=None) -> dict | None:
        if formula == "constant":
            return {"value": _numf(constant, 0), "stdev": None, "n": 1}
        if from_sql is None:
            from_sql, tables = self._plan(src, {item.field.lower()})
        sql_for, is_agg, lower = self._resolver(src, tables)
        base_expr = sql_for(item.field)[0]
        fn = {"average": "AVG", "median": "MEDIAN", "min": "MIN",
              "max": "MAX"}.get(formula, "AVG")
        expr = f"{fn}({base_expr})" if not is_agg(item.field) else base_expr
        where = [sql for sql in (filter_sql(f, lower, src.fields, sql_for, is_agg, from_sql, self.params)
                                 for f in filters) if sql]
        sql = f"SELECT {expr}, STDDEV_SAMP({base_expr}), COUNT(*) FROM {from_sql}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._execute(src.extract, sql)
        row = rows[0] if rows else None
        if not row:
            return None
        return {"value": None if row[0] is None else float(row[0]),
                "stdev": None if row[1] is None else float(row[1]),
                "n": int(row[2] or 0)}

    def reference_value(self, src: Source, item: ShelfItem, filters: list[dict],
                        from_sql: str | None = None,
                        tables: list[str] | None = None,
                        formula: str = "average",
                        constant=None) -> float | None:
        st = self.reference_stats(src, item, filters, from_sql, tables,
                                  formula, constant)
        return st["value"] if st else None

    def filter_options(self, src: Source, filters: list[dict],
                       from_sql: str | None = None,
                       tables: list[str] | None = None) -> list[dict]:
        if from_sql is None:
            from_sql, tables = self._plan(
                src, self._wanted_fields(src, [], filters))
        sql_for, is_agg, lower = self._resolver(src, tables)
        out = []
        for f in filters:
            field = f.get("field")
            if not field:
                continue
            entry = {"field": field, "class": f.get("class") or "categorical",
                     "mode": f.get("mode"), "selected": f.get("members", []),
                     "exclude": f.get("exclude", []), "none": f.get("none", False),
                     "expression": f.get("expression"),
                     "range": f.get("range"),
                     "relative": f.get("relative"), "wildcard": f.get("wildcard"),
                     "top": f.get("top")}
            fld = src.fields.get(_base_field(field))
            if fld and fld.aliases:
                entry["valueLabels"] = fld.aliases
            base_field = _base_field(field)
            expr = lower.get(base_field.lower())
            if not expr and not is_agg(base_field):
                try:
                    expr = sql_for(base_field)[0]
                except CalcError:
                    expr = None
            if expr and (f.get("class") or "categorical") == "categorical":
                m = _TRUNC_NAME.match(field)
                if m:
                    suffix = m.group(2)
                    if suffix in DATE_PART:
                        expr = f"date_part('{DATE_PART[suffix]}', {expr})"
                    elif suffix in DATE_TRUNC:
                        expr = f"date_trunc('{DATE_TRUNC[suffix]}', {expr})"
                key = (src.extract, expr)
                if key not in self._distinct:
                    if len(self._distinct) >= 256:
                        self._distinct.clear()
                    try:
                        rows = self._execute(
                            src.extract,
                            f"SELECT DISTINCT {expr} FROM {from_sql} "
                            f"WHERE {expr} IS NOT NULL ORDER BY 1 LIMIT 500")
                        self._distinct[key] = [row[0] for row in rows]
                    except Exception:
                        self._distinct[key] = []
                entry["values"] = self._distinct[key]
            out.append(entry)
        return out

    def render_sheet_by_name(self, name: str, params: dict | None = None,
                             filters: dict | None = None,
                             drill: list[str] | None = None) -> dict:
        _validate_sheet_inputs(params, filters, drill)
        with self._lock:
            old_params = dict(self.params)
            self.warnings = []
            try:
                return self._render_sheet_by_name_locked(name, params, filters, drill)
            finally:
                self.params = old_params

    def _render_sheet_by_name_locked(self, name: str, params: dict | None = None,
                                     filters: dict | None = None,
                                     drill: list[str] | None = None) -> dict:
        sheet = next((s for s in self.sheets if s.name == name), None)
        if sheet is None:
            raise TWBXError(f"feuille inconnue : {name}")
        sheet = replace(sheet, filters=[dict(f) for f in sheet.filters])
        if drill:
            extra = [ShelfItem(ds=sheet.datasource or "", agg="none", field=f, kind="nk")
                     for f in drill]
            sheet.rows = list(sheet.rows) + extra

        if params:
            dts = {p["name"]: p["datatype"] for p in self.param_meta}
            for pname, raw in params.items():
                if pname in self.params:
                    self.params[pname] = _param_literal(raw, dts.get(pname))

        if filters:
            known = {f.get("field") for f in sheet.filters}
            used = {i.field for i in sheet.rows + sheet.cols
                    + [i for lst in sheet.encodings.values() for i in lst]}
            for f in sheet.filters:
                if f.get("field") in filters:
                    val = filters[f["field"]]
                    if isinstance(val, dict):
                        if any(k in val for k in ("range", "relative", "wildcard", "top")):
                            f["members"] = []
                            f["exclude"] = []
                        if val.get("members") is not None:
                            f["exclude"] = []
                            f["none"] = False
                        if val.get("exclude") is not None:
                            f["members"] = []
                            f["none"] = False
                        if val.get("none"):
                            f["members"] = []
                            f["exclude"] = []
                        f.update(val)
                    else:
                        f["members"] = list(val or [])
                        f["exclude"] = []
                        f["range"] = None
                        f["relative"] = None
                        f["wildcard"] = None
                        f["top"] = None
            for field, values in filters.items():
                if field not in known and values and _base_field(field) in used:
                    if isinstance(values, dict):
                        f = {"field": field, "class": "categorical", "members": [],
                             "exclude": [], "nonnull": False}
                        f.update(values)
                        sheet.filters.append(f)
                    else:
                        sheet.filters.append({"field": field, "class": "categorical",
                                              "members": list(values), "exclude": [],
                                              "nonnull": False})

        return self._render_sheet(sheet)

    def query(self, src: Source, items: list[ShelfItem], filters: list[dict],
              sorts: list[dict] | None = None, from_sql: str | None = None,
              tables: list[str] | None = None, view_dims: list[str] | None = None,
              limit: int = MAX_ROWS) -> tuple[list[str], list[list]]:
        dims, measures = [], []
        for it in _dedupe(items):
            (dims if AGGS.get(it.agg) is None else measures).append(it)

        if from_sql is None:
            from_sql, tables = self._plan(src, {i.field.lower() for i in items})

        if view_dims is None:
            view_dims = [d.field for d in dims]

        lod_specs = self._collect_lods(src, items, view_dims)
        lod_alias = None
        if lod_specs:
            wrapped = self._wrap_lods(src, from_sql, tables, lod_specs)
            if wrapped != from_sql:
                from_sql = wrapped
                lod_alias = "_lod"

        base_for, _, lower = self._resolver(src, tables, lod_alias=lod_alias)
        tc_specs, tc_hidden = self._collect_table_calcs(src, items, base_for)
        sql_for, is_agg, _ = self._resolver(src, tables, lod_alias=lod_alias,
                                            tc_alias="_tc" if tc_specs else None)

        base_select, base_names, group = [], [], []
        dim_names = []
        for d in dims:
            try:
                expr = base_for(d.field)[0]
            except CalcError as exc:
                self.warnings.append(f"{d.field} : {exc}")
                continue
            if is_agg(d.field):
                df = src.fields.get(d.field)
                if not (df and "{" in (df.formula or "")):
                    self.warnings.append(f"{d.field} : dimension calculée agrégée non supportée")
                    continue
            unit = TRUNC_AGG.get(d.agg)
            if unit:
                expr = f"date_trunc('{unit}', {expr})"
            elif d.agg in PART_UNITS:
                expr = f"date_part('{PART_UNITS[d.agg]}', {expr})"
            if _is_spatial(src, d.field):
                expr = f"CAST({expr} AS VARCHAR)"
            name = dim_name(d)
            base_select.append(f"{expr} AS {quote(name)}")
            base_names.append(name)
            dim_names.append(name)
            group.append(expr)

        tc_labels = set()
        for m in measures:
            label = f"{m.agg}:{m.field}"
            if label in base_names:
                continue
            if m.field == "__records":
                base_select.append(f"COUNT(*) AS {quote(label)}")
                base_names.append(label)
                continue
            if self._has_table_calc(src, m.field):
                tc_labels.add(label)
                # les agrégats cités par le calcul de table doivent être
                # calculés dans le sous-requête, même s'ils ne sont pas sur l'étagère
                for rlabel in _agg_refs(src.fields[m.field].formula or ""):
                    if rlabel in base_names:
                        continue
                    ragg, rfield = rlabel.split(":", 1)
                    try:
                        rexpr = base_for(rfield)[0]
                    except CalcError:
                        continue
                    rfn = AGGS.get(ragg)
                    if ragg in ("quart1", "quart3"):
                        q = 0.25 if ragg == "quart1" else 0.75
                        rexpr = f"PERCENTILE_CONT({q}) WITHIN GROUP (ORDER BY {rexpr})"
                    elif rfn == "ASIS":
                        rexpr = (f"MAX({rexpr})" if is_agg(rfield) else f"SUM({rexpr})")
                    elif not is_agg(rfield):
                        rexpr = (f"COUNT(DISTINCT {rexpr})" if rfn == "COUNT_DISTINCT"
                                 else f"{rfn}({rexpr})")
                    elif src.fields.get(rfield) and "{" in (src.fields[rfield].formula or ""):
                        rexpr = f"MAX({rexpr})"
                    base_select.append(f"{rexpr} AS {quote(rlabel)}")
                    base_names.append(rlabel)
                continue
            try:
                expr = base_for(m.field)[0]
            except CalcError as exc:
                self.warnings.append(f"{m.field} : {exc}")
                continue
            fn = AGGS.get(m.agg)
            if m.agg in ("quart1", "quart3"):
                q = 0.25 if m.agg == "quart1" else 0.75
                expr = (f"PERCENTILE_CONT({q}) WITHIN GROUP "
                        f"(ORDER BY {expr})")
            elif fn == "ASIS":
                mf = src.fields.get(m.field)
                if is_agg(m.field):
                    if mf and "{" in (mf.formula or ""):
                        expr = f"MAX({expr})"     # colonne LOD : constante sur le groupe
                else:
                    expr = f"SUM({expr})"
            elif not is_agg(m.field):
                expr = (f"COUNT(DISTINCT {expr})" if fn == "COUNT_DISTINCT"
                        else f"{fn}({expr})")
            else:
                mf = src.fields.get(m.field)
                if mf and "{" in (mf.formula or ""):
                    expr = f"MAX({expr})"     # LOD : constante sur le groupe
            if _is_spatial(src, m.field):
                expr = f"CAST({expr} AS VARCHAR)"
            base_select.append(f"{expr} AS {quote(label)}")
            base_names.append(label)

        for ha, expr in tc_hidden.items():
            base_select.append(f"{expr} AS {quote(ha)}")

        if not base_select:
            return [], []

        order = []
        for srt in sorts or []:
            target = None
            if srt.get("using"):
                label = f"{srt['using'].agg}:{srt['using'].field}"
                if label in base_names:
                    target = quote(label)
            if target is None:
                label = dim_name(srt["column"])
                if label in base_names:
                    target = quote(label)
            if target:
                order.append(f"{target} {_direction(srt.get('direction'), 'ASC')}")

        def assemble(fs: list[dict], sql_from: str) -> str:
            where = [w for w in (filter_sql(f, lower, src.fields, base_for, is_agg, sql_from, self.params)
                                 for f in fs) if w]
            q = f"SELECT {', '.join(base_select)} FROM {sql_from} "
            if where:
                q += "WHERE " + " AND ".join(where) + " "
            if group:
                q += f"GROUP BY {', '.join(group)} "
            if order:
                q += "ORDER BY " + ", ".join(order) + " "
            return q + f"LIMIT {limit}"

        sql = assemble(filters, from_sql)

        def run(base_sql: str):
            if not tc_specs:
                return base_names, self._execute(src.extract, base_sql)
            order_sql = ", ".join(f'"_tc".{quote(n)}' for n in dim_names) or "1"
            wins = []
            for alias, sp in tc_specs.items():
                args = [f'"_tc".{quote(a)}' if a in tc_hidden else a for a in sp["args"]]
                try:
                    expr = window_sql(sp["func"], args, order_sql,
                                      desc=sp["func"].startswith("RANK"))
                except CalcError as exc:
                    self.warnings.append(f"calcul de table {sp['func']} : {exc}")
                    continue
                wins.append(f"{expr} AS {quote(alias)}")
            if not wins:
                return base_names, self._execute(src.extract, base_sql)
            win = f"SELECT *, {', '.join(wins)} FROM ({base_sql}) AS {quote('_tc')}"
            tc_for, _, _ = self._resolver(src, tables, lod_alias="_tc2", tc_alias="_tc2")
            cols, names = [], list(base_names)
            for name in base_names:
                cols.append(f'"_tc2".{quote(name)} AS {quote(name)}')
            for m in measures:
                label = f"{m.agg}:{m.field}"
                if label not in tc_labels:
                    continue
                try:
                    expr = tc_for(m.field)[0]
                except CalcError as exc:
                    self.warnings.append(f"{m.field} : {exc}")
                    continue
                # dans un calcul de table, `SUM([x])` désigne déjà la mesure
                # agrégée par groupe : on lit la colonne du sous-requête.
                for bname in base_names:
                    if ":" not in bname or bname in tc_labels:
                        continue
                    bagg, bfield = bname.split(":", 1)
                    fn = AGGS.get(bagg)
                    if not fn or fn in ("COUNT_DISTINCT", "PERCENTILE", "ASIS"):
                        continue
                    try:
                        raw = tc_for(bfield)[0]
                    except CalcError:
                        continue
                    expr = expr.replace(f"{fn}({raw})", f'"_tc2".{quote(bname)}')
                cols.append(f"{expr} AS {quote(label)}")
                names.append(label)
            outer_sql = (f"SELECT {', '.join(cols)} FROM ({win}) AS {quote('_tc2')} "
                         f"LIMIT {limit}")
            return names, self._execute(src.extract, outer_sql)

        try:
            return run(sql)
        except TWBXError:
            raise
        except Exception:
            physical = [f for f in filters
                        if f.get("field") and _base_field(f["field"]).lower() in lower]
            fallback = assemble(physical, from_sql)
            if fallback == sql:
                raise
            dropped = [str(f.get("field")) for f in filters if f not in physical]
            self.warnings.append("filtre calculé ignoré (échec d'évaluation) : "
                                 + ", ".join(dropped))
            return run(fallback)

    def _wanted_fields(self, src: Source, items: list[ShelfItem],
                       filters: list[dict] | None = None) -> set[str]:
        """Champs physiques nécessaires, dépendances de calculs/groupes/sets comprises."""
        out: set[str] = set()

        def walk(name: str, stack: frozenset = frozenset()) -> None:
            if name == "__records":
                return
            key = name.lower()
            if key in out or name in stack:
                return
            out.add(key)
            f = src.fields.get(name)
            if f is not None:
                if f.formula:
                    for r in referenced_fields(f.formula):
                        walk(r, stack | {name})
                if f.group:
                    walk(f.group["field"], stack | {name})
                if f.bin:
                    walk(f.bin["field"], stack | {name})
            if name in src.sets:
                walk(src.sets[name]["field"], stack | {name})

        for it in items:
            walk(it.field)
        for f in filters or []:
            if f.get("field"):
                walk(_base_field(f["field"]))
        return out

    def _source_for(self, s: Sheet) -> Source:
        if s.datasource in self.sources and self.sources[s.datasource].extract:
            return self.sources[s.datasource]
        items = s.rows + s.cols + [i for lst in s.encodings.values() for i in lst]
        for it in items:
            src = self.sources.get(it.ds)
            if src and src.extract:
                return src
        for src in self.sources.values():
            if src.extract:
                return src
        raise TWBXError("aucune source avec extrait")

    # -- rendu --------------------------------------------------------------- #
    def render(self, lazy: bool = False) -> dict:
        with self._lock:
            self.warnings = []
            return self._render_locked(lazy)

    def _render_locked(self, lazy: bool = False) -> dict:
        if lazy:
            # gros classeurs : les feuilles seront rendues à la demande par l'API
            sheets = [{"name": s.name, "title": self._title_for(s),
                       "tab_color": s.tab_color, "kind": "pending",
                       "labels": {}, "filters": [], "total": 0}
                      for s in self.sheets]
        else:
            sheets = []
            for s in self.sheets:
                try:
                    sheets.append(self._render_sheet(s))
                except Exception as exc:
                    self.warnings.append(f"{s.name} : {exc}")
        return {
            "sheets": sheets,
            "dashboards": [{"name": d.name, "title": d.title, "zones": d.zones,
                            "width": d.width, "height": d.height,
                            "background": d.background, "story": d.story,
                            "devices": d.devices, "device_layouts": d.device_layouts,
                            "viewpoints": d.viewpoints, "sizing": d.sizing,
                            "tab_color": d.tab_color}
                           for d in self.dashboards],
            "parameters": self.param_meta,
            "actions": self.actions,
            "warnings": list(dict.fromkeys(self.warnings)),
            "lazy": lazy,
        }

    def _render_sheet(self, s: Sheet) -> dict:
        src = self._source_for(s)
        enc_items = [i for lst in s.encodings.values() for i in lst]
        items = _dedupe(s.rows + s.cols + enc_items)
        if s.encodings.get("geometry"):
            # champs d'infobulle : sans eux, les lignes de la carte sont vides
            items = _dedupe(items + _tooltip_items(s.tooltip))
        # feuille multi-sources (cartes superposées) : on rend la couche principale
        items = [i for i in items if not i.ds or i.ds == src.name]

        row_dims = [i for i in s.rows if AGGS.get(i.agg) is None]
        col_dims = [i for i in s.cols if AGGS.get(i.agg) is None]
        dims = row_dims + col_dims
        if not dims and s.mark.lower() == "pie":
            dims = [i for i in enc_items
                    if AGGS.get(i.agg) is None and (not i.ds or i.ds == src.name)]

        if s.measure_filter:
            # :Measure Names ne filtre que les mesures de l'étagère (pas les
            # mesures de couleur / texte / taille, qui restent indépendantes).
            allowed = set(s.measure_filter)
            shelf_keys = {f"{i.agg}:{i.field}" for i in s.rows + s.cols
                          if AGGS.get(i.agg) is not None}
            items = [i for i in items
                     if (AGGS.get(i.agg) is None
                         or f"{i.agg}:{i.field}" not in shelf_keys
                         or f"{i.agg}:{i.field}" in allowed)]

        def enc_measures(tag: str) -> list[ShelfItem]:
            keys = {f"{i.agg}:{i.field}" for i in s.encodings.get(tag, [])
                    if AGGS.get(i.agg) is not None}
            return [i for i in items if f"{i.agg}:{i.field}" in keys]

        text_measures = enc_measures("text")
        color_measures = enc_measures("color")
        size_measures = enc_measures("size")
        measures = [i for i in items if AGGS.get(i.agg) is not None]
        text_dims = [i for i in s.encodings.get("text", []) if AGGS.get(i.agg) is None]
        lod_dims = [i for i in s.encodings.get("lod", []) if AGGS.get(i.agg) is None]
        detail_dims = [i for i in s.encodings.get("detail", []) if AGGS.get(i.agg) is None]
        treemap_dim = next(iter(text_dims or lod_dims or detail_dims), None)
        treemap = (s.mark.lower() == "square" and not row_dims and not col_dims
                   and bool(size_measures) and treemap_dim is not None)
        if treemap:
            dims = [treemap_dim]
        item_keys = {(i.agg, i.field) for i in items}
        axis_measures = [i for i in s.rows + s.cols
                         if AGGS.get(i.agg) is not None and (i.agg, i.field) in item_keys]
        crosstab_like = (len(dims) >= 2 and s.mark.lower() != "pie"
                         and not s.encodings.get("geometry"))
        count_item: ShelfItem | None = None

        def add_count() -> None:
            nonlocal count_item, items, measures, axis_measures
            if count_item is not None:
                return
            count_item = ShelfItem(ds=src.name, agg="cnt", field="__records", kind="qk")
            items = items + [count_item]
            measures = measures + [count_item]
            axis_measures = [count_item]

        # Tableau ajoute un COUNT(*) implicite quand une vue n'a que des dimensions.
        if not axis_measures and not s.encodings.get("geometry") and not treemap:
            if crosstab_like:
                if not text_measures and not color_measures:
                    add_count()
            elif dims and not text_measures:
                add_count()
        if not measures and dims and not s.encodings.get("geometry") and not treemap:
            add_count()

        wanted = self._wanted_fields(src, items, s.filters)
        from_sql, tables = self._plan(src, wanted)
        view_dims = [i.field for i in (s.rows + s.cols) if AGGS.get(i.agg) is None]
        names, rows = self.query(src, items, s.filters, s.sorts, from_sql, tables, view_dims)
        data = [dict(zip(names, r)) for r in rows]

        formats, labels = {}, {}
        for name in names:
            base = name.split(":", 1)[1] if ":" in name else name
            labels[name] = _field_label(src, base)
            f = src.fields.get(base)
            fmt = _parse_format(f.number_format if f else None)
            if fmt:
                formats[name] = fmt
        if count_item is not None:
            labels["cnt:__records"] = "Nombre de lignes"
        for flt in s.filters:
            base = flt.get("field")
            if not base:
                continue
            labels.setdefault(base, _field_label(src, _base_field(base)))
        mn = src.fields.get(":Measure Names")
        if mn and mn.aliases:
            for key, val in mn.aliases.items():
                m = _REF.search(key)
                if not m:
                    continue
                parts = m.group(2).split(":")
                if len(parts) >= 3:
                    labels[f"{parts[0]}:{':'.join(parts[1:-1])}"] = val

        alias_reverse = {}
        for name in names:
            if ":" in name or "(" in name:
                continue
            f = src.fields.get(name)
            if not f or not f.aliases:
                continue
            alias_reverse[name] = {v: k for k, v in f.aliases.items()}
            for row in data:
                v = row.get(name)
                if v is not None and str(v) in f.aliases:
                    row[name] = f.aliases[str(v)]

        base = {"name": s.name, "title": self._title_for(s), "mark": s.mark,
                "tab_color": s.tab_color,
                "columns": names, "total": len(data),
                "formats": formats, "labels": labels,
                "aliasReverse": alias_reverse,
                "tooltip": s.tooltip,
                "drill": s.drill,
                "filters": self.filter_options(src, s.filters, from_sql, tables)}

        if not names:
            return {**base, "kind": "notice",
                    "message": "Feuille sans champ exploitable : champs calculés, paramètres "
                               "ou colonnes absentes de l'extrait."}

        if s.encodings.get("geometry"):
            carte = map_payload(s, data, names, labels, formats, s.style)
            if carte:
                return {**base, **carte}

        if treemap:
            size_key = f"{size_measures[0].agg}:{size_measures[0].field}"
            ci = next((i for i in s.encodings.get("color", [])
                       if f"{i.agg}:{i.field}" in names or i.field in names), None)
            color_key = (f"{ci.agg}:{ci.field}" if ci and f"{ci.agg}:{ci.field}" in names
                         else (ci.field if ci else None))
            spec = treemap_spec(s, treemap_dim, size_key, color_key, names, data,
                                labels, formats, s.style)
            if spec:
                spec = _apply_custom_tooltip(spec, s, names)
                return {**base, "kind": "chart", "spec": spec}

        if not dims and len(measures) == 1 and names and len(data) == 1:
            label = f"{measures[0].agg}:{measures[0].field}"
            f = src.fields.get(measures[0].field)
            return {**base, "kind": "kpi", "value": data[0].get(label),
                    "label": labels.get(label) or (f.caption if f else measures[0].field),
                    "format": formats.get(label)}

        def present(lst: list[ShelfItem]) -> list[str]:
            return [f"{i.agg}:{i.field}" for i in lst
                    if f"{i.agg}:{i.field}" in names]

        display = present(axis_measures) or present(text_measures) or present(color_measures)
        if not display and count_item is not None:
            display = present([count_item])
        display = list(dict.fromkeys(display))

        if crosstab_like and display:
            if len(data) > CROSSTAB_MAX:
                self.warnings.append(f"{s.name} : tableau croisé tronqué à {CROSSTAB_MAX} lignes")
            display_items = [i for i in items if f"{i.agg}:{i.field}" in display]
            totals = (self._crosstab_totals(src, s, from_sql, tables, row_dims, col_dims,
                                            display_items)
                      if s.totals else None)
            if totals:
                for key in ("row", "col", "grand"):
                    for rec in totals.get(key, []):
                        for dim in row_dims + col_dims:
                            dn = dim_name(dim)
                            if dn in rec:
                                df = src.fields.get(_base_field(dn))
                                if df and df.aliases and str(rec[dn]) in df.aliases:
                                    rec[dn] = df.aliases[str(rec[dn])]
            color_info = None
            ci = next((i for i in color_measures if f"{i.agg}:{i.field}" in names), None)
            if ci is not None:
                key = f"{ci.agg}:{ci.field}"
                color_info = {"field": key, "type": "measure",
                              "label": labels.get(key, key),
                              "format": formats.get(key)}
            else:
                ci = next((i for i in s.encodings.get("color", [])
                           if AGGS.get(i.agg) is None
                           and (f"{i.agg}:{i.field}" in names or i.field in names)), None)
                if ci is not None:
                    key = (f"{ci.agg}:{ci.field}" if f"{ci.agg}:{ci.field}" in names
                           else ci.field)
                    color_info = {"field": key, "type": "dimension",
                                  "label": labels.get(key, key),
                                  "format": formats.get(key)}
            return {**base, "kind": "crosstab",
                    "rowDims": [dim_name(d) for d in row_dims if dim_name(d) in names],
                    "colDims": [dim_name(d) for d in col_dims if dim_name(d) in names],
                    "measures": display,
                    "measure": display[0] if display else None,
                    "records": data[:CROSSTAB_MAX],
                    "manual": s.manual, "totals": totals,
                    "cellStyle": s.style.get("cell", {}),
                    "color": color_info}

        text_only = present(text_measures)
        if text_only and not any(AGGS.get(i.agg) is not None for i in s.rows + s.cols):
            return {**base, "kind": "table", "rows": data[:1000]}

        refs, bands = [], []
        for rl in s.ref_lines:
            try:
                v = self.reference_value(src, rl["value"], s.filters, from_sql, tables,
                                         rl.get("formula", "average"), rl.get("constant"))
            except Exception:
                v = None
            if v is not None:
                refs.append({"axis": f"{rl['axis'].agg}:{rl['axis'].field}", "value": v})
        for rb in s.ref_bands:
            try:
                st = self.reference_stats(src, rb["value"], s.filters, from_sql, tables,
                                          rb.get("formula", "average"), rb.get("constant"))
            except Exception:
                st = None
            if st and st["value"] is not None:
                pcts = rb.get("percentages") or [rb.get("probability") or 95]
                for p in pcts:
                    z = (NormalDist().inv_cdf(0.5 + p / 200.0)
                         if 0 < p < 100 else 1.96)
                    half = 0.0
                    if st["stdev"] and st["n"]:
                        half = z * st["stdev"] / (st["n"] ** 0.5)
                    bands.append({"axis": f"{rb['axis'].agg}:{rb['axis'].field}",
                                  "value": st["value"], "low": st["value"] - half,
                                  "high": st["value"] + half,
                                  "percentage": p})

        axis_measures = axis_measures or [i for i in measures
                                           if f"{i.agg}:{i.field}" in names]
        spec = vega_spec(s, dims, axis_measures, names, data, refs, labels, formats,
                         s.style, bands)
        if spec is not None:
            spec = _apply_custom_tooltip(spec, s, names)
        if spec is None:
            return {**base, "kind": "table", "rows": data[:1000]}
        return {**base, "kind": "chart", "spec": spec}

    def _crosstab_totals(self, src: Source, s: Sheet, from_sql: str, tables: list[str],
                         row_dims: list[ShelfItem], col_dims: list[ShelfItem],
                         measure_items: list[ShelfItem]) -> dict:
        out: dict = {}
        measure_items = _dedupe(measure_items)
        try:
            if s.totals.get("col") and row_dims:
                names, rows = self.query(src, col_dims + measure_items, s.filters,
                                         from_sql=from_sql, tables=tables,
                                         view_dims=[i.field for i in col_dims])
                out["col"] = [dict(zip(names, r)) for r in rows]
            if s.totals.get("row") and col_dims:
                names, rows = self.query(src, row_dims + measure_items, s.filters,
                                         from_sql=from_sql, tables=tables,
                                         view_dims=[i.field for i in row_dims])
                out["row"] = [dict(zip(names, r)) for r in rows]
            if s.totals.get("grand") or (s.totals.get("row") and s.totals.get("col")):
                names, rows = self.query(src, measure_items, s.filters,
                                         from_sql=from_sql, tables=tables, view_dims=[])
                out["grand"] = [dict(zip(names, r)) for r in rows]
        except Exception as exc:
            self.warnings.append(f"{s.name} : totaux indisponibles ({exc})")
        return out

    def csv_sheet(self, name: str) -> tuple[list[str], list[list], dict]:
        with self._lock:
            self.warnings = []
            return self._csv_sheet_locked(name)

    def _csv_sheet_locked(self, name: str) -> tuple[list[str], list[list], dict]:
        sheet = next((s for s in self.sheets if s.name == name), None)
        if sheet is None:
            raise TWBXError(f"feuille inconnue : {name}")
        src = self._source_for(sheet)
        enc_items = [i for lst in sheet.encodings.values() for i in lst]
        items = _dedupe(sheet.rows + sheet.cols + enc_items)
        items = [i for i in items if not i.ds or i.ds == src.name]
        if sheet.measure_filter:
            allowed = set(sheet.measure_filter)
            shelf_keys = {f"{i.agg}:{i.field}" for i in sheet.rows + sheet.cols
                          if AGGS.get(i.agg) is not None}
            items = [i for i in items
                     if (AGGS.get(i.agg) is None
                         or f"{i.agg}:{i.field}" not in shelf_keys
                         or f"{i.agg}:{i.field}" in allowed)]
        if (not any(AGGS.get(i.agg) is not None for i in items)
                and (sheet.rows or sheet.cols)):
            items.append(ShelfItem(ds=src.name, agg="cnt", field="__records", kind="qk"))
        wanted = self._wanted_fields(src, items, sheet.filters)
        from_sql, tables = self._plan(src, wanted)
        names, rows = self.query(src, items, sheet.filters, sheet.sorts, from_sql, tables)
        labels = {}
        for n in names:
            base = n.split(":", 1)[1] if ":" in n else n
            f = src.fields.get(base)
            if f and f.caption:
                labels[n] = f.caption
        if "cnt:__records" in names:
            labels["cnt:__records"] = "Nombre de lignes"
        mn = src.fields.get(":Measure Names")
        if mn and mn.aliases:
            for key, val in mn.aliases.items():
                m = _REF.search(key)
                if not m:
                    continue
                parts = m.group(2).split(":")
                if len(parts) >= 3:
                    labels[f"{parts[0]}:{':'.join(parts[1:-1])}"] = val
        return names, rows, labels


# --------------------------------------------------------------------------- #
# helpers de parsing
# --------------------------------------------------------------------------- #
def _make_field(col) -> Field | None:
    name = (col.get("name") or "").strip("[]")
    if not name:
        return None
    calc = col.find("calculation")
    f = Field(name=name, caption=col.get("caption") or name,
              datatype=col.get("datatype") or "string",
              role=col.get("role") or "dimension",
              formula=calc.get("formula") if calc is not None else None,
              number_format=col.get("default-format"))
    al = col.find("aliases")
    if al is not None:
        f.aliases = {_unquote(a.get("key")): _unquote(a.get("value"))
                     for a in al.findall("alias") if a.get("key") is not None}
    grp = col.find("group")
    if grp is not None:
        f.group = _parse_group(grp)
    bin_el = col.find("bin")
    if bin_el is not None and bin_el.get("size"):
        lvl = (bin_el.get("level") or "").strip("[]")
        if lvl:
            f.bin = {"field": lvl, "size": _numf(bin_el.get("size"), 1),
                     "peg": _numf(bin_el.get("peg"), 0)}
    return f


def _parse_group(el) -> dict | None:
    base, members = None, {}

    def walk(node, caption=None):
        nonlocal base
        fn = node.get("function")
        cap = node.get("caption") or caption
        if fn == "member" and node.get("member") is not None:
            lvl = (node.get("level") or "").strip("[]")
            if lvl:
                base = lvl
            members[_unquote(node.get("member"))] = cap or "Groupe"
        else:
            for child in node:
                walk(child, cap)

    walk(el)
    if base and members:
        return {"field": base, "members": members}
    return None


def _parse_set(el) -> dict | None:
    base, members = None, []

    def walk(node):
        nonlocal base
        fn = node.get("function")
        if fn == "member" and node.get("member") is not None:
            lvl = (node.get("level") or "").strip("[]")
            if lvl:
                base = lvl
            members.append(_unquote(node.get("member")))
        else:
            for child in node:
                walk(child)

    walk(el)
    if base and members:
        return {"field": base, "members": members}
    return None


def _alias_members(wb: "Workbook", sheet: Sheet, field: str, members: list[str]) -> list[str]:
    src = wb.sources.get(sheet.datasource or "")
    f = src.fields.get(field) if src else None
    if f and f.aliases:
        return [f.aliases.get(m, m) for m in members]
    return members


def _unquote(v: str | None) -> str:
    v = v or ""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _tableau_text(text: str, resolve=None) -> str:
    """Texte Tableau : références de champs et sauts de ligne (Æ)."""
    if resolve is not None:
        text = _REF.sub(lambda m: resolve(m.group(1), m.group(2)), text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("Æ\n", "\n").replace("Æ", "\n")


def _runs_text(el, resolve=None) -> str:
    text = "".join(r.text or "" for r in el.findall("run"))
    if not text:
        text = "".join(el.itertext())
    text = _tableau_text(text, resolve)
    return "\n".join(" ".join(line.split()) for line in text.split("\n")).strip()


def _tooltip_text(el, resolve=None) -> str:
    parts = []
    for r in el.findall("run"):
        parts.append(_tableau_text(r.text or "", resolve))
    return "".join(parts).strip()


def _parse_runs(el, resolve=None) -> list[dict]:
    runs = []
    for r in el.findall("run"):
        text = _tableau_text(r.text or "", resolve)
        if not text:
            continue
        runs.append({"text": text, "bold": r.get("bold") == "true",
                     "italic": r.get("italic") == "true",
                     "underline": r.get("underline") == "true",
                     "size": r.get("fontsize"), "color": r.get("fontcolor"),
                     "font": r.get("fontname"), "align": r.get("fontalignment")})
    if not runs:
        text = _runs_text(el, resolve)
        if text:
            runs.append({"text": text})
    return runs


def _parse_style(table) -> dict:
    st: dict = {}
    style = table.find("style")
    if style is None:
        return st
    for rule in style.findall("style-rule"):
        element = rule.get("element")
        for enc in rule.findall("encoding"):
            if enc.get("attr") == "color":
                st["palette"] = enc.get("palette")
                st["reverse"] = enc.get("reverse") == "true"
                st["colorField"] = _zone_field(enc.get("field"))
                st["colorType"] = enc.get("type")
                st["center"] = enc.get("center")
        for fmt in rule.findall("format"):
            attr = fmt.get("attr")
            val = fmt.get("value")
            if element == "table" and attr == "background-color":
                st["background"] = val
            elif element == "cell":
                st.setdefault("cell", {})[attr] = val
            elif element in ("worksheet", "pane"):
                st.setdefault("sheet", {})[attr] = val
    return st


def _zone_field(param: str | None) -> str | None:
    if not param:
        return None
    m = re.search(r"\[([^\]]+)\]$", param)
    if not m:
        return None
    parsed = instance_parts(m.group(1))
    if parsed:
        agg, base, kind = parsed
        if agg in PART_LABEL or agg in TRUNC_LABEL:
            return dim_name(ShelfItem(ds="", agg=agg, field=base, kind=kind))
        return base
    return m.group(1).split(":")[0]


def _zone_param(param: str | None) -> str | None:
    if not param:
        return None
    m = re.search(r"\[Parameters\]\.\[([^\]]+)\]", param)
    if m:
        return m.group(1)
    m = re.search(r"\[([^\]]+)\]$", param)
    return m.group(1) if m else param


def _zone_style(z) -> dict:
    st = {}
    for fmt in z.findall("zone-style/format"):
        attr = fmt.get("attr")
        val = fmt.get("value")
        if attr and val is not None:
            st[attr] = val
    return st


def _parse_zones(container, sheet_names: set[str], resolve=None) -> list[dict]:
    zones: list[dict] = []
    seen = set()
    for z in container.iter("zone"):
        x, y = _num(z.get("x")), _num(z.get("y"))
        w, h = _num(z.get("w")), _num(z.get("h"))
        if w <= 0 or h <= 0:
            continue
        kind = z.get("type-v2") or ""
        nm = z.get("name")
        style = _zone_style(z)
        if kind in ("layout-basic", "layout-flow", "flipboard-nav", "title", "flipboard"):
            continue
        if kind in ("", "viz") and nm and nm in sheet_names:
            key = ("sheet", nm, x, y, w, h)
            if key not in seen:
                seen.add(key)
                zones.append({"kind": "sheet", "sheet": nm,
                              "show_title": z.get("show-title") != "false",
                              "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
        if kind == "filter":
            field = _zone_field(z.get("param"))
            if nm and field:
                key = ("filter", nm, field, x, y, w, h)
                if key not in seen:
                    seen.add(key)
                    label = None
                    m = _REF.search(z.get("param") or "")
                    if m and resolve is not None:
                        label = resolve(m.group(1), m.group(2))
                    zones.append({"kind": "filter", "sheet": nm, "field": field,
                                  "label": label, "mode": z.get("mode"),
                                  "values": z.get("values"),
                                  "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
        if kind == "paramctrl":
            pname = _zone_param(z.get("param"))
            if pname:
                key = ("param", pname, x, y, w, h)
                if key not in seen:
                    seen.add(key)
                    zones.append({"kind": "param", "param": pname,
                                  "mode": z.get("mode") or "input",
                                  "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
        if kind == "color":
            field = _zone_field(z.get("param"))
            if nm and field:
                key = ("legend", nm, field, x, y, w, h)
                if key not in seen:
                    seen.add(key)
                    zones.append({"kind": "legend", "sheet": nm, "field": field,
                                  "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
        if kind == "size":
            items = parse_shelf(z.get("param"))
            label = f"{items[0].agg}:{items[0].field}" if items else _zone_field(z.get("param"))
            if nm and label:
                key = ("size", nm, label, x, y, w, h)
                if key not in seen:
                    seen.add(key)
                    zones.append({"kind": "size", "sheet": nm, "field": label,
                                  "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
        if kind == "bitmap":
            url = z.get("image-file-url")
            path = z.get("image-file-path")
            path = path.replace("\\", "/").lstrip("/") if path else None
            if url or path:
                key = ("image", url or path, x, y, w, h)
                if key not in seen:
                    seen.add(key)
                    zones.append({"kind": "image", "url": url, "path": path,
                                  "scaled": z.get("is-scaled") == "1",
                                  "centered": z.get("is-centered") == "1",
                                  "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
        if kind == "web":
            url = z.get("url") or z.get("web-page-url")
            if url:
                zones.append({"kind": "web", "url": url,
                              "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
        if kind == "button":
            text = _runs_text(z.find("formatted-text"), resolve) if z.find("formatted-text") is not None else ""
            target = z.get("param") or z.get("target")
            zones.append({"kind": "button", "text": text or "Ouvrir",
                          "target": _zone_param(target) or target,
                          "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
        if kind == "empty":
            zones.append({"kind": "empty", "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
        ft = z.find("formatted-text")
        if ft is not None:
            runs = _parse_runs(ft, resolve)
            text = _runs_text(ft, resolve)
            if text:
                key = ("text", text, x, y, w, h)
                if key not in seen:
                    seen.add(key)
                    zones.append({"kind": "text", "text": text, "runs": runs,
                                  "halign": z.get("halign"), "valign": z.get("valign"),
                                  "x": x, "y": y, "w": w, "h": h, "style": style})
            continue
    return zones


def _parse_action(act) -> dict | None:
    cmd = act.find("command")
    if cmd is None:
        return None
    params = {p.get("name"): p.get("value") for p in cmd.findall("param")}
    command = (cmd.get("command") or "").lower()
    kind = None
    for k, name in (("filter", "filter"), ("highlight", "highlight"), ("goto", "goto"),
                    ("url", "url"), ("set-parameter", "parameter"), ("set_parameter", "parameter"),
                    ("set", "set")):
        if k in command:
            kind = name
            break
    if kind is None:
        return None
    src = act.find("source")
    actv = act.find("activation")
    return {
        "caption": act.get("caption"),
        "type": kind,
        "activation": (actv.get("type") if actv is not None else None) or "on-select",
        "auto_clear": bool(actv is not None and actv.get("auto-clear") == "true"),
        "source_dashboard": src.get("dashboard") if src is not None else None,
        "source_sheet": src.get("worksheet") if src is not None else None,
        "target": params.get("target"),
        "url": params.get("url") or params.get("destination"),
        "parameter": params.get("parameter") or params.get("param"),
        "value": params.get("value"),
        "fields": "all" if params.get("special-fields") == "all" else params.get("fields"),
        "field": _zone_field(params.get("field")),
    }


# --------------------------------------------------------------------------- #
# helpers généraux
# --------------------------------------------------------------------------- #
AGGS = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX",
        "cnt": "COUNT", "count": "COUNT", "ctd": "COUNT_DISTINCT",
        "countd": "COUNT_DISTINCT", "med": "MEDIAN", "median": "MEDIAN",
        "attr": "MAX", "stdev": "STDDEV_SAMP", "stdevp": "STDDEV_POP",
        "var": "VAR_SAMP", "varp": "VAR_POP", "quart1": "PERCENTILE",
        "quart3": "PERCENTILE", "none": None, "usr": "ASIS"}

# Codes de dérivation du .twb.  Les parties de date (yr, mn, dy…) sont des
# valeurs discrètes ; les troncatures (tyr, tdy…) sont des dates continues.
PART_UNITS = {"yr": "year", "qr": "quarter", "mn": "month", "dy": "day",
              "wk": "week", "wd": "dow", "hr": "hour", "mi": "minute",
              "sc": "second", "weekday": "dow"}
PART_LABEL = {"yr": "year", "qr": "quarter", "mn": "month", "dy": "day",
              "wk": "week", "wd": "weekday", "hr": "hour", "mi": "minute",
              "sc": "second", "weekday": "weekday"}
TRUNC_AGG = {"tyr": "year", "tqr": "quarter", "tmn": "month", "twk": "week",
             "tdy": "day", "thr": "hour", "tmi": "minute", "tsc": "second",
             "md": "day", "mdy": "day", "my": "month", "month-year": "month",
             "trunc-year": "year", "trunc-qtr": "quarter", "trunc-month": "month",
             "trunc-week": "week", "trunc-day": "day", "trunc-hour": "hour",
             "trunc-minute": "minute", "trunc-second": "second"}
TRUNC_LABEL = {"tyr": "year trunc", "tqr": "quarter trunc", "tmn": "month trunc",
               "twk": "week trunc", "tdy": "day trunc", "thr": "hour trunc",
               "tmi": "minute trunc", "tsc": "second trunc", "md": "date",
               "mdy": "date", "my": "month-year", "month-year": "month-year",
               "trunc-year": "year trunc", "trunc-qtr": "quarter trunc",
               "trunc-month": "month trunc", "trunc-week": "week trunc",
               "trunc-day": "day trunc", "trunc-hour": "hour trunc",
               "trunc-minute": "minute trunc", "trunc-second": "second trunc"}
DATE_PART = {label: PART_UNITS[agg] for agg, label in PART_LABEL.items()}
DATE_TRUNC = {label: TRUNC_AGG[agg] for agg, label in TRUNC_LABEL.items()}
TRUNC_UNITS = set(DATE_PART) | set(DATE_TRUNC)
_TRUNC_NAME = re.compile(r"^(.+?) \((" + "|".join(
    re.escape(x) for x in sorted(TRUNC_UNITS, key=len, reverse=True)) + r")\)$")


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _direction(value, default: str = "ASC") -> str:
    value = str(value or default).strip().upper()
    return value if value in ("ASC", "DESC") else default


_ALLOWED_FILTER_KEYS = {"members", "exclude", "none", "range", "relative",
                        "wildcard", "top", "class", "nonnull", "mode", "values"}


def _validate_sheet_inputs(params, filters, drill) -> None:
    if params is not None:
        if not isinstance(params, dict):
            raise TWBXError("paramètres invalides")
        for key, value in params.items():
            if not isinstance(key, str) or isinstance(value, (dict, list, tuple)):
                raise TWBXError("paramètres invalides")
    if filters is not None:
        if not isinstance(filters, dict):
            raise TWBXError("filtres invalides")
        for key, value in filters.items():
            if not isinstance(key, str):
                raise TWBXError("filtres invalides")
            if isinstance(value, dict):
                for k, v in value.items():
                    if k not in _ALLOWED_FILTER_KEYS:
                        raise TWBXError(f"filtre non supporté : {k}")
                    if k in ("members", "exclude") and (
                            not isinstance(v, (list, tuple)) or len(v) > 5000):
                        raise TWBXError("filtre trop volumineux")
                    if k in ("range", "relative", "wildcard", "top") and not isinstance(v, dict):
                        raise TWBXError("filtre invalide")
                top = value.get("top")
                if top is not None:
                    if not isinstance(top, dict):
                        raise TWBXError("filtre top invalide")
                    if "n" in top:
                        try:
                            int(top["n"])
                        except (TypeError, ValueError):
                            raise TWBXError("filtre top invalide") from None
                    if "direction" in top and _direction(top["direction"], "") not in ("ASC", "DESC"):
                        raise TWBXError("direction de filtre invalide")
            elif not isinstance(value, (list, tuple)) or len(value) > 5000:
                raise TWBXError("filtre invalide")
    if drill is not None:
        if (not isinstance(drill, list) or len(drill) > 50
                or not all(isinstance(x, str) for x in drill)):
            raise TWBXError("drill invalide")


def _base_field(name: str) -> str:
    m = _TRUNC_NAME.match(name)
    return m.group(1) if m else name


_REF = re.compile(r"\[([^\]]+)\]\.\[([^\]]+)\]")
_TITLE_REF = re.compile(r"<\[([^\]]+)\]\.\[([^\]]+)\]>")
_AGG_CALL = re.compile(
    r"\b(SUM|AVG|MIN|MAX|COUNTD|COUNT|MEDIAN|ATTR|STDEVP|STDEV|VARP|VAR)"
    r"\s*\(\s*\[([^\]]+)\]\s*\)", re.I)
_AGG_KEY = {"SUM": "sum", "AVG": "avg", "MIN": "min", "MAX": "max",
            "COUNT": "cnt", "COUNTD": "ctd", "MEDIAN": "med", "ATTR": "attr",
            "STDEV": "stdev", "STDEVP": "stdevp", "VAR": "var", "VARP": "varp"}


def _agg_refs(formula: str) -> list[str]:
    """Mesures agrégées citées par une formule de calcul de table."""
    out = []
    for fn, field in _AGG_CALL.findall(formula or ""):
        label = f"{_AGG_KEY[fn.upper()]}:{field}"
        if label not in out:
            out.append(label)
    return out


def instance_parts(inner: str) -> tuple[str, str, str] | None:
    """Décompose `sum:champ:qk` ou `pcto:sum:champ:qk:2` en (agrégat, champ, type)."""
    parts = inner.split(":")
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    if len(parts) < 3:
        return None
    if len(parts) >= 4:
        return parts[-3], parts[-2], parts[-1]
    return parts[0], ":".join(parts[1:-1]), parts[-1]


def _tooltip_items(text: str | None) -> list[ShelfItem]:
    """Champs cités dans une infobulle personnalisée, pour les rendre disponibles."""
    out = []
    for ds, inner in _REF.findall(text or ""):
        if ds in ("Parameters", "Paramètres"):
            continue
        parsed = instance_parts(inner)
        if parsed:
            agg, field, kind = parsed
            out.append(ShelfItem(ds=ds, agg=agg, field=field, kind=kind))
    return out


def parse_shelf(text: str | None) -> list[ShelfItem]:
    out = []
    for ds, inner in _REF.findall(text or ""):
        parsed = instance_parts(inner)
        if parsed:
            agg, field, kind = parsed
            out.append(ShelfItem(ds=ds, agg=agg, field=field, kind=kind))
    return out


def split_args_text(text: str) -> list[str]:
    out, depth, cur, quote_ch = [], 0, "", None
    for ch in text:
        if quote_ch:
            cur += ch
            if ch == quote_ch:
                quote_ch = None
            continue
        if ch in "\"'":
            quote_ch = ch
            cur += ch
        elif ch in "([":
            depth += 1
            cur += ch
        elif ch in ")]":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip() or out:
        out.append(cur.strip())
    return out


def parse_filter(el) -> dict:
    m = _REF.search(el.get("column") or "")
    field = None
    measure_names: list[str] = []
    if m:
        parsed = instance_parts(m.group(2))
        if parsed:
            agg, base, kind = parsed
            if agg in PART_LABEL or agg in TRUNC_LABEL:
                field = dim_name(ShelfItem(ds=m.group(1), agg=agg, field=base, kind=kind))
            else:
                field = base
        elif len(m.group(2).split(":")) == 1:
            field = m.group(2)
        elif m.group(2).startswith(":Measure Names"):
            measure_names = []
    if field and (field.startswith("Action (") or field == ":Measure Names"):
        field = None
    cls = el.get("class")
    out = {"field": field, "class": cls, "members": [], "exclude": [],
           "nonnull": el.get("included-values") == "non-null",
           "range": None, "top": None, "relative": None, "wildcard": None,
           "context": cls == "context", "preset": None,
           "expression": None, "measure_names": measure_names}

    preset = el.find("preset")
    if preset is not None:
        out["preset"] = preset.get("type")

    rng = el.find("range")
    if rng is not None:
        out["range"] = {"min": rng.get("min"), "max": rng.get("max")}
    mn, mx = el.find("min"), el.find("max")
    if mn is not None or mx is not None:
        out["range"] = {"min": (mn.text or "").strip() if mn is not None else None,
                        "max": (mx.text or "").strip() if mx is not None else None}

    rel = el.find("relative-date")
    if rel is not None:
        out["relative"] = {"period": rel.get("period"), "value": rel.get("value"),
                           "anchor": rel.get("anchor")}
    elif cls == "relative-date":
        out["relative"] = {"period": el.get("period"), "value": el.get("value"),
                           "anchor": el.get("anchor")}

    wc = el.find("wildcard")
    if wc is not None:
        out["wildcard"] = {"match": wc.get("match"), "exclude": wc.get("exclude") == "true"}

    is_measure_names = bool(m and m.group(2).startswith(":Measure Names"))
    for g in el.iter("groupfilter"):
        fn = g.get("function")
        if fn == "member" and g.get("member") is not None:
            member = _unquote(g.get("member"))
            if is_measure_names:
                ref = _REF.search(member)
                if ref:
                    bits = ref.group(2).split(":")
                    if len(bits) >= 2:
                        out["measure_names"].append(":".join(bits[:-1]))
                continue
            in_except = any(a.get("function") == "except" for a in g.iterancestors())
            (out["exclude"] if in_except else out["members"]).append(member)
        elif fn == "filter" and g.get("expression"):
            out["expression"] = g.get("expression")
        elif fn == "range":
            out["range"] = {"min": g.get("from"), "max": g.get("to")}
        elif fn == "top":
            out["top"] = {"n": _num(g.get("count")), "by": _zone_field(g.get("by")),
                          "direction": _direction(g.get("direction"), "DESC")}
        elif fn == "wildcard":
            out["wildcard"] = {"match": g.get("match"), "exclude": g.get("exclude") == "true"}
    return out


def _join_conditions(el) -> list[tuple[str, str]]:
    op = el.get("op")
    if op == "AND":
        out = []
        for child in el:
            out.extend(_join_conditions(child))
        return out
    if op == "=":
        kids = list(el)
        if len(kids) == 2:
            return [(kids[0].get("op") or "", kids[1].get("op") or "")]
    return []


def _literal(value, datatype: str) -> str:
    v = str(value).strip('"')
    if datatype in ("real", "integer"):
        try:
            float(v)
            return v
        except ValueError:
            pass
    if datatype == "boolean":
        return "TRUE" if v.lower() in ("true", "1") else "FALSE"
    if datatype in ("date", "datetime"):
        v = v.strip("#")
        if re.fullmatch(r"\d{8}", v):
            v = f"{v[:4]}-{v[4:6]}-{v[6:]}"
    return "'" + v.replace("'", "''") + "'"


def _str_literal(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


_SET_IN = re.compile(r"\[([^\]]+)\]\s+(NOT\s+)?IN\s+\[([^\]]+)\]", re.I)


def _rewrite_sets(formula: str, sets: dict[str, dict]) -> str:
    """`[champ] IN [Ensemble]` -> `[Ensemble]` (le set porte déjà l'appartenance)."""
    def repl(m):
        neg, name = m.group(2), m.group(3)
        if name in sets:
            return f"NOT [{name}]" if neg else f"[{name}]"
        return m.group(0)
    return _SET_IN.sub(repl, formula)


def _datatype_of(src: Source, name: str) -> str:
    f = src.fields.get(name)
    return f.datatype if f else "string"


def filter_sql(f: dict, lower: dict[str, str], fields: dict[str, Field],
               sql_for=None, is_agg=None, from_sql: str | None = None,
               params: dict | None = None) -> str | None:
    name = f.get("field")
    if not name:
        return None
    suffix = None
    m = _TRUNC_NAME.match(name)
    if m:
        name, suffix = m.group(1), m.group(2)
    c = lower.get(name.lower())
    if c:
        expr = c
    elif sql_for is not None:
        try:
            if is_agg is not None and is_agg(name):
                return None
            expr = sql_for(name)[0]
        except CalcError:
            return None
    else:
        return None
    part = suffix in DATE_PART
    if part:
        col = f"date_part('{DATE_PART[suffix]}', {expr})"
    elif suffix in DATE_TRUNC:
        col = f"date_trunc('{DATE_TRUNC[suffix]}', {expr})"
    else:
        col = expr
    dt = (fields.get(name).datatype if name in fields else "string")
    parts = []
    if f.get("none"):
        parts.append("FALSE")
    if f.get("members"):
        parts.append(f"{col} IN ({', '.join(_member_literal(v, dt, suffix, part) for v in f['members'])})")
    if f.get("exclude"):
        parts.append(f"{col} NOT IN ({', '.join(_member_literal(v, dt, suffix, part) for v in f['exclude'])})")
    if f.get("nonnull"):
        parts.append(f"{expr} IS NOT NULL")
    rng = f.get("range")
    if rng and (rng.get("min") is not None or rng.get("max") is not None):
        lo, hi = rng.get("min"), rng.get("max")
        if lo is not None:
            parts.append(f"{col} >= {_member_literal(lo, dt, suffix, part)}")
        if hi is not None:
            parts.append(f"{col} <= {_member_literal(hi, dt, suffix, part)}")
    rel = f.get("relative")
    if rel and rel.get("period"):
        unit = str(rel["period"]).lower()
        try:
            n = int(rel.get("value") or 0)
        except ValueError:
            n = 0
        if unit in ("year", "quarter", "month", "week", "day"):
            start = f"date_trunc('{unit}', current_date)"
            if n:
                start = f"({start} + ({n}) * INTERVAL '1' {unit.upper()})"
            parts.append(f"{expr} >= {start}")
    wc = f.get("wildcard")
    if wc and wc.get("match"):
        pat = str(wc["match"]).replace("*", "%").replace("?", "_")
        parts.append(f"{expr} {'NOT ' if wc.get('exclude') else ''}LIKE {_str_literal(pat)}")
    # filtre par formule (ex. date([JOUR]) > [Parameters].[…]) : traduit en SQL
    formula = f.get("expression")
    if formula and sql_for is not None and not has_aggregate(formula):
        try:
            parts.append(f"({translate(formula, sql_for, params or {})})")
        except CalcError:
            pass
    top = f.get("top")
    if top and top.get("n") and from_sql:
        by = top.get("by")
        order = "1"
        if by and sql_for is not None:
            agg, _, fname = by.partition(":")
            try:
                bexpr = sql_for(fname or by)[0]
                fn = AGGS.get(agg) if fname else None
                order = (f"{fn}({bexpr})" if fn and fn != "ASIS" else bexpr)
            except CalcError:
                order = "1"
        direction = _direction(top.get("direction"), "DESC")
        parts.append(
            f"{col} IN (SELECT {col} FROM {from_sql} WHERE {col} IS NOT NULL "
            f"GROUP BY {col} ORDER BY {order} {direction} LIMIT {int(top['n'])})")
    return " AND ".join(parts) or None


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "janvier": 1, "février": 2, "fevrier": 2, "mars": 3,
    "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
_WEEKDAYS = {
    "sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "dimanche": 0, "lundi": 1, "mardi": 2,
    "mercredi": 3, "jeudi": 4, "vendredi": 5, "samedi": 6,
}


def _part_literal(value, unit: str) -> str:
    v = str(value).strip().strip('"').strip("'")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", v):
        return v
    low = v.lower()
    if unit == "month" and low in _MONTHS:
        return str(_MONTHS[low])
    if unit == "dow" and low in _WEEKDAYS:
        return str(_WEEKDAYS[low])
    return _str_literal(v)


def _member_literal(value: str, datatype: str, trunc: str | None,
                    part: bool = False) -> str:
    if part:
        return _part_literal(value, DATE_PART.get(trunc or "", ""))
    lit = _literal(value, datatype)
    if trunc or datatype in ("date", "datetime"):
        return "CAST(" + lit.replace("T", " ") + " AS TIMESTAMP)"
    return lit


def quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def dim_name(d: ShelfItem) -> str:
    label = PART_LABEL.get(d.agg) or TRUNC_LABEL.get(d.agg)
    if label:
        return f"{d.field} ({label})"
    return d.field


def _dedupe(items: list[ShelfItem]) -> list[ShelfItem]:
    seen, out = set(), []
    for it in items:
        key = (it.agg, it.field)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _parse_format(fmt: str | None) -> dict | None:
    if not fmt:
        return None
    f = fmt.strip()
    if f.startswith("p"):
        dec = len(f.split(".")[1].rstrip("%")) if "." in f else 0
        return {"kind": "percent", "decimals": dec}
    if f.startswith("c"):
        dec = len(f.split(".")[1]) if "." in f else 2
        return {"kind": "currency", "decimals": dec}
    if f.startswith("n") or (f[:1].isdigit()) or f[:1] in "#$":
        dec = len(f.split(".")[1]) if "." in f else 0
        return {"kind": "number", "decimals": dec}
    if f.startswith("*") or "/" in f or "Y" in f or "y" in f:
        return {"kind": "date"}
    return None


def _is_spatial(src: Source, name: str) -> bool:
    f = src.fields.get(name)
    return f is not None and f.datatype == "spatial"


def _field_type(f: Field | None) -> str:
    if f is None:
        return "any"
    if f.datatype == "string":
        return "str"
    if f.datatype == "boolean":
        return "bool"
    if f.datatype in ("date", "datetime"):
        return "any"
    return "num"


def _param_literal(value: str | None, datatype: str | None) -> str:
    if value is None:
        return "NULL"
    v = str(value).strip()
    if datatype in ("date", "datetime"):
        v = v.strip("#").replace("T", " ")
        return "CAST('" + v.replace("'", "''") + "' AS TIMESTAMP)"
    if datatype in ("integer", "real"):
        try:
            float(v)
            return v
        except ValueError:
            return "NULL"
    if datatype == "boolean":
        return "TRUE" if v.lower() in ("true", "1") else "FALSE"
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return "'" + v.replace("'", "''") + "'"


def _num(v):
    try:
        return int(v) if v is not None else 0
    except ValueError:
        return 0


def _numf(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _jsonable(v):
    if isinstance(v, (_dt.date, _dt.datetime, _dt.time)):
        return v.isoformat()
    if isinstance(v, _decimal.Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, _dt.timedelta):
        return v.total_seconds()
    if type(v).__name__ in ("Date", "Timestamp", "Time", "Interval", "Numeric"):
        return str(v)
    return v


# --------------------------------------------------------------------------- #
# géographie
# --------------------------------------------------------------------------- #
_WKT_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _wkt_nums(buf: str) -> list[float]:
    return [float(x) for x in _WKT_NUM.findall(buf)]


def _wkt_group(s: str, i: int = 0):
    out, buf = [], ""
    i += 1
    while i < len(s):
        ch = s[i]
        if ch == "(":
            sub, i = _wkt_group(s, i)
            out.append(sub)
            continue
        if ch == ")":
            if buf.strip():
                out.append(_wkt_nums(buf))
            return out, i + 1
        if ch == ",":
            if buf.strip():
                out.append(_wkt_nums(buf))
            buf = ""
        else:
            buf += ch
        i += 1
    return out, i


def _wkt_to_geojson(wkt) -> dict | None:
    if not isinstance(wkt, str) or "(" not in wkt:
        return None
    head, _, body = wkt.partition("(")
    kind = head.strip().upper().split(" ")[0]
    tree, _ = _wkt_group("(" + body)
    if not tree:
        return None
    if kind == "POINT":
        return {"type": "Point", "coordinates": tree[0]}
    if kind == "MULTIPOINT":
        pts = [p[0] if len(p) == 1 and isinstance(p[0], list) else p for p in tree]
        return {"type": "MultiPoint", "coordinates": pts}
    if kind in ("LINESTRING", "MULTILINESTRING", "POLYGON", "MULTIPOLYGON"):
        gtype = {"LINESTRING": "LineString", "MULTILINESTRING": "MultiLineString",
                 "POLYGON": "Polygon", "MULTIPOLYGON": "MultiPolygon"}[kind]
        return {"type": gtype, "coordinates": tree}
    return None


def map_payload(s: Sheet, data: list[dict], names: list[str], labels: dict,
                formats: dict | None = None, style: dict | None = None) -> dict | None:
    geo = next((i for i in s.encodings.get("geometry", [])), None)
    if geo is None or geo.field not in names:
        return None

    color = next((i for i in s.encodings.get("color", [])), None)
    ckey = None
    if color:
        for cand in (f"{color.agg}:{color.field}", color.field):
            if cand in names:
                ckey = cand
                break

    size = next((i for i in s.encodings.get("size", [])), None)
    skey = None
    if size:
        for cand in (f"{size.agg}:{size.field}", size.field):
            if cand in names:
                skey = cand
                break

    details = []
    for i in (s.encodings.get("lod", []) + s.rows + s.cols):
        if i.field == geo.field:
            continue
        key = (dim_name(i) if (i.agg in PART_LABEL or i.agg in TRUNC_LABEL)
               else (f"{i.agg}:{i.field}" if f"{i.agg}:{i.field}" in names else i.field))
        if key in names:
            details.append(key)
    details = list(dict.fromkeys(details))

    features, values, sizes = [], [], []
    for row in data[:MAP_MAX]:
        geom = _wkt_to_geojson(row.get(geo.field))
        if geom is None:
            continue
        props = {n: row.get(n) for n in names if n != geo.field}
        v = row.get(ckey) if ckey else None
        if ckey:
            props["__value"] = v
            props[ckey] = v
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values.append(v)
        if skey:
            sv = row.get(skey)
            props["__size"] = sv
            props[skey] = sv
            if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                sizes.append(sv)
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    if not features:
        return {"kind": "notice",
                "message": "Aucune géométrie à afficher (extrait vide ou filtres trop restrictifs)."}

    sample = [r.get(ckey) for r in data if ckey and r.get(ckey) is not None]
    categorical = bool(sample) and not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in sample)
    return {
        "kind": "map",
        "features": features,
        "valueField": "__value" if ckey else None,
        "valueLabel": labels.get(ckey, ckey) if ckey else None,
        "valueFormat": (formats or {}).get(ckey) if ckey else None,
        "categorical": categorical,
        "categories": sorted({str(v) for v in sample}) if categorical else None,
        "details": details,
        "tooltip": s.tooltip,
        "sizeField": "__size" if skey else None,
        "sizeLabel": labels.get(skey, skey) if skey else None,
        "sizeMin": min(sizes) if sizes else None,
        "sizeMax": max(sizes) if sizes else None,
        "palette": (style or {}).get("palette"),
        "reverse": bool((style or {}).get("reverse")),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "diverging": bool(values) and min(values) < 0 < max(values)
                     and sum(1 for v in values if v < 0) / len(values) >= 0.15,
        "truncated": len(data) > MAP_MAX,
    }


# --------------------------------------------------------------------------- #
# Vega-Lite
# --------------------------------------------------------------------------- #
PALETTES = {
    "tableau10": ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
                  "#EDC948", "#B07AA1", "#FF9DA7", "#9D7660", "#BAB0AC"],
    "tableau20": ["#4E79A7", "#A0CBE8", "#F28E2B", "#FFBE7D", "#59A14F", "#8CD17D",
                  "#B6992D", "#F1CE63", "#499894", "#86BCB6", "#E15759", "#FF9D9A",
                  "#79706E", "#BAB0AC", "#D37295", "#FABFD2", "#B07AA1", "#D4A6C8",
                  "#9D7660", "#D7B5A6"],
    "colorblind10": ["#006BA4", "#FF800E", "#ABABAB", "#595959", "#5F9ED1",
                     "#C85200", "#898989", "#A2C8EC", "#FFBC79", "#CFCFCF"],
}

DIVERGING = {
    "orange_blue_white_diverging_10_0": ["#b35806", "#e08214", "#fdb863", "#fee0b6",
                                         "#f7f7f7", "#d8daeb", "#b2abd2", "#8073ac",
                                         "#542788", "#2d004b"],
    "red_blue_diverging_10_0": ["#b2182b", "#d6604d", "#f4a582", "#fddbc7", "#f7f7f7",
                                "#d1e5f0", "#92c5de", "#4393c3", "#2166ac", "#053061"],
    "blue_white_teal": ["#08306b", "#4292c6", "#9ecae1", "#deebf7", "#f7f7f7",
                        "#d9f0d3", "#7fcdbb", "#41b6c4", "#1d91c0", "#225ea8"],
}

SEQ = ["#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#08306b"]


def palette_colors(name: str | None, reverse: bool = False) -> list[str]:
    if not name:
        return PALETTES["tableau10"]
    base = name.split("_diverging")[0]
    if name in DIVERGING:
        cols = DIVERGING[name]
    elif base in PALETTES:
        cols = PALETTES[base]
    elif "diverging" in name:
        cols = DIVERGING["red_blue_diverging_10_0"]
    else:
        cols = PALETTES["tableau10"]
    return list(reversed(cols)) if reverse else cols


def _short_label(text: str | None, n: int = 48) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _type_of(name: str, data: list[dict]) -> str:
    for row in data:
        v = row.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return "quantitative"
        if isinstance(v, str):
            return "temporal" if re.match(r"^\d{4}-\d{2}-\d{2}", v) else "nominal"
    return "nominal"


def _dim_type(name: str, data: list[dict]) -> str:
    """Une partie de date est discrète ; une troncature de date est continue."""
    m = _TRUNC_NAME.match(name)
    if m:
        suffix = m.group(2)
        if suffix in DATE_PART:
            return "nominal"
        if suffix in DATE_TRUNC:
            return "ordinal" if suffix in ("date", "month-year") else "temporal"
    return _type_of(name, data)


def _part_axis_expr(name: str) -> str | None:
    m = _TRUNC_NAME.match(name)
    if not m:
        return None
    suffix = m.group(2)
    if suffix == "month":
        return ("['Janvier','Février','Mars','Avril','Mai','Juin','Juillet',"
                "'Août','Septembre','Octobre','Novembre','Décembre'][datum.value-1]")
    if suffix == "weekday":
        return ("['Dimanche','Lundi','Mardi','Mercredi','Jeudi','Vendredi',"
                "'Samedi'][datum.value]")
    if suffix == "quarter":
        return "'T' + datum.value"
    if suffix == "hour":
        return "datum.value + ' h'"
    return None


MARK_MAP = {"bar": "bar", "line": "line", "area": "area", "circle": "point",
            "shape": "point", "square": "rect", "heatmap": "rect",
            "ganttbar": "bar", "gantt-bar": "bar", "polygon": "area",
            "automatic": "bar", "text": "text"}


def vega_spec(s: Sheet, dims, measures, names, data, refs=None, labels=None,
              formats=None, style=None, bands=None) -> dict | None:
    labels = labels or {}
    style = style or {}
    dnames = [dim_name(d) for d in dims if dim_name(d) in names]
    mnames = [f"{m.agg}:{m.field}" for m in measures if f"{m.agg}:{m.field}" in names]

    def qformat(name: str) -> str | None:
        return _value_format(name, formats, data)

    if not data or not names:
        return None
    mark = s.mark.lower()
    if mark == "text" or not dnames or not mnames:
        return None

    colors = palette_colors(style.get("palette"), style.get("reverse", False))
    dtype = _dim_type(dnames[0], data)
    if dtype == "nominal" and len({str(r.get(dnames[0])) for r in data}) > 500:
        return None          # au-delà de 500 catégories, un tableau reste lisible
    axis_expr = _part_axis_expr(dnames[0])
    axis = {"labelAngle": 0 if dtype == "nominal" else -45,
            "labelLimit": 200, "labelFontSize": 10}
    if axis_expr:
        axis["labelExpr"] = axis_expr
    enc_dim = {"field": dnames[0], "type": dtype,
               "title": _short_label(labels.get(dnames[0], dnames[0]), 60),
               "axis": axis}
    on_rows = any(dim_name(d) == dnames[0] for d in s.rows)
    if on_rows:
        axis = {"labelLimit": 200}
        if axis_expr:
            axis["labelExpr"] = axis_expr
        enc_dim = {"field": dnames[0], "type": dtype,
                   "title": _short_label(labels.get(dnames[0], dnames[0]), 60),
                   "axis": axis}

    # camembert
    if mark == "pie":
        return {"data": {"values": data},
                "mark": {"type": "arc", "tooltip": True},
                "encoding": {"theta": {"field": mnames[0], "type": "quantitative"},
                             "color": {"field": dnames[0], "type": "nominal",
                                       "scale": {"range": colors}},
                             "tooltip": _tooltip_enc(dnames, mnames, labels, formats, data)}}

    # tableau croisé de chaleur
    if len(dnames) >= 2 and mark in ("square", "heatmap", "rect"):
        ex, ey = _part_axis_expr(dnames[0]), _part_axis_expr(dnames[1])
        enc = {"x": {"field": dnames[0], "type": _dim_type(dnames[0], data),
                     "title": _short_label(labels.get(dnames[0], dnames[0]), 40)},
               "y": {"field": dnames[1], "type": _dim_type(dnames[1], data),
                     "title": _short_label(labels.get(dnames[1], dnames[1]), 40)},
               "color": {"field": mnames[0], "type": "quantitative",
                         "scale": {"range": SEQ}},
               "tooltip": _tooltip_enc(dnames, mnames, labels, formats, data)}
        if qformat(mnames[0]):
            enc["color"]["legend"] = {"format": qformat(mnames[0])}
        if ex:
            enc["x"]["axis"] = {"labelExpr": ex}
        if ey:
            enc["y"]["axis"] = {"labelExpr": ey}
        return {"data": {"values": data}, "mark": {"type": "rect", "tooltip": True},
                "encoding": enc}

    # barres groupées (2 dimensions)
    if len(dnames) >= 2 and len(mnames) == 1 and mark == "bar":
        enc = {"x": {"field": dnames[0], "type": _dim_type(dnames[0], data),
                     "title": _short_label(labels.get(dnames[0], dnames[0]), 40)},
               "xOffset": {"field": dnames[1], "type": "nominal"},
               "y": {"field": mnames[0], "type": "quantitative",
                     "title": _short_label(labels.get(mnames[0], mnames[0]), 48),
                     "axis": {"format": qformat(mnames[0])}},
               "color": {"field": dnames[1], "type": "nominal",
                         "scale": {"range": colors}},
               "tooltip": _tooltip_enc(dnames, mnames, labels, formats, data)}
        ex = _part_axis_expr(dnames[0])
        if ex:
            enc["x"]["axis"] = {"labelExpr": ex}
        return {"data": {"values": data}, "mark": {"type": "bar", "tooltip": True},
                "encoding": enc}

    # plusieurs mesures sur le même axe : une série par mesure (sauf double axe)
    if len(mnames) >= 2 and len(dnames) == 1:
        if s.dual:
            first, second = mnames[0], mnames[1]
            enc0 = {"x": enc_dim,
                    "y": {"field": first, "type": "quantitative",
                          "title": _short_label(labels.get(first, first), 40),
                          "axis": {"titleColor": colors[0], "format": qformat(first)}},
                    "tooltip": _tooltip_enc(dnames, mnames, labels, formats, data)}
            enc1 = {"y": {"field": second, "type": "quantitative",
                          "title": _short_label(labels.get(second, second), 40),
                          "axis": {"orient": "right", "titleColor": colors[1],
                                   "format": qformat(second)}}}
            return {"data": {"values": data},
                    "encoding": {"x": enc_dim},
                    "layer": [
                        {"mark": {"type": "bar", "color": colors[0], "tooltip": True},
                         "encoding": enc0},
                        {"mark": {"type": "line", "color": colors[1], "point": True,
                                  "strokeWidth": 2.5},
                         "encoding": enc1},
                    ],
                    "resolve": {"scale": {"y": "independent"}}}
        values = []
        for row in data:
            for m in mnames:
                values.append({**row, "__measure": labels.get(m, m),
                               "__value": row.get(m)})
        series_mark = ("line" if (dtype == "temporal" or mark == "line")
                       else "area" if mark == "area" else "bar")
        vfmt = _value_format("__value", None, values)
        enc = {"x": enc_dim,
               "y": {"field": "__value", "type": "quantitative", "title": "Valeur",
                     "axis": {"format": vfmt}},
               "color": {"field": "__measure", "type": "nominal", "title": "Mesure",
                         "scale": {"range": colors}},
               "tooltip": [
                   {"field": dnames[0], "type": dtype,
                    "title": _short_label(labels.get(dnames[0], dnames[0]), 40)},
                   {"field": "__measure", "type": "nominal", "title": "Mesure"},
                   {"field": "__value", "type": "quantitative", "title": "Valeur",
                    "format": vfmt}]}
        return {"data": {"values": values},
                "mark": {"type": series_mark, "tooltip": True},
                "encoding": enc}

    # gantt (début / fin)
    if mark in ("ganttbar", "gantt-bar") and len(mnames) >= 2:
        enc = {"y": enc_dim,
               "x": {"field": mnames[0], "type": "temporal", "title": "Début"},
               "x2": {"field": mnames[1]},
               "color": {"field": mnames[2] if len(mnames) > 2 else mnames[0],
                         "type": "nominal"},
               "tooltip": _tooltip_enc(dnames, mnames, labels, formats, data)}
        return {"data": {"values": data}, "mark": {"type": "bar", "tooltip": True},
                "encoding": enc}

    meas = mnames[0]
    kind = ("line" if (mark == "automatic" and dtype == "temporal")
            else MARK_MAP.get(mark, "bar"))
    enc = {"tooltip": _tooltip_enc(dnames, mnames, labels, formats, data)}
    if on_rows:
        enc["y"] = enc_dim
        enc["x"] = {"field": meas, "type": "quantitative",
                    "title": _short_label(labels.get(meas, meas), 48),
                    "axis": {"format": qformat(meas)}}
    else:
        enc["x"] = enc_dim
        enc["y"] = {"field": meas, "type": "quantitative",
                    "title": _short_label(labels.get(meas, meas), 48),
                    "axis": {"format": qformat(meas)}}

    color_items = [c for c in s.encodings.get("color", [])
                   if f"{c.agg}:{c.field}" in names or c.field in names]
    if color_items:
        c = color_items[0]
        ckey = f"{c.agg}:{c.field}" if f"{c.agg}:{c.field}" in names else c.field
        ctype = _dim_type(ckey, data)
        enc["color"] = {"field": ckey, "type": ctype,
                        "title": _short_label(labels.get(ckey, ckey), 40)}
        if ctype == "quantitative":
            enc["color"]["scale"] = {"range": SEQ}
        else:
            enc["color"]["scale"] = {"range": colors}
        ex = _part_axis_expr(ckey)
        legend = {}
        if ex:
            legend["labelExpr"] = ex
        if ctype == "quantitative" and qformat(ckey):
            legend["format"] = qformat(ckey)
        if legend:
            enc["color"]["legend"] = legend
    elif kind == "rect":
        enc["color"] = {"field": meas, "type": "quantitative", "scale": {"range": SEQ}}

    size_items = [c for c in s.encodings.get("size", [])
                  if f"{c.agg}:{c.field}" in names or c.field in names]
    if size_items:
        c = size_items[0]
        skey = f"{c.agg}:{c.field}" if f"{c.agg}:{c.field}" in names else c.field
        enc["size"] = {"field": skey, "type": "quantitative",
                       "title": _short_label(labels.get(skey, skey), 40),
                       "scale": {"range": [30, 900]}}
        if qformat(skey):
            enc["size"]["legend"] = {"format": qformat(skey)}

    base = {"data": {"values": data}, "mark": {"type": kind, "tooltip": True},
            "encoding": enc}

    layers = []
    if s.labels:
        text_enc = {"text": {"field": meas, "type": "quantitative"}}
        tf = qformat(meas)
        if tf:
            text_enc["text"]["format"] = tf
        for ch in ("x", "y"):
            if ch in enc:
                text_enc[ch] = enc[ch]
        layers.append({"mark": {"type": "text", "fontSize": 10, "color": "#333",
                                "dx": 6 if on_rows else 0, "dy": 0 if on_rows else -6,
                                "align": "left" if on_rows else "center"},
                       "encoding": text_enc})
    if bands:
        for b in bands:
            if b["axis"] != meas:
                continue
            ch = "x" if on_rows else "y"
            layers.append({"mark": {"type": "rect", "color": "#9aa3ad", "opacity": 0.18},
                           "encoding": {ch: {"datum": b["low"]},
                                        f"{ch}2": {"datum": b["high"]}}})
    if refs:
        for r in refs:
            if r["axis"] != meas:
                continue
            ch = "x" if on_rows else "y"
            layers.append({"mark": {"type": "rule", "color": "#8a8a8a",
                                    "strokeDash": [4, 3], "size": 1},
                           "encoding": {ch: {"datum": r["value"]}}})
    if s.trend and dtype != "nominal":
        layers.append({"mark": {"type": "line", "color": "#E15759", "strokeWidth": 2},
                       "transform": [{"regression": meas, "on": dnames[0]}],
                       "encoding": {"x": enc.get("x"), "y": {"field": meas, "type": "quantitative"}}})

    if not layers:
        return base
    return {"data": {"values": data},
            "layer": [{"mark": base["mark"], "encoding": enc}] + layers}


def _squarify(values, x, y, w, h):
    items = sorted(values, key=lambda kv: kv[1], reverse=True)
    total = sum(v for _, v in items)
    if total <= 0:
        items = [(k, 1.0) for k, _ in items]
        total = len(items) or 1
    scale = (w * h) / total if w > 0 and h > 0 else 0
    items = [(k, v * scale) for k, v in items]
    out = []

    def worst(row, side):
        s = sum(v for _, v in row)
        if s <= 0 or side <= 0:
            return float("inf")
        mx = max(v for _, v in row)
        mn = min(v for _, v in row)
        if mn <= 0:
            return float("inf")
        return max((side * side * mx) / (s * s), (s * s) / (side * side * mn))

    while items and w > 0 and h > 0:
        horizontal = w >= h
        side = w if horizontal else h
        row = [items[0]]
        while len(row) < len(items):
            nxt = items[len(row)]
            if worst(row + [nxt], side) <= worst(row, side):
                row.append(nxt)
            else:
                break
        items = items[len(row):]
        s = sum(v for _, v in row)
        if horizontal:
            strip = s / w if w else 0
            cx = x
            for key, v in row:
                rw = (v / s) * w if s else 0
                out.append((key, cx, y, rw, strip))
                cx += rw
            y += strip
            h -= strip
        else:
            strip = s / h if h else 0
            cy = y
            for key, v in row:
                rh = (v / s) * h if s else 0
                out.append((key, x, cy, strip, rh))
                cy += rh
            x += strip
            w -= strip
    return out


def treemap_spec(s, dim, size_key, color_key, names, data, labels, formats, style):
    dname = dim_name(dim)
    if dname not in names:
        return None
    size_key = size_key if size_key in names else None
    color_key = color_key if color_key in names else None
    values = []
    for row in data:
        key = row.get(dname)
        v = row.get(size_key) if size_key else 1
        try:
            v = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            v = 0.0
        if v < 0:
            v = 0.0
        values.append((key, v, row))
    if not values:
        return None
    rects = _squarify([(k, v) for k, v, _ in values], 0, 0, 100, 100)
    by_key = {}
    for key, _v, row in values:
        by_key.setdefault(str(key), row)
    out = []
    for key, rx, ry, rw, rh in rects:
        row = by_key.get(str(key), {})
        out.append({dname: key,
                    "__x": rx, "__y": ry, "__x2": rx + rw, "__y2": ry + rh,
                    "__cx": rx + rw / 2, "__cy": ry + rh / 2,
                    "__label": _short_label(str(key), 22),
                    "__value": row.get(size_key) if size_key else None,
                    "__color": (row.get(color_key) if color_key else
                                (row.get(size_key) if size_key else 1))})
    color_numeric = all(isinstance(r.get("__color"), (int, float))
                        and not isinstance(r.get("__color"), bool) for r in out)
    colors = palette_colors((style or {}).get("palette"), (style or {}).get("reverse", False))
    tooltip = [{"field": "__label", "type": "nominal",
                "title": _short_label(labels.get(dname, dname), 40)}]
    if size_key:
        tooltip.append({"field": "__value", "type": "quantitative",
                        "title": _short_label(labels.get(size_key, size_key), 40)})
    ckey = color_key or size_key or ""
    enc = {"x": {"field": "__x", "type": "quantitative", "axis": None,
                 "scale": {"domain": [0, 100]}},
           "x2": {"field": "__x2"},
           "y": {"field": "__y", "type": "quantitative", "axis": None,
                 "scale": {"domain": [0, 100]}},
           "y2": {"field": "__y2"},
           "color": {"field": "__color",
                     "type": "quantitative" if color_numeric else "nominal",
                     "title": _short_label(labels.get(ckey, ckey), 40),
                     "scale": {"range": SEQ if color_numeric else colors}},
           "tooltip": tooltip}
    text_enc = {"x": {"field": "__cx", "type": "quantitative", "axis": None,
                      "scale": {"domain": [0, 100]}},
                "y": {"field": "__cy", "type": "quantitative", "axis": None,
                      "scale": {"domain": [0, 100]}},
                "text": {"field": "__label", "type": "nominal"},
                "tooltip": tooltip}
    return {"data": {"values": out},
            "layer": [
                {"mark": {"type": "rect", "stroke": "#fff", "strokeWidth": 1},
                 "encoding": enc},
                {"mark": {"type": "text", "fontSize": 10, "color": "#222",
                          "align": "center", "baseline": "middle"},
                 "encoding": text_enc},
            ]}


def _custom_tooltip_expr(s, names: list[str]) -> str | None:
    if not s.tooltip:
        return None
    parts = re.split(r"(<[^>]+>)", s.tooltip)
    exprs = []
    for part in parts:
        if not part:
            continue
        if part.startswith("<") and part.endswith(">"):
            items = parse_shelf(part[1:-1])
            if items:
                it = items[0]
                key = (dim_name(it) if (it.agg in PART_LABEL or it.agg in TRUNC_LABEL)
                       else f"{it.agg}:{it.field}")
                if key not in names:
                    key = it.field if it.field in names else key
                if key in names:
                    ref = json.dumps(key)
                    exprs.append(f"(isValid(datum[{ref}]) ? datum[{ref}] : '')")
                    continue
            exprs.append('""')
        else:
            lit = (part.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "<br>").replace("\t", "&nbsp;&nbsp;&nbsp;"))
            exprs.append('"' + lit + '"')
    return " + ".join(exprs) if exprs else None


def _apply_custom_tooltip(spec, s, names: list[str]):
    expr = _custom_tooltip_expr(s, names)
    if not expr or not isinstance(spec, dict):
        return spec
    tt = {"field": "__tooltip", "type": "nominal"}
    spec["transform"] = [{"calculate": expr, "as": "__tooltip"}]
    if "layer" in spec:
        for layer in spec.get("layer", []):
            enc = layer.get("encoding") or {}
            if "tooltip" in enc:
                enc["tooltip"] = tt
    elif "encoding" in spec:
        spec["encoding"]["tooltip"] = tt
    return spec


def _d3_format(fmt: dict | None) -> str | None:
    if not fmt:
        return None
    d = fmt.get("decimals", 0)
    if fmt.get("kind") == "percent":
        return f".{d}%"
    if fmt.get("kind") == "currency":
        return f"$,.{d}f"
    if fmt.get("kind") == "number":
        return f",.{d}f"
    return None


def _value_format(name: str, formats: dict | None, data: list[dict]) -> str | None:
    """Format d3 : format Tableau s'il existe, sinon arrondi lisible."""
    fmt = _d3_format((formats or {}).get(name))
    if fmt:
        return fmt
    vals = [row.get(name) for row in data
            if isinstance(row.get(name), (int, float))
            and not isinstance(row.get(name), bool)]
    if not vals:
        return None
    if all(float(v).is_integer() for v in vals):
        return ",.0f"
    biggest = max(abs(float(v)) for v in vals)
    if biggest < 0.001:
        return ",.3e"
    if biggest >= 1000:
        return ",.1f"
    return ",.2f"


def _tooltip_enc(dnames: list[str], mnames: list[str], labels: dict,
                 formats: dict | None = None, data: list[dict] | None = None) -> list[dict]:
    out = []
    for d in dnames:
        out.append({"field": d, "type": _dim_type(d, []),
                    "title": _short_label(labels.get(d, d), 40)})
    for m in mnames:
        item = {"field": m, "type": "quantitative",
                "title": _short_label(labels.get(m, m), 40)}
        fmt = _value_format(m, formats, data or [])
        if fmt:
            item["format"] = fmt
        out.append(item)
    return out


if __name__ == "__main__":
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    src = os.path.abspath(args[0])
    workdir = args[1] if len(args) > 1 else os.path.dirname(src)

    if "--deps" in sys.argv:
        print(json.dumps(scan_dependencies(src, workdir), indent=2, ensure_ascii=False))
    else:
        wb = open_workbook(src, workdir)
        print(json.dumps(wb.render(), indent=2, default=str))
