"""Socle partagé : chemins, dépôt de fichiers, rendu, sessions publiques.

Aucune dépendance nouvelle : tout repose sur la bibliothèque standard et sur
le moteur `reader.py` existant.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from reader import TWBXError, open_workbook, scan_dependencies

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
PENDING = DATA / "_pending"
PUBLIC = DATA / "public"
HOSTED = DATA / "hosted"
for _d in (DATA, PENDING, PUBLIC, HOSTED):
    _d.mkdir(exist_ok=True)

MAX_UPLOAD = 1500 * 1024 ** 2
MAX_FILES = int(os.environ.get("MAX_FILES", "200"))
MAX_CACHE = int(os.environ.get("MAX_CACHE", "64"))
RETENTION_DAYS = 7
PUBLIC_TTL_HOURS = float(os.environ.get("PUBLIC_TTL_HOURS", "4"))
PENDING_HOURS = 24
MAX_DATA_BYTES = 20 * 1024 ** 3
MAX_PUBLIC_BYTES = int(os.environ.get("MAX_PUBLIC_BYTES", str(5 * 1024 ** 3)))
PUBLIC_MAX_FILES = int(os.environ.get("PUBLIC_MAX_FILES", "20"))
DEFAULT_QUOTA = int(os.environ.get("DEFAULT_QUOTA_BYTES", str(2 * 1024 ** 3)))
MAP_STYLE = os.environ.get("MAP_STYLE_URL", "https://tiles.openfreemap.org/styles/liberty")
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
TRUST_PROXY = os.environ.get("TRUST_PROXY", "") in ("1", "true", "yes")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "") in ("1", "true", "yes")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
PUBLIC_COOKIE = "reader_pub"
SESSION_COOKIE = "reader_sid"
CSRF_COOKIE = "reader_csrf"
SHARE_TTL_HOURS = 12
ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico", ".avif"}


def _code_version() -> str:
    here = Path(__file__).resolve().parent
    files = [here / "reader.py", here / "calcs.py", here / "view.html"]
    return str(int(max(f.stat().st_mtime for f in files if f.exists())))


CODE_VERSION = _code_version()


def load_secret() -> bytes:
    env = os.environ.get("SECRET_KEY", "")
    if env:
        return env.encode()
    path = DATA / ".secret"
    if path.exists():
        return path.read_bytes()
    value = secrets.token_bytes(32)
    path.write_bytes(value)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


SECRET = load_secret()
FINALIZE_LOCK = threading.Lock()


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def public_session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# cache des classeurs
# --------------------------------------------------------------------------- #
_CACHE: OrderedDict[str, tuple[float, object]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _find_twb(dir_path: Path) -> Path:
    twbs = [p for p in Path(dir_path).rglob("*")
            if p.is_file() and p.suffix.lower() == ".twb"]
    if not twbs:
        raise HTTPException(404, "classeur introuvable")
    twbs.sort(key=lambda p: (len(p.relative_to(dir_path).parts), len(str(p))))
    return twbs[0]


def workbook(dir_path: Path, key: str | None = None):
    dir_path = Path(dir_path)
    path = _find_twb(dir_path)
    mtime = path.stat().st_mtime
    key = key or str(dir_path)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == mtime:
            _CACHE.move_to_end(key)
            return hit[1]
    wb = open_workbook(str(path), str(dir_path))
    with _CACHE_LOCK:
        _CACHE[key] = (mtime, wb)
        _CACHE.move_to_end(key)
        while len(_CACHE) > MAX_CACHE:
            _CACHE.popitem(last=False)
    return wb


def drop_cache(key: str) -> None:
    with _CACHE_LOCK:
        _CACHE.pop(key, None)


def _write_json(path: Path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def read_payload(dir_path: Path, key: str | None = None) -> dict:
    path = Path(dir_path) / "render.json"
    if not path.exists():
        raise HTTPException(404, "classeur introuvable")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not data or data.get("version") != CODE_VERSION:
        try:
            fresh = workbook(dir_path, key).render()
            fresh["title"] = data.get("title")
            fresh["files"] = data.get("files", [])
            fresh["version"] = CODE_VERSION
            _write_json(path, fresh)
            data = fresh
        except Exception:
            if not data:
                raise HTTPException(500, "rendu indisponible")
    return data


# --------------------------------------------------------------------------- #
# dépôt et finalisation
# --------------------------------------------------------------------------- #
async def save_files(files: list[UploadFile], dest_dir: Path) -> tuple[int, list[str]]:
    size, names = 0, []
    seen = set()
    reserved = {"pending.json", "render.json", "meta.json"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        name = os.path.basename((f.filename or "").replace("\\", "/"))
        if not name:
            continue
        if name in reserved:
            raise HTTPException(400, f"nom de fichier réservé : {name}")
        if name not in seen:
            if len(seen) >= MAX_FILES:
                raise HTTPException(413, f"trop de fichiers (max {MAX_FILES})")
            seen.add(name)
        names.append(name)
        with (dest_dir / name).open("wb") as out:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "fichiers trop volumineux (max 1,5 Go)")
                out.write(chunk)
    return size, names


def inspect(dir_path: Path) -> dict:
    """État d'un dépôt : twbx complet, twb complet, ou fichiers manquants."""
    dir_path = Path(dir_path)
    try:
        twbx = sorted(p for p in dir_path.iterdir() if p.suffix.lower() == ".twbx")
        if twbx:
            return {"ok": True, "name": twbx[0].name, "kind": "twbx"}
        twbs = sorted(p for p in dir_path.iterdir() if p.suffix.lower() == ".twb")
        if not twbs:
            return {"ok": False, "error": "Aucun fichier .twb ou .twbx reçu."}
        info = scan_dependencies(str(twbs[0]), str(dir_path))
        return {"ok": info["ok"], "name": twbs[0].name, "kind": "twb", "info": info}
    except Exception as exc:
        return {"ok": False, "error": f"Classeur illisible : {exc}"}


def finalize(src_dir: Path, dst_dir: Path, name: str,
             expires_at: float | None = None,
             cache_key: str | None = None) -> tuple[dict, int]:
    """Déplace un dépôt complet vers son dossier définitif et écrit le rendu."""
    src_dir, dst_dir = Path(src_dir), Path(dst_dir)
    (src_dir / "pending.json").unlink(missing_ok=True)
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_dir), str(dst_dir))

    target = dst_dir / name
    wb = open_workbook(str(target), str(dst_dir))
    payload = wb.render()
    payload["title"] = os.path.splitext(name)[0]
    payload["files"] = sorted(p.name for p in dst_dir.iterdir() if p.is_file())
    payload["version"] = CODE_VERSION
    _write_json(dst_dir / "render.json", payload)
    created = time.time()
    exp = expires_at
    _write_json(dst_dir / "meta.json", {
        "files": payload["files"],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(created)),
        "expires": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(exp)) if exp else None,
    })
    size = dir_size(dst_dir)
    key = cache_key or str(dst_dir)
    with _CACHE_LOCK:
        _CACHE[key] = (target.stat().st_mtime, wb)
        _CACHE.move_to_end(key)
        while len(_CACHE) > MAX_CACHE:
            _CACHE.popitem(last=False)
    return payload, size


def view_response(payload: dict, embed: bool = False) -> HTMLResponse:
    """Page de lecture avec nonce CSP (les données du classeur restent non fiables)."""
    nonce = secrets.token_urlsafe(16)
    path = Path(__file__).with_name("view.html")
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    # évite une fermeture prématurée de </script> par une donnée du classeur
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = path.read_text(encoding="utf-8").replace("/*__DATA__*/null", blob)
    if "__CSP_NONCE__" in page:
        page = page.replace("__CSP_NONCE__", nonce)
    else:
        page = page.replace("<script>\nconst WB", f'<script nonce="{nonce}">\nconst WB', 1)
    csp = (
        "default-src 'self' data: blob: https: http:; "
        f"script-src 'self' 'nonce-{nonce}' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https: http:; "
        "font-src 'self' data: https: http:; "
        "connect-src 'self' data: blob: https: http:; "
        "media-src 'self' data: blob: https: http:; "
        "frame-src https: http:; "
        "worker-src 'self' blob:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        + ("frame-ancestors *" if embed else "frame-ancestors 'self'")
    )
    return HTMLResponse(page, headers={"Content-Security-Policy": csp})


# --------------------------------------------------------------------------- #
# purge
# --------------------------------------------------------------------------- #
def cleanup_public() -> None:
    now = time.time()
    for path in PENDING.iterdir():
        if path.is_dir() and now - path.stat().st_mtime > PENDING_HOURS * 3600:
            shutil.rmtree(path, ignore_errors=True)

    # anciens dossiers à la racine de data/
    sessions = [(p, p.stat().st_mtime) for p in DATA.iterdir()
                if p.is_dir() and not p.name.startswith(("_", "public", "hosted"))]
    keep = []
    for path, mtime in sessions:
        if now - mtime > RETENTION_DAYS * 86400:
            shutil.rmtree(path, ignore_errors=True)
            drop_cache(str(path))
        else:
            keep.append((path, mtime))

    sizes = []
    for path, mtime in sorted(keep, key=lambda x: x[1]):
        try:
            sizes.append((path, dir_size(path)))
        except OSError:
            continue
    total = sum(size for _, size in sizes)
    for path, size in sizes:
        if total <= MAX_DATA_BYTES:
            break
        shutil.rmtree(path, ignore_errors=True)
        drop_cache(str(path))
        total -= size


def csv_response(wb, sheet: str) -> Response:
    try:
        engine = wb if hasattr(wb, "csv_sheet") else workbook(Path(wb["dir"]), wb["id"])
        names, rows, labels = engine.csv_sheet(sheet)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, str(exc)) from exc
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([labels.get(n, n) for n in names])
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    filename = sheet.replace('"', "").replace("/", "-").replace("\r", "").replace("\n", "") + ".csv"
    ascii_name = filename.encode("ascii", "ignore").decode() or "export.csv"
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{ascii_name}"; '
                             f"filename*=UTF-8''{quote(filename)}"})


def asset_response(wb: dict, path: str) -> FileResponse:
    root = Path(wb["dir"]).resolve()
    target = (root / path).resolve()
    if (root not in target.parents or not target.is_file()
            or target.suffix.lower() not in ASSET_EXT):
        raise HTTPException(404)
    resp = FileResponse(target)
    if target.suffix.lower() == ".svg":
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    return resp
