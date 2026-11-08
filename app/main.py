"""Serveur : lecteur public éphémère + hébergement (comptes, API, partages)."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import core
import store
import ui
from hosted import router as hosted_router
from reader import TWBXError

DATA = core.DATA
PENDING = core.PENDING
PUBLIC = core.PUBLIC
RETENTION_DAYS = core.RETENTION_DAYS
PUBLIC_TTL_HOURS = core.PUBLIC_TTL_HOURS
ACCESS_PASSWORD = core.ACCESS_PASSWORD

store.init()

app = FastAPI(title="Tableau Libre", description="Lecteur et hébergement de classeurs Tableau")
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
app.include_router(hosted_router)

_ID = re.compile(r"^[A-Za-z0-9_-]{4,90}$")


# --------------------------------------------------------------------------- #
# sécurité globale
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    csrf = request.cookies.get(core.CSRF_COOKIE) or secrets.token_urlsafe(24)
    request.state.csrf = csrf
    response = await call_next(request)
    if not request.cookies.get(core.CSRF_COOKIE):
        response.set_cookie(core.CSRF_COOKIE, csrf, max_age=30 * 86400, httponly=True,
                            samesite="lax", secure=core.COOKIE_SECURE, path="/")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    path = request.url.path
    if path == "/" or path.startswith(("/v/", "/s/", "/embed/", "/account", "/api/")):
        response.headers.setdefault("Cache-Control", "private, no-store")
    if not path.startswith("/embed/"):
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    user = store.get_session_user(request.cookies.get(core.SESSION_COOKIE))
    title = {400: "Requête invalide", 401: "Connexion requise", 403: "Accès refusé",
             404: "Introuvable", 410: "Lien expiré", 413: "Trop volumineux",
             429: "Trop de tentatives"}.get(exc.status_code, "Erreur")
    body = ui.error_page(exc.status_code, title, str(exc.detail), user,
                         getattr(request.state, "csrf", ""))
    return HTMLResponse(body, status_code=exc.status_code)


def _csrf(request: Request) -> str:
    return getattr(request.state, "csrf", "") or request.cookies.get(core.CSRF_COOKIE, "")


def _require_csrf(request: Request, form_token: str = "") -> None:
    token = form_token or request.headers.get("x-csrf-token", "")
    cookie = request.cookies.get(core.CSRF_COOKIE)
    if not cookie or not token or not hmac.compare_digest(cookie, token):
        raise HTTPException(403, "Jeton de sécurité invalide. Rechargez la page.")


def _user(request: Request) -> dict | None:
    user = store.get_session_user(request.cookies.get(core.SESSION_COOKIE))
    if user:
        user["is_admin"] = bool(user.get("is_admin")) or user["email"].lower() in core.ADMIN_EMAILS
    return user


# --------------------------------------------------------------------------- #
# mot de passe global optionnel (compatibilité)
# --------------------------------------------------------------------------- #
def _pw_token(wb_id: str) -> str:
    return hashlib.sha256((ACCESS_PASSWORD + wb_id).encode()).hexdigest()


def _allowed(request: Request, wb_id: str) -> bool:
    if not ACCESS_PASSWORD:
        return True
    token = request.cookies.get("reader_pw")
    return bool(token) and secrets.compare_digest(token, _pw_token(wb_id))


def _lock_page(request: Request, wb_id: str) -> HTMLResponse:
    body = f"""<div class="auth"><div class="card"><h1>Accès protégé</h1>
<p class="lede">Ce classeur est protégé par un mot de passe.</p>
<form method="post" action="/unlock/{ui.esc(wb_id)}">
{ui.csrf_input(_csrf(request))}
<label for="pw">Mot de passe</label>
<input id="pw" name="password" type="password" required autofocus>
<div class="actions" style="margin-top:14px"><button class="btn" type="submit">Ouvrir</button></div>
</form></div></div>"""
    return HTMLResponse(ui.layout("Accès protégé", body, _user(request), _csrf(request), narrow=True))


# --------------------------------------------------------------------------- #
# session publique éphémère
# --------------------------------------------------------------------------- #
def _public_token(request: Request) -> str | None:
    return request.cookies.get(core.PUBLIC_COOKIE)


def _public_hash(token: str | None) -> str | None:
    return core.public_session_hash(token) if token else None


def _set_public_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(core.PUBLIC_COOKIE, token, httponly=True, samesite="lax",
                    secure=core.COOKIE_SECURE, path="/")


def _public_dir(request: Request, wb_id: str) -> Path:
    if not _ID.match(wb_id or ""):
        raise HTTPException(404)
    row = store.public_get(wb_id)
    if row:
        token = _public_token(request)
        if not token or not hmac.compare_digest(_public_hash(token), row["session_hash"]):
            raise HTTPException(403, "Session expirée : ce classeur n’appartient plus à cette session.")
        store.public_touch(wb_id)
        return Path(row["dir"])
    legacy = DATA / wb_id
    if wb_id not in ("public", "hosted", "_pending") and legacy.is_dir():
        return legacy
    raise HTTPException(404, "classeur introuvable")


def _public_payload(request: Request, wb_id: str, directory: Path) -> dict:
    data = core.read_payload(directory, wb_id)
    data["id"] = wb_id
    data["map_style"] = core.MAP_STYLE
    row = store.public_get(wb_id)
    data["public"] = bool(row)
    data["back"] = "/"
    data["backLabel"] = "← Nouveau fichier"
    data["csrf"] = _csrf(request)
    if row:
        data["sessionEnd"] = "/session/end"
    data["apiBase"] = f"/api/sheet/{wb_id}"
    data["csvBase"] = f"/api/csv/{wb_id}"
    data["assetBase"] = f"/asset/{wb_id}/"
    meta_path = directory / "meta.json"
    if meta_path.exists():
        try:
            data["expires"] = json.loads(meta_path.read_text(encoding="utf-8")).get("expires")
        except ValueError:
            pass
    sheet = request.query_params.get("sheet")
    if sheet:
        data["openSheet"] = sheet
    return data


def _pending_public(request: Request, sid: str, info: dict | None, message: str = "") -> HTMLResponse:
    rows = []
    if info:
        for src in info.get("sources", []):
            status = {"ok": '<span class="badge ok">présent</span>',
                      "manquant": '<span class="badge danger">manquant</span>',
                      "live": '<span class="badge warn">connexion live</span>'}.get(
                          src.get("status"), ui.esc(src.get("status")))
            rows.append(f"<tr><td>{ui.esc(src.get('caption'))}"
                        f"<div class='small muted'>{len(src.get('sheets', []))} feuille(s)</div></td>"
                        f"<td>{ui.esc(src.get('kind'))}</td>"
                        f"<td><code class='mono'>{ui.esc(src.get('required') or '—')}</code></td>"
                        f"<td>{status}</td></tr>")
    d = PENDING / sid
    received = ", ".join(sorted(p.name for p in d.iterdir()
                                if p.is_file() and p.name != "pending.json")) if d.is_dir() else "—"
    body = f"""<div class="card">
<h1>Il manque des fichiers</h1>
<p class="lede">Ajoutez les fichiers attendus pour ouvrir le classeur.
La session reste locale et sera supprimée automatiquement.</p>
{('<p class="err">' + ui.esc(message) + '</p>') if message else ''}
{('<div class="tablewrap"><table><thead><tr><th>Source</th><th>Type</th><th>Fichier attendu</th><th>État</th></tr></thead>'
  '<tbody>' + ''.join(rows) + '</tbody></table></div>') if rows else ''}
<form method="post" action="/add/{ui.esc(sid)}" enctype="multipart/form-data" style="margin-top:18px">
{ui.csrf_input(_csrf(request))}
<div class="drop"><input type="file" name="files" multiple required>
<div class="selected-files" data-file-list aria-live="polite" aria-label="Fichiers sélectionnés" hidden></div>
<p class="small muted">Déjà reçu : {ui.esc(received)}</p></div>
<div class="actions" style="margin-top:12px">
<button class="btn" type="submit">Ajouter et ouvrir</button>
<a class="btn sec" href="/">Recommencer</a></div>
</form></div>"""
    return HTMLResponse(ui.layout("Fichiers manquants", body, _user(request), _csrf(request)))


def _advance_public(request: Request, sid: str, token: str) -> Response:
    d = PENDING / sid
    if not d.is_dir():
        raise HTTPException(404, "session expirée, recommencez")
    state = core.inspect(d)
    if not state.get("ok"):
        resp = _pending_public(request, sid, state.get("info"), state.get("error", ""))
        _set_public_cookie(resp, token)
        return resp
    session_hash = _public_hash(token)
    with core.FINALIZE_LOCK:
        if len(store.public_list(session_hash)) >= core.PUBLIC_MAX_FILES:
            shutil.rmtree(d, ignore_errors=True)
            return HTMLResponse(ui.error_page(413, "Limite de session atteinte",
                                              "Trop de classeurs dans cette session. Terminez la session ou supprimez un classeur.",
                                              _user(request), _csrf(request)), status_code=413)
        new_size = core.dir_size(d)
        if store.public_total() + new_size > core.MAX_PUBLIC_BYTES:
            shutil.rmtree(d, ignore_errors=True)
            return HTMLResponse(ui.error_page(413, "Stockage public saturé",
                                              "Le stockage public est momentanément plein. Réessayez plus tard.",
                                              _user(request), _csrf(request)), status_code=413)
        final = PUBLIC / sid
        try:
            payload, size = core.finalize(d, final, state["name"],
                                          time.time() + PUBLIC_TTL_HOURS * 3600,
                                          cache_key=sid)
        except Exception as exc:
            shutil.rmtree(final, ignore_errors=True)
            core.drop_cache(sid)
            return HTMLResponse(ui.error_page(400, "Classeur illisible", str(exc),
                                              _user(request), _csrf(request)), status_code=400)
        if store.public_total() + size > core.MAX_PUBLIC_BYTES:
            shutil.rmtree(final, ignore_errors=True)
            core.drop_cache(sid)
            return HTMLResponse(ui.error_page(413, "Stockage public saturé",
                                              "Le stockage public est momentanément plein. Réessayez plus tard.",
                                              _user(request), _csrf(request)), status_code=413)
        store.public_add(sid, _public_hash(token), Path(state["name"]).stem,
                         str(final), size)
    resp = RedirectResponse(f"/v/{sid}", status_code=303)
    _set_public_cookie(resp, token)
    return resp


# --------------------------------------------------------------------------- #
# pages publiques
# --------------------------------------------------------------------------- #
def _index_page(request: Request, token: str, new_session: bool) -> HTMLResponse:
    user = _user(request)
    rows = []
    for w in store.public_list(_public_hash(token)):
        rows.append(f"""<tr><td><a href="/v/{ui.esc(w['id'])}"><b>{ui.esc(w['name'])}</b></a>
<div class="small muted">ouvert le {ui.fmt_ts(w['created_at'])}</div></td>
<td class="num">{ui.fmt_bytes(w['size_bytes'])}</td>
<td><form method="post" action="/public/{ui.esc(w['id'])}/delete" data-confirm="Supprimer ce classeur de la session ?">
{ui.csrf_input(_csrf(request))}<button class="btn sm danger" type="submit">{ui.icon("trash")} Supprimer</button>
</form></td></tr>""")

    session_html = ""
    if rows:
        session_html = f"""<div class="card" style="margin-top:20px">
<div class="card-head">
  <div><h2>Session en cours</h2>
  <p class="hint" style="margin:0">Vos fichiers sont visibles uniquement avec ce navigateur.</p></div>
  <div class="actions">
    <span class="badge warn">éphémère</span>
    <form method="post" action="/session/end" style="margin:0" data-confirm="Supprimer tous les fichiers de cette session ?">
    {ui.csrf_input(_csrf(request))}<button class="btn sm danger" type="submit">Terminer la session</button>
    </form>
  </div>
</div>
<div class="tablewrap"><table><thead><tr><th>Classeur</th><th class="num">Taille</th><th></th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="small muted" style="margin:12px 0 0">Suppression automatique après
{PUBLIC_TTL_HOURS:g} h sans activité, ou immédiatement avec le bouton.</p></div>"""

    if user:
        hosted = f'<div class="actions" style="margin-top:18px"><a class="btn" href="/account">Ouvrir mon espace {ui.icon("arrow")}</a></div>'
    else:
        hosted = ('<div class="actions" style="margin-top:18px">'
                  '<a class="btn" href="/signup">Créer un compte</a>'
                  '<a class="btn sec" href="/login">Se connecter</a></div>')

    body = f"""<section class="hero">
<span class="eyebrow">{ui.icon("spark", 14)} Lecteur Tableau auto-hébergé</span>
<h1>Vos classeurs Tableau,<br>en lecture seule, sans Tableau.</h1>
<p class="lede">Déposez un <b>.twbx</b>, ou un <b>.twb</b> accompagné de ses <b>.hyper</b> /
<b>.tdsx</b>. Tout reste sur cette machine. Les graphiques, filtres et tableaux de bord
s’ouvrent comme dans Tableau Reader.</p>
<div class="hero-points">
  <span>{ui.icon("check")} Lecture seule</span>
  <span>{ui.icon("check")} Aucun envoi à Tableau</span>
  <span>{ui.icon("check")} Session éphémère</span>
</div></section>

<div class="card featured home-upload" id="lecture">
<div class="card-head">
  <div><h2>Lecture immédiate</h2>
  <p class="hint" style="margin:0">Déposez un classeur et ouvrez-le tout de suite. Rien à installer,
  rien à héberger.</p></div>
  <span class="badge blue">Sans compte</span>
</div>
<form id="form" action="/upload" method="post" enctype="multipart/form-data">
{ui.csrf_input(_csrf(request))}
<div class="drop" id="drop">
  <div class="drop-icon">{ui.icon("upload", 22)}</div>
  <p><b>Déposez votre classeur ici</b></p>
  <p class="small muted">ou cliquez pour parcourir</p>
  <input type="file" name="files" accept=".twbx,.twb,.hyper,.tdsx" multiple required>
  <div class="selected-files" data-file-list aria-live="polite" aria-label="Fichiers sélectionnés" hidden></div>
  <div class="bar" id="bar"><i></i></div>
  <p class="small muted" id="state" style="margin:8px 0 0">1,5 Go maximum par dépôt.</p>
</div>
<div class="actions" style="margin-top:14px">
  <button class="btn" id="go" type="submit">{ui.icon("upload")} Ouvrir le classeur</button>
  <span class="small muted">La session est supprimée automatiquement.</span>
</div>
<p class="format-help"><b>Formats :</b> <code>.twbx</code> seul · <code>.twb</code> +
<code>.hyper</code> · <code>.twb</code> + <code>.tdsx</code><br>
<span class="muted">Exemples : ventes.twbx · ventes.twb + ventes.hyper</span></p>
</form>
</div>

<div class="card host-strip">
<div class="host-copy">
  <h2>Héberger et partager</h2>
  <p class="hint">Gardez vos classeurs en ligne et partagez-les avec un simple lien.</p>
  {hosted}
</div>
<div class="host-benefits">
  <div class="host-benefit"><div class="feature-icon">{ui.icon("shield")}</div>
    <div><b>Vos classeurs restent privés</b><span>Seuls vous et les personnes invitées y accèdent.</span></div></div>
  <div class="host-benefit"><div class="feature-icon">{ui.icon("link")}</div>
    <div><b>Partagez un lien</b><span>Avec une date d’expiration ou un mot de passe.</span></div></div>
  <div class="host-benefit"><div class="feature-icon">{ui.icon("database")}</div>
    <div><b>Mettez les données à jour</b><span>Remplacez le fichier de données, pas le classeur.</span></div></div>
</div>
</div>

<div class="steps">
  <div class="step"><span class="step-num">1</span><div><b>Déposez vos fichiers</b>
    <span>Un .twbx, ou un .twb avec ses fichiers de données.</span></div></div>
  <div class="step"><span class="step-num">2</span><div><b>Naviguez dans le classeur</b>
    <span>Graphiques, filtres et tableaux de bord, sans installation.</span></div></div>
  <div class="step"><span class="step-num">3</span><div><b>Partagez</b>
    <span>Créez un compte pour garder le classeur en ligne.</span></div></div>
</div>
{session_html}
<script>
const form=document.getElementById('form'),
      bar=document.getElementById('bar'),go=document.getElementById('go'),
      st=document.getElementById('state');
form.addEventListener('submit',e=>{{
  if(!form.files.files.length)return;
  e.preventDefault();
  const fd=new FormData(form),xhr=new XMLHttpRequest();
  xhr.open('POST','/upload');
  bar.style.display='block';go.disabled=true;
  xhr.upload.onprogress=ev=>{{if(!ev.lengthComputable)return;
    const p=Math.round(ev.loaded/ev.total*100);
    bar.firstElementChild.style.width=p+'%';st.textContent='Envoi… '+p+' %';}};
  xhr.onload=()=>{{
    if(xhr.status>=200&&xhr.status<400&&/\\/v\\//.test(xhr.responseURL)){{
      window.location=xhr.responseURL;return;}}
    if(xhr.status>=400){{go.disabled=false;bar.style.display='none';
      st.textContent='Échec : '+xhr.status;return;}}
    document.open();document.write(xhr.responseText);document.close();
  }};
  xhr.onerror=()=>{{go.disabled=false;bar.style.display='none';st.textContent='Échec du transfert.';}};
  xhr.send(fd);
}});
</script>"""
    resp = HTMLResponse(ui.layout("Accueil", body, user, _csrf(request)))
    if new_session:
        _set_public_cookie(resp, token)
    return resp


@app.get("/credits", response_class=HTMLResponse)
def credits(request: Request) -> HTMLResponse:
    """Crédits, versions et licences des composants tiers."""
    user = _user(request)
    libs = [
        ("Vega", "5.33.1", "BSD-3-Clause", "Grammaire et rendu des visualisations.",
         "https://vega.github.io/vega/"),
        ("Vega-Lite", "5.23.0", "BSD-3-Clause", "Spécifications des graphiques.",
         "https://vega.github.io/vega-lite/"),
        ("vega-embed", "6.29.0", "BSD-3-Clause", "Intégration des graphiques dans la page.",
         "https://github.com/vega/vega-embed"),
        ("MapLibre GL JS", "4.7.1", "BSD-3-Clause", "Cartes vectorielles et interactions.",
         "https://maplibre.org/"),
        ("html2canvas", "1.4.1", "MIT", "Export PNG des vues.",
         "https://html2canvas.hertzen.com/"),
        ("FastAPI", ">=0.115,<1", "MIT", "Serveur HTTP et API.",
         "https://fastapi.tiangolo.com/"),
        ("Uvicorn", ">=0.30,<1", "BSD-3-Clause", "Serveur ASGI.",
         "https://www.uvicorn.org/"),
        ("lxml", ">=5,<7", "BSD-3-Clause", "Lecture XML des classeurs.",
         "https://lxml.de/"),
        ("python-multipart", ">=0.0.9", "Apache-2.0", "Téléversement de fichiers.",
         "https://github.com/Kludex/python-multipart"),
        ("SQLite", "3", "Domaine public", "Base de données locale des comptes et partages.",
         "https://www.sqlite.org/"),
        ("Python", "3", "PSF-2.0", "Langage du serveur.",
         "https://www.python.org/"),
    ]
    lib_rows = "".join(
        f'<tr><td><b>{ui.esc(name)}</b><div class="small muted">{ui.esc(role)}</div></td>'
        f'<td><code>{ui.esc(version)}</code></td><td>{ui.esc(license_name)}</td>'
        f'<td><a href="{ui.esc(url)}" target="_blank" rel="noopener noreferrer">Source</a></td></tr>'
        for name, version, license_name, role, url in libs)
    fonts = [
        ("IBM Plex Serif", "SIL Open Font License 1.1",
         "https://fonts.google.com/specimen/IBM+Plex+Serif"),
        ("IBM Plex Sans", "SIL Open Font License 1.1",
         "https://fonts.google.com/specimen/IBM+Plex+Sans"),
        ("JetBrains Mono", "SIL Open Font License 1.1",
         "https://www.jetbrains.com/lp/mono/"),
    ]
    font_rows = "".join(
        f'<tr><td><b>{ui.esc(name)}</b></td><td>{ui.esc(license_name)}</td>'
        f'<td><a href="{ui.esc(url)}" target="_blank" rel="noopener noreferrer">Source</a></td></tr>'
        for name, license_name, url in fonts)
    body = f"""<div class="pagehead">
<div><span class="eyebrow">{ui.icon("shield", 14)} Transparence</span>
<h1>Crédits &amp; licences</h1>
<p class="lede">Les outils tiers utilisés par Tableau Libre, leurs versions et leurs licences.
Les versions indiquées sont celles téléchargées à l’installation dans
<code>app/static/</code> ou contraintes par <code>requirements.txt</code>.
Les textes complets des licences sont dans <code>licenses/</code>.</p></div>
</div>

<div class="card" style="margin-bottom:20px">
<h2>Logiciels open source</h2>
<p class="hint">Le site ne redistribue aucun code de Tableau Reader. Le rendu est assuré
par des bibliothèques open source indépendantes.</p>
<div class="tablewrap"><table>
<thead><tr><th>Composant</th><th>Version</th><th>Licence</th><th></th></tr></thead>
<tbody>{lib_rows}</tbody></table></div>
</div>

<div class="card" style="margin-bottom:20px">
<h2>Polices</h2>
<p class="hint">Polices auto-hébergées dans <code>app/static/fonts/</code>, sous SIL OFL 1.1.</p>
<div class="tablewrap"><table>
<thead><tr><th>Police</th><th>Licence</th><th></th></tr></thead>
<tbody>{font_rows}</tbody></table></div>
</div>

<div class="card" style="margin-bottom:20px">
<h2>Composants Tableau</h2>
<p class="hint">Tableau Libre est un projet indépendant, sans affiliation avec Tableau Software
ou Salesforce. La Tableau Hyper API est open source (Apache-2.0). Aucun binaire, code source
ou ressource graphique de Tableau Reader n'est intégré, copié ou redistribué.</p>
<div class="tablewrap"><table>
<thead><tr><th>Composant</th><th>Version</th><th>Licence</th><th>Usage</th></tr></thead>
<tbody>
<tr><td><b>Tableau Hyper API</b></td><td><code>0.0.26963</code></td>
<td>Apache-2.0</td>
<td>Lecture des extraits <code>.hyper</code>. Dépendance Python open source, non modifiée.</td></tr>
<tr><td><b>Formats Tableau</b></td><td>—</td><td>Formats de fichiers</td>
<td>Lecture de <code>.twb</code>, <code>.twbx</code>, <code>.hyper</code> et <code>.tdsx</code>
pour l'interopérabilité.</td></tr>
</tbody></table></div>
</div>

<div class="card">
<h2>Services externes</h2>
<p class="hint">Le fond de carte par défaut est fourni par <b>OpenFreeMap</b>
(style MapLibre, données OpenStreetMap sous ODbL). Il est remplaçable par
n'importe quelle URL de style MapLibre via <code>MAP_STYLE_URL</code>.</p>
<p class="small muted" style="margin:0">« Tableau » est une marque de Salesforce.
Son usage ici est nominatif, pour décrire la compatibilité avec ses formats de fichiers.</p>
</div>"""
    return HTMLResponse(ui.layout("Crédits & licences", body, user, _csrf(request)))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    token = _public_token(request)
    new = False
    if not token:
        token = secrets.token_urlsafe(32)
        new = True
    return _index_page(request, token, new)


@app.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...),
                 csrf: str = Form("")):
    _require_csrf(request, csrf)
    token = _public_token(request) or secrets.token_urlsafe(32)
    sid = secrets.token_urlsafe(8)
    d = PENDING / sid
    d.mkdir(parents=True)
    (d / "pending.json").write_text(json.dumps({
        "kind": "public", "session": _public_hash(token),
    }), encoding="utf-8")
    try:
        await core.save_files(files, d)
        return await run_in_threadpool(_advance_public, request, sid, token)
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    except TWBXError as exc:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc


@app.post("/add/{sid}")
async def add_public(sid: str, request: Request, files: list[UploadFile] = File(...),
                     csrf: str = Form("")):
    _require_csrf(request, csrf)
    d = PENDING / sid
    if not d.is_dir():
        raise HTTPException(404, "session expirée, recommencez")
    token = _public_token(request)
    try:
        meta = json.loads((d / "pending.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if meta.get("kind") != "public" or meta.get("session") != _public_hash(token):
        raise HTTPException(404, "dépôt introuvable")
    try:
        await core.save_files(files, d)
        return await run_in_threadpool(_advance_public, request, sid,
                                       token or secrets.token_urlsafe(32))
    except HTTPException:
        raise
    except TWBXError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/v/{wb_id}", response_class=HTMLResponse)
def view(wb_id: str, request: Request) -> HTMLResponse:
    if not _allowed(request, wb_id):
        return _lock_page(request, wb_id)
    directory = _public_dir(request, wb_id)
    return core.view_response(_public_payload(request, wb_id, directory))


@app.post("/public/{wb_id}/delete")
def public_delete(wb_id: str, request: Request, csrf: str = Form("")):
    _require_csrf(request, csrf)
    row = store.public_get(wb_id)
    if not row:
        raise HTTPException(404)
    token = _public_token(request)
    if not token or not hmac.compare_digest(_public_hash(token), row["session_hash"]):
        raise HTTPException(403)
    shutil.rmtree(row["dir"], ignore_errors=True)
    core.drop_cache(row["dir"])
    store.public_delete(wb_id)
    return RedirectResponse("/", status_code=303)


@app.post("/session/end")
def session_end(request: Request, csrf: str = Form("")):
    _require_csrf(request, csrf)
    token = _public_token(request)
    if token:
        sh = _public_hash(token)
        for row in store.public_end_session(sh):
            shutil.rmtree(row["dir"], ignore_errors=True)
            core.drop_cache(row["dir"])
        for path in PENDING.iterdir():
            if not path.is_dir():
                continue
            try:
                meta = json.loads((path / "pending.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("kind") == "public" and meta.get("session") == sh:
                shutil.rmtree(path, ignore_errors=True)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(core.PUBLIC_COOKIE, path="/")
    return resp


@app.post("/unlock/{wb_id}")
def unlock(wb_id: str, request: Request, password: str = Form(""), csrf: str = Form("")):
    _require_csrf(request, csrf)
    if not wb_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(404)
    if not ACCESS_PASSWORD or not secrets.compare_digest(password, ACCESS_PASSWORD):
        raise HTTPException(403, "mot de passe incorrect")
    resp = RedirectResponse(f"/v/{wb_id}", status_code=303)
    resp.set_cookie("reader_pw", _pw_token(wb_id), httponly=True,
                    samesite="lax", secure=core.COOKIE_SECURE,
                    max_age=RETENTION_DAYS * 86400)
    return resp


# --------------------------------------------------------------------------- #
# API publiques (session éphémère)
# --------------------------------------------------------------------------- #
def _public_authorized(request: Request, wb_id: str) -> Path:
    if not _allowed(request, wb_id):
        raise HTTPException(401, "accès protégé")
    return _public_dir(request, wb_id)


@app.post("/api/sheet/{wb_id}")
def api_sheet(wb_id: str, body: dict, request: Request):
    directory = _public_authorized(request, wb_id)
    try:
        wb = core.workbook(directory, wb_id)
        return wb.render_sheet_by_name(body.get("sheet"), body.get("params"),
                                       body.get("filters"), body.get("drill"))
    except TWBXError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/csv/{wb_id}/{sheet}")
def api_csv(wb_id: str, sheet: str, request: Request):
    directory = _public_authorized(request, wb_id)
    return core.csv_response(core.workbook(directory, wb_id), sheet)


@app.get("/asset/{wb_id}/{path:path}")
def asset(wb_id: str, path: str, request: Request):
    directory = _public_authorized(request, wb_id)
    return core.asset_response({"dir": str(directory)}, path)


# --------------------------------------------------------------------------- #
# purge
# --------------------------------------------------------------------------- #
def cleanup() -> None:
    core.cleanup_public()
    store.cleanup()
    now = time.time()
    for row in store.public_expired(now, core.PUBLIC_TTL_HOURS):
        shutil.rmtree(row["dir"], ignore_errors=True)
        core.drop_cache(row["dir"])
        store.public_delete(row["id"])
    for row in store.expired_workbooks(now):
        shutil.rmtree(row["dir"], ignore_errors=True)
        core.drop_cache(row["dir"])
        store.delete_workbook_any(row["id"])
    known_public = store.all_public_dirs()
    for path in core.PUBLIC.iterdir():
        if path.is_dir() and str(path) not in known_public and now - path.stat().st_mtime > 86400:
            shutil.rmtree(path, ignore_errors=True)
            core.drop_cache(str(path))
    known = store.all_workbook_dirs()
    for user_dir in core.HOSTED.iterdir():
        if not user_dir.is_dir():
            continue
        for path in user_dir.iterdir():
            if not path.is_dir():
                continue
            if (".new." in path.name or ".old." in path.name
                    or path.name.endswith((".new", ".old"))):
                if now - path.stat().st_mtime > 86400:
                    shutil.rmtree(path, ignore_errors=True)
            elif str(path) not in known and now - path.stat().st_mtime > 86400:
                shutil.rmtree(path, ignore_errors=True)
    for directory in known:
        path = Path(directory)
        if not path.is_dir():
            continue
        for f in path.iterdir():
            if (f.is_file() and (".bak." in f.name or ".tmp." in f.name)
                    and now - f.stat().st_mtime > 86400):
                f.unlink(missing_ok=True)


def _cleanup_loop() -> None:
    while True:
        time.sleep(600)
        try:
            cleanup()
        except Exception:
            pass


try:
    cleanup()
except Exception:
    pass
threading.Thread(target=_cleanup_loop, daemon=True).start()
