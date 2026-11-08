"""Hébergement : comptes, espace personnel, API, partages et iframe."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

import core
import store
import ui

router = APIRouter()

# --------------------------------------------------------------------------- #
# petits utilitaires
# --------------------------------------------------------------------------- #
def _csrf(request: Request) -> str:
    return getattr(request.state, "csrf", "") or request.cookies.get(core.CSRF_COOKIE, "")


def _require_csrf(request: Request, token: str) -> None:
    cookie = request.cookies.get(core.CSRF_COOKIE)
    if not cookie or not token or not hmac.compare_digest(cookie, token):
        raise HTTPException(403, "Jeton de sécurité invalide. Rechargez la page.")


def _user(request: Request) -> dict | None:
    user = store.get_session_user(request.cookies.get(core.SESSION_COOKIE))
    if user:
        user["is_admin"] = bool(user.get("is_admin")) or user["email"].lower() in core.ADMIN_EMAILS
    return user


def _login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)


def _client_ip(request: Request) -> str:
    """IP cliente réelle quand l'app est derrière un reverse proxy de confiance."""
    if core.TRUST_PROXY:
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return real
        forwarded = request.headers.get("x-forwarded-for") or ""
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _safe_next(value: str | None) -> str:
    value = (value or "").strip()
    decoded = unquote(value)
    if (not decoded.startswith("/") or decoded.startswith("//")
            or "\\" in decoded or any(ord(c) < 32 for c in decoded)):
        return "/account"
    parts = urlsplit(decoded)
    if parts.scheme or parts.netloc:
        return "/account"
    return value


def _set_login(resp: Response, token: str) -> None:
    resp.set_cookie(core.SESSION_COOKIE, token, max_age=30 * 86400, httponly=True,
                    samesite="lax", secure=core.COOKIE_SECURE, path="/")


def _url(request: Request, path: str) -> str:
    if core.PUBLIC_BASE_URL:
        return core.PUBLIC_BASE_URL + path
    return str(request.base_url).rstrip("/") + path


def _page(request: Request, title: str, body: str, user: dict | None = None,
          narrow: bool = False, status: int = 200) -> HTMLResponse:
    return HTMLResponse(ui.layout(title, body, user, _csrf(request), narrow=narrow),
                        status_code=status)


def _iso(ts: float | None) -> str | None:
    return ui.fmt_date(ts) if ts else None


def _source_files(wb: dict) -> list[str]:
    return [f for f in (wb.get("files") or [])
            if f not in ("render.json", "meta.json")
            and ".bak." not in f and ".tmp." not in f]


def _file_details(wb: dict) -> list[dict]:
    directory = Path(wb["dir"])
    out = []
    for name in _source_files(wb):
        try:
            size = (directory / name).stat().st_size
        except OSError:
            size = 0
        out.append({"name": name, "size_bytes": size,
                    "kind": "data" if name.lower().endswith((".hyper", ".tdsx"))
                    else "workbook"})
    return out


def _data_size(directory: Path) -> int:
    total = 0
    for f in directory.rglob("*"):
        if not f.is_file():
            continue
        if any(".bak." in part or ".tmp." in part for part in f.parts):
            continue
        total += f.stat().st_size
    return total


# --------------------------------------------------------------------------- #
# limitation de tentatives de connexion
# --------------------------------------------------------------------------- #
_FAILS: dict[str, list[float]] = {}
_FAILS_LAST = 0.0


def _prune_fails(now: float) -> None:
    global _FAILS_LAST
    if now - _FAILS_LAST < 300:
        return
    _FAILS_LAST = now
    for key in list(_FAILS):
        hits = [t for t in _FAILS[key] if now - t < 900]
        if hits:
            _FAILS[key] = hits
        else:
            _FAILS.pop(key, None)


def _rate_key(request: Request, email: str) -> str:
    host = _client_ip(request)
    return host + "|" + (email or "").lower()


def _rate_blocked(key: str) -> bool:
    now = time.time()
    _prune_fails(now)
    hits = [t for t in _FAILS.get(key, []) if now - t < 900]
    _FAILS[key] = hits
    return len(hits) >= 8


def _rate_fail(key: str) -> None:
    now = time.time()
    _prune_fails(now)
    _FAILS.setdefault(key, []).append(now)


def _rate_clear(key: str) -> None:
    _FAILS.pop(key, None)


# --------------------------------------------------------------------------- #
# comptes
# --------------------------------------------------------------------------- #
def _auth_form(request: Request, *, signup: bool, error: str = "", email: str = "",
               name: str = "", next_url: str = "") -> HTMLResponse:
    title = "Créer un compte" if signup else "Se connecter"
    action = "/signup" if signup else "/login"
    extra = ""
    if signup:
        extra = f"""<label for="name">Nom (optionnel)</label>
<input id="name" name="name" type="text" value="{ui.esc(name)}" autocomplete="name">"""
    body = f"""<div class="auth"><div class="card">
<div class="auth-head"><div class="auth-mark">{ui.icon("chart", 22)}</div>
<h1>{title}</h1>
<p class="lede">{'Hébergez vos classeurs, créez des liens de partage et une API.'
                 if signup else 'Retrouvez vos classeurs hébergés et vos liens de partage.'}</p></div>
{('<p class="err">' + ui.esc(error) + '</p>') if error else ''}
<form method="post" action="{action}">
{ui.csrf_input(_csrf(request))}
<input type="hidden" name="next" value="{ui.esc(next_url)}">
<label for="email">Adresse e-mail</label>
<input id="email" name="email" type="email" value="{ui.esc(email)}"
       autocomplete="email" required autofocus>
{extra}
<label for="password">Mot de passe</label>
<input id="password" name="password" type="password" minlength="8"
       autocomplete="{'new-password' if signup else 'current-password'}" required>
<p class="hint small">8 caractères minimum.</p>
<div class="actions" style="margin-top:14px">
  <button class="btn" type="submit">{'Créer mon compte' if signup else 'Se connecter'}</button>
  <a class="btn sec" href="{'/login' if signup else '/signup'}">
    {'J’ai déjà un compte' if signup else 'Créer un compte'}</a>
</div>
<p class="auth-alt"><a href="/#lecture">Lire un classeur sans créer de compte</a></p>
</form></div></div>"""
    return _page(request, title, body, _user(request), narrow=True)


@router.get("/signup")
def signup_page(request: Request):
    if _user(request):
        return RedirectResponse("/account", status_code=303)
    return _auth_form(request, signup=True, next_url=request.query_params.get("next", ""))


@router.post("/signup")
def signup(request: Request, email: str = Form(""), password: str = Form(""),
           name: str = Form(""), next: str = Form(""), csrf: str = Form("")):
    _require_csrf(request, csrf)
    key = "signup|" + _client_ip(request)
    if _rate_blocked(key):
        return _auth_form(request, signup=True, error="Trop de créations de compte. Réessayez dans 15 minutes.",
                          email=email, name=name, next_url=next)
    try:
        user = store.create_user(email, password, name)
    except ValueError as exc:
        return _auth_form(request, signup=True, error=str(exc), email=email, name=name, next_url=next)
    _rate_fail(key)
    token = store.create_session(user["id"])
    resp = RedirectResponse(_safe_next(next), status_code=303)
    _set_login(resp, token)
    return resp


@router.get("/login")
def login_page(request: Request):
    if _user(request):
        return RedirectResponse("/account", status_code=303)
    return _auth_form(request, signup=False, next_url=request.query_params.get("next", ""))


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""),
          next: str = Form(""), csrf: str = Form("")):
    _require_csrf(request, csrf)
    key = _rate_key(request, email)
    if _rate_blocked(key):
        return _auth_form(request, signup=False, error="Trop de tentatives. Réessayez dans 15 minutes.",
                          email=email, next_url=next)
    user = store.authenticate(email, password)
    if not user:
        _rate_fail(key)
        return _auth_form(request, signup=False, error="Adresse ou mot de passe incorrect.",
                          email=email, next_url=next)
    _rate_clear(key)
    token = store.create_session(user["id"])
    resp = RedirectResponse(_safe_next(next), status_code=303)
    _set_login(resp, token)
    return resp


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    _require_csrf(request, csrf)
    store.delete_session(request.cookies.get(core.SESSION_COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(core.SESSION_COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- #
# espace personnel
# --------------------------------------------------------------------------- #
def _account_page(request: Request, user: dict, *, new_token: str = "",
                  error: str = "", ok: str = "") -> HTMLResponse:
    workbooks = store.list_workbooks(user["id"])
    used = store.user_used(user["id"])
    quota = max(1, int(user["quota_bytes"]))
    pct = min(100, round(used / quota * 100))
    tokens = store.list_api_tokens(user["id"])

    share_total = sum(w["share_count"] for w in workbooks)
    user_name = user.get("name") or user["email"]
    rows = []
    for w in workbooks:
        state = ('<span class="badge danger">expiré</span>'
                 if w["expires_at"] and w["expires_at"] < time.time()
                 else (f'<span class="badge">jusqu’au {ui.fmt_date(w["expires_at"])}</span>'
                       if w["expires_at"] else '<span class="badge ok">permanent</span>'))
        sources = _source_files(w)
        source_label = ", ".join(sources[:2]) + ("…" if len(sources) > 2 else "")
        rows.append(f"""<tr>
<td><div class="workbook-cell"><span class="file-icon">{ui.icon("chart", 16)}</span>
<div><a href="/account/workbooks/{ui.esc(w['id'])}"><b>{ui.esc(w['name'])}</b></a>
<div class="small muted">{ui.esc(source_label)}</div></div></div></td>
<td class="num">{ui.fmt_bytes(w['size_bytes'])}</td>
<td>{state}</td>
<td class="num">{w['share_count']}</td>
<td class="muted small">{ui.fmt_ts(w['updated_at'])}</td>
<td><div class="actions">
  <a class="btn sm sec" href="/account/workbooks/{ui.esc(w['id'])}/view">{ui.icon("external")} Ouvrir</a>
  <a class="btn sm sec" href="/account/workbooks/{ui.esc(w['id'])}">Gérer</a>
  <form method="post" action="/account/workbooks/{ui.esc(w['id'])}/delete" style="margin:0" data-confirm="Supprimer ce classeur et tous ses liens ?">
    {ui.csrf_input(_csrf(request))}
    <button class="btn sm danger" type="submit">{ui.icon("trash")} Supprimer</button>
  </form>
</div></td></tr>""")

    token_rows = []
    for t in tokens:
        token_rows.append(f"""<tr>
<td class="mono">{ui.esc(t['prefix'])}…</td>
<td>{ui.esc(t['name'])}</td>
<td class="muted small">{ui.fmt_ts(t['created_at'])}</td>
<td class="muted small">{ui.fmt_ts(t['last_used'])}</td>
<td><form method="post" action="/account/tokens/{ui.esc(t['id'])}/delete" data-confirm="Révoquer ce jeton ?">
  {ui.csrf_input(_csrf(request))}
  <button class="btn sm danger" type="submit">Révoquer</button></form></td></tr>""")

    new_token_html = ""
    if new_token:
        new_token_html = f"""<div class="okbox"><b>Jeton créé.</b> Copiez-le maintenant :
il ne sera plus affiché ensuite.<div class="codebox">{ui.esc(new_token)}</div></div>"""

    body = f"""<div class="pagehead">
<div><span class="eyebrow">{ui.icon("users", 14)} Espace personnel</span>
<h1>Mes classeurs</h1>
<p class="lede">Bonjour {ui.esc(user_name)}. Gérez vos classeurs, vos partages et vos accès API.</p></div>
<div class="actions"><a class="btn sec" href="/#lecture" title="Ouvrir un classeur sans le déposer dans votre espace">{ui.icon("external")} Lire sans héberger</a></div>
</div>
{('<p class="okbox">' + ui.esc(ok) + '</p>') if ok else ''}
{('<p class="err">' + ui.esc(error) + '</p>') if error else ''}
{new_token_html}

<div class="stats">
  <div class="stat"><div class="label">{ui.icon("database", 14)} Stockage</div>
    <div class="value">{ui.fmt_bytes(used)}</div>
    <div class="meta">sur {ui.fmt_bytes(quota)} · {pct} % utilisé</div>
    <div class="quota"><i style="width:{pct}%"></i></div></div>
  <div class="stat"><div class="label">{ui.icon("folder", 14)} Classeurs</div>
    <div class="value">{len(workbooks)}</div>
    <div class="meta">hébergé(s) dans votre espace</div></div>
  <div class="stat"><div class="label">{ui.icon("link", 14)} Partages</div>
    <div class="value">{share_total}</div>
    <div class="meta">lien(s) de partage</div></div>
  <div class="stat"><div class="label">{ui.icon("key", 14)} Jetons API</div>
    <div class="value">{len(tokens)}</div>
    <div class="meta">accès programmatique</div></div>
</div>

<div class="card" style="margin-bottom:20px">
<div class="card-head">
  <div><h2>Classeurs hébergés</h2>
  <p class="hint" style="margin:0">Ouvrez, partagez ou mettez à jour vos fichiers.</p></div>
  <span class="badge">{len(workbooks)} classeur(s)</span>
</div>
{('<div class="tablewrap"><table><thead><tr><th>Nom</th><th class="num">Taille</th><th>Expiration</th>'
  '<th class="num">Partages</th><th>Modifié</th><th></th></tr></thead><tbody>'
  + ''.join(rows) + '</tbody></table></div>') if rows else
 '<div class="empty"><div class="empty-icon">' + ui.icon("folder", 22) + '</div>'
 '<b>Aucun classeur pour le moment</b><span>Déposez votre premier fichier pour commencer.</span>'
 '<a class="btn" href="#nouveau">' + ui.icon("upload") + ' Héberger un classeur</a></div>'}
</div>

<div class="card featured" id="nouveau" style="margin-bottom:20px">
<div class="card-head">
  <div><h2>Héberger un classeur</h2>
  <p class="hint" style="margin:0">Formats : <b>.twbx</b> seul, ou <b>.twb</b> accompagné de ses
  <b>.hyper</b> / <b>.tdsx</b>. Les connexions live ne sont pas supportées.<br>
  Exemples : <code>ventes.twbx</code> · <code>ventes.twb</code> + <code>ventes.hyper</code> ·
  <code>ventes.twb</code> + <code>ventes.tdsx</code>.</p></div>
  <span class="badge blue">.twbx · .twb + .hyper · .tdsx</span>
</div>
<form method="post" action="/account/workbooks" enctype="multipart/form-data" id="upform">
{ui.csrf_input(_csrf(request))}
<div class="formrow">
  <div><label for="wbname">Nom affiché (optionnel)</label>
    <input id="wbname" name="name" type="text" placeholder="Mon tableau de bord"></div>
  <div><label for="wbexp">Expiration (optionnel)</label>
    <input id="wbexp" name="expires" type="date"></div>
</div>
<div class="drop" id="drop" style="margin-top:12px">
  <div class="drop-icon">{ui.icon("upload", 22)}</div>
  <p><b>Déposez les fichiers ici</b></p>
  <p class="small muted">ou cliquez pour parcourir · 1,5 Go maximum par dépôt</p>
  <input type="file" name="files" accept=".twbx,.twb,.hyper,.tdsx" multiple required>
  <div class="selected-files" data-file-list aria-live="polite" aria-label="Fichiers sélectionnés" hidden></div>
  <div class="bar" id="bar"><i></i></div>
  <p class="small muted" id="upstate" style="margin:8px 0 0">Prêt à héberger.</p>
</div>
<div class="actions" style="margin-top:12px">
  <button class="btn" type="submit" id="upgo">{ui.icon("upload")} Héberger</button>
</div>
</form>
</div>

<details class="card details" style="margin-bottom:20px">
<summary>{ui.icon("shield")} Sécurité du compte
<span class="summary-meta">mot de passe · suppression</span>
<span class="chev">{ui.icon("arrow", 16)}</span></summary>
<div class="details-body"><div class="grid2">
<div>
<h3>Changer le mot de passe</h3>
<form method="post" action="/account/password">
{ui.csrf_input(_csrf(request))}
<label for="curpw">Mot de passe actuel</label>
<input id="curpw" name="current" type="password" autocomplete="current-password" required>
<label for="newpw">Nouveau mot de passe</label>
<input id="newpw" name="password" type="password" minlength="8"
       autocomplete="new-password" required>
<div class="actions" style="margin-top:10px">
<button class="btn sec" type="submit">Changer</button></div>
</form>
</div>
<div>
<h3>Supprimer le compte</h3>
<p class="hint">Supprime le compte, tous les classeurs hébergés, les liens et les jetons.
Action définitive.</p>
<form method="post" action="/account/delete" data-confirm="Supprimer définitivement votre compte et tous vos classeurs ?">
{ui.csrf_input(_csrf(request))}
<label for="delpw">Confirmez avec votre mot de passe</label>
<input id="delpw" name="password" type="password" autocomplete="current-password" required>
<div class="actions" style="margin-top:10px">
<button class="btn danger" type="submit">Supprimer mon compte</button></div>
</form>
</div>
</div></div>
</details>

<details class="card details"{' open' if new_token else ''}>
<summary>{ui.icon("key")} Jetons d’API
<span class="summary-meta">{len(tokens)} actif(s)</span>
<span class="chev">{ui.icon("arrow", 16)}</span></summary>
<div class="details-body">
<p class="hint">Un jeton donne un accès complet à votre espace via
<code class="mono">Authorization: Bearer …</code>. Créez-en un par outil.</p>
<form method="post" action="/account/tokens" class="formrow" style="align-items:end">
{ui.csrf_input(_csrf(request))}
<div><label for="tokname">Nom du jeton</label>
<input id="tokname" name="name" type="text" placeholder="CI, script, n8n…"></div>
<div style="flex:0 0 auto"><button class="btn" type="submit">Créer un jeton</button></div>
</form>
{('<div class="tablewrap" style="margin-top:12px"><table><thead><tr><th>Préfixe</th><th>Nom</th><th>Créé</th>'
  '<th>Dernier usage</th><th></th></tr></thead><tbody>' + ''.join(token_rows) +
  '</tbody></table></div>') if token_rows else '<p class="small muted">Aucun jeton actif.</p>'}
<p class="small" style="margin:12px 0 0"><a href="/docs">Documentation OpenAPI</a></p>
</div>
</details>

<script>
const form=document.getElementById('upform'),
      bar=document.getElementById('bar'),go=document.getElementById('upgo'),
      st=document.getElementById('upstate');
form.addEventListener('submit',e=>{{
  if(!form.files.files.length)return;
  e.preventDefault();
  const fd=new FormData(form),xhr=new XMLHttpRequest();
  xhr.open('POST','/account/workbooks');
  bar.style.display='block';go.disabled=true;
  xhr.upload.onprogress=ev=>{{if(!ev.lengthComputable)return;
    const p=Math.round(ev.loaded/ev.total*100);
    bar.firstElementChild.style.width=p+'%';st.textContent='Envoi… '+p+' %';}};
  xhr.onload=()=>{{if(xhr.status>=200&&xhr.status<400&&
      /\\/account\\/workbooks\\/[^/]+/.test(xhr.responseURL)){{window.location=xhr.responseURL;return;}}
    if(xhr.status>=400){{go.disabled=false;bar.style.display='none';
      st.innerHTML='<span style="color:#b42318">'+((xhr.responseText||('Erreur '+xhr.status))
      .replace(/<[^>]+>/g,' ').slice(0,300))+'</span>';return;}}
    document.open();document.write(xhr.responseText);document.close();}};
  xhr.onerror=()=>{{go.disabled=false;bar.style.display='none';st.textContent='Échec du transfert.';}};
  xhr.send(fd);
}});
</script>"""
    return _page(request, "Mes classeurs", body, user)


def _admin_page(request: Request, user: dict, *, ok: str = "", error: str = "") -> HTMLResponse:
    stats = store.admin_stats()
    users = store.list_users()
    workbooks = store.all_workbooks()
    user_rows = []
    for u in users:
        is_self = u["id"] == user["id"]
        quota_go = round(int(u["quota_bytes"]) / (1024 ** 3), 1)
        badge = '<span class="badge blue">admin</span>' if u.get("is_admin") else ""
        actions = []
        if not is_self:
            actions.append(f"""<form method="post" action="/admin/users/{ui.esc(u['id'])}/admin" style="margin:0">
{ui.csrf_input(_csrf(request))}
<input type="hidden" name="admin" value="{0 if u.get('is_admin') else 1}">
<button class="btn sm sec" type="submit">{'Retirer admin' if u.get('is_admin') else 'Nommer admin'}</button>
</form>""")
            actions.append(f"""<form method="post" action="/admin/users/{ui.esc(u['id'])}/delete" style="margin:0"
      data-confirm="Supprimer définitivement cet utilisateur et tous ses classeurs ?">
{ui.csrf_input(_csrf(request))}
<button class="btn sm danger" type="submit">Supprimer</button>
</form>""")
        user_rows.append(f"""<tr><td><b>{ui.esc(u['email'])}</b> {badge}
<div class="small muted">{ui.esc(u.get('name') or '—')} · inscrit le {ui.fmt_ts(u['created_at'])}</div></td>
<td class="num">{ui.fmt_bytes(u.get('used_bytes', 0))}</td>
<td class="num">{u.get('workbook_count', 0)}</td>
<td><form method="post" action="/admin/users/{ui.esc(u['id'])}/quota" class="quotaform">
{ui.csrf_input(_csrf(request))}
<input type="number" name="quota_go" min="0.1" step="0.1" value="{quota_go}">
<button class="btn sm sec" type="submit">Go</button></form></td>
<td><div class="actions">{''.join(actions) or '<span class="small muted">compte courant</span>'}</div></td></tr>""")
    wb_rows = []
    for w in workbooks:
        wb_rows.append(f"""<tr><td><b>{ui.esc(w['name'])}</b>
<div class="small muted">{ui.esc(w.get('owner_email') or '')}</div></td>
<td class="num">{ui.fmt_bytes(w['size_bytes'])}</td>
<td class="num">{w.get('share_count', 0)}</td>
<td class="muted small">{ui.fmt_ts(w['updated_at'])}</td></tr>""")
    body = f"""<div class="pagehead">
<div><span class="eyebrow">{ui.icon("shield", 14)} Administration</span>
<h1>Vue d’ensemble</h1>
<p class="lede">Utilisateurs, stockage et classeurs hébergés sur cette instance.</p></div>
</div>
{('<p class="okbox">' + ui.esc(ok) + '</p>') if ok else ''}
{('<p class="err">' + ui.esc(error) + '</p>') if error else ''}
<div class="stats">
  <div class="stat"><div class="label">{ui.icon("users", 14)} Utilisateurs</div><div class="value">{stats['users']}</div><div class="meta">comptes actifs</div></div>
  <div class="stat"><div class="label">{ui.icon("folder", 14)} Classeurs</div><div class="value">{stats['workbooks']}</div><div class="meta">{stats['shares']} lien(s) · {stats['tokens']} jeton(s)</div></div>
  <div class="stat"><div class="label">{ui.icon("database", 14)} Stockage privé</div><div class="value">{ui.fmt_bytes(stats['storage'])}</div><div class="meta">fichiers hébergés</div></div>
  <div class="stat"><div class="label">{ui.icon("external", 14)} Stockage public</div><div class="value">{ui.fmt_bytes(stats['public_storage'])}</div><div class="meta">{stats['public']} session(s) publique(s)</div></div>
</div>
<div class="card" style="margin-bottom:20px">
<div class="card-head"><div><h2>Utilisateurs</h2><p class="hint" style="margin:0">Quota, rôle et suppression.</p></div><span class="badge">{len(users)} compte(s)</span></div>
<div class="tablewrap"><table><thead><tr><th>Compte</th><th class="num">Utilisé</th><th class="num">Classeurs</th><th>Quota (Go)</th><th>Actions</th></tr></thead>
<tbody>{''.join(user_rows)}</tbody></table></div>
</div>
<div class="card">
<div class="card-head"><div><h2>Tous les classeurs</h2><p class="hint" style="margin:0">Vue globale, sans modification.</p></div><span class="badge">{len(workbooks)} classeur(s)</span></div>
<div class="tablewrap"><table><thead><tr><th>Classeur</th><th class="num">Taille</th><th class="num">Partages</th><th>Modifié</th></tr></thead>
<tbody>{''.join(wb_rows)}</tbody></table></div>
</div>"""
    return _page(request, "Administration", body, user)


@router.get("/admin")
def admin_page(request: Request):
    user = _user(request)
    if not user:
        return _login_redirect(request)
    if not user.get("is_admin"):
        return HTMLResponse(ui.error_page(403, "Accès refusé",
                                          "Cette page est réservée aux administrateurs.",
                                          user, _csrf(request)), status_code=403)
    return _admin_page(request, user, ok=request.query_params.get("ok", ""))


@router.post("/admin/users/{user_id}/quota")
def admin_quota(user_id: str, request: Request, quota_go: str = Form(""),
                csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    if not user.get("is_admin"):
        raise HTTPException(403, "accès refusé")
    try:
        value = float(quota_go.replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        return _admin_page(request, user, error="Quota invalide.")
    store.set_user_quota(user_id, int(value * 1024 ** 3))
    return RedirectResponse("/admin?ok=Quota+mis+%C3%A0+jour", status_code=303)


@router.post("/admin/users/{user_id}/admin")
def admin_role(user_id: str, request: Request, admin: str = Form("0"),
               csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    if not user.get("is_admin"):
        raise HTTPException(403, "accès refusé")
    if user_id == user["id"] and admin != "1":
        return _admin_page(request, user, error="Vous ne pouvez pas retirer votre propre accès admin.")
    store.set_user_admin(user_id, admin == "1")
    return RedirectResponse("/admin?ok=R%C3%B4le+mis+%C3%A0+jour", status_code=303)


@router.post("/admin/users/{user_id}/delete")
def admin_delete_user(user_id: str, request: Request, csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    if not user.get("is_admin"):
        raise HTTPException(403, "accès refusé")
    if user_id == user["id"]:
        return _admin_page(request, user, error="Vous ne pouvez pas supprimer votre propre compte ici.")
    target = store.get_user(user_id)
    if not target:
        return _admin_page(request, user, error="Utilisateur introuvable.")
    for wb in store.list_workbooks(user_id):
        shutil.rmtree(wb["dir"], ignore_errors=True)
        core.drop_cache(wb["dir"])
    store.delete_user(user_id)
    return RedirectResponse("/admin?ok=Utilisateur+supprim%C3%A9", status_code=303)


@router.get("/account")
def account(request: Request):
    user = _user(request)
    if not user:
        return _login_redirect(request)
    return _account_page(request, user, ok=request.query_params.get("ok", ""))


@router.post("/account/tokens")
def create_token(request: Request, name: str = Form("API"), csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    _, token = store.create_api_token(user["id"], name)
    return _account_page(request, user, new_token=token, ok="Jeton créé.")


@router.post("/account/tokens/{token_id}/delete")
def delete_token(token_id: str, request: Request, csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    store.delete_api_token(user["id"], token_id)
    return RedirectResponse("/account?ok=Jeton+r%C3%A9voqu%C3%A9", status_code=303)


@router.post("/account/password")
def change_password(request: Request, current: str = Form(""), password: str = Form(""),
                    csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    if not store.verify_password(current, user["pw_hash"]):
        return _account_page(request, user, error="Mot de passe actuel incorrect.")
    try:
        store.set_password(user["id"], password)
    except ValueError as exc:
        return _account_page(request, user, error=str(exc))
    store.delete_user_sessions(user["id"])
    token = store.create_session(user["id"])
    resp = RedirectResponse("/account?ok=Mot+de+passe+modifi%C3%A9", status_code=303)
    _set_login(resp, token)
    return resp


@router.post("/account/delete")
def delete_account(request: Request, password: str = Form(""), csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    if not store.verify_password(password, user["pw_hash"]):
        return _account_page(request, user, error="Mot de passe incorrect.")
    for wb in store.list_workbooks(user["id"]):
        shutil.rmtree(wb["dir"], ignore_errors=True)
        core.drop_cache(wb["dir"])
    store.delete_user(user["id"])
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(core.SESSION_COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- #
# dépôt / remplacement
# --------------------------------------------------------------------------- #
def _finalize_hosted(user: dict, src: Path, twb_name: str, display_name: str | None,
                     expires_at: float | None, existing: dict | None = None) -> dict:
    with core.FINALIZE_LOCK:
        return _finalize_hosted_locked(user, src, twb_name, display_name,
                                       expires_at, existing)


def _finalize_hosted_locked(user: dict, src: Path, twb_name: str, display_name: str | None,
                            expires_at: float | None, existing: dict | None = None) -> dict:
    new_size = core.dir_size(src)
    used = store.user_used(user["id"])
    quota = int(user["quota_bytes"])
    if existing:
        used -= existing["size_bytes"]
    if used + new_size > quota:
        shutil.rmtree(src, ignore_errors=True)
        raise HTTPException(413, "Quota dépassé : ce classeur ne tient pas dans votre espace.")

    wb_id = existing["id"] if existing else "w_" + secrets.token_urlsafe(10)
    final = core.HOSTED / user["id"] / wb_id
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.with_name(wb_id + ".new." + secrets.token_urlsafe(4))
    old = final.with_name(wb_id + ".old." + secrets.token_urlsafe(4))
    try:
        if existing:
            core.finalize(src, staging, twb_name, expires_at)
            actual = core.dir_size(staging)
            if used + actual > quota:
                shutil.rmtree(staging, ignore_errors=True)
                core.drop_cache(str(staging))
                raise HTTPException(413, "Quota dépassé après extraction : ce classeur ne tient pas dans votre espace.")
            if final.exists():
                final.rename(old)
            staging.rename(final)
            core.drop_cache(str(staging))
            core.drop_cache(str(final))
            files = sorted(p.name for p in final.iterdir()
                           if p.is_file() and p.name not in ("render.json", "meta.json"))
            store.update_workbook(user["id"], wb_id, name=display_name,
                                  files=files, size=actual,
                                  expires_at=expires_at)
            shutil.rmtree(old, ignore_errors=True)
        else:
            core.finalize(src, final, twb_name, expires_at, cache_key=wb_id)
            actual = core.dir_size(final)
            if used + actual > quota:
                shutil.rmtree(final, ignore_errors=True)
                core.drop_cache(wb_id)
                raise HTTPException(413, "Quota dépassé après extraction : ce classeur ne tient pas dans votre espace.")
            files = sorted(p.name for p in final.iterdir()
                           if p.is_file() and p.name not in ("render.json", "meta.json"))
            store.create_workbook(user["id"], wb_id,
                                  display_name or Path(twb_name).stem,
                                  str(final), files, actual, expires_at)
    except HTTPException:
        raise
    except Exception as exc:
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        core.drop_cache(wb_id)
        core.drop_cache(str(staging))
        if existing:
            if old.exists():
                shutil.rmtree(final, ignore_errors=True)
                old.rename(final)
        else:
            shutil.rmtree(final, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    return store.get_workbook(user["id"], wb_id)


DATA_EXT = {".hyper", ".tdsx"}


async def _replace_data(user: dict, wb: dict, files: list[UploadFile]) -> dict:
    """Remplace des extraits existants (même nom), puis régénère le rendu."""
    tmp = core.PENDING / ("d_" + secrets.token_urlsafe(10))
    tmp.mkdir(parents=True)
    try:
        await core.save_files(files, tmp)
        return await run_in_threadpool(_replace_data_sync, user, wb, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _replace_data_sync(user: dict, wb: dict, tmp: Path) -> dict:
    with core.FINALIZE_LOCK:
        return _replace_data_locked(user, wb, tmp)


def _replace_data_locked(user: dict, wb: dict, tmp: Path) -> dict:
    directory = Path(wb["dir"])
    token = secrets.token_urlsafe(6)
    backups: list[tuple[Path, Path]] = []
    cache_backups: list[tuple[Path, Path]] = []
    render = directory / "render.json"
    render_backup: Path | None = None
    try:
        uploads = [p for p in tmp.iterdir() if p.is_file()]
        if not uploads:
            raise HTTPException(400, "aucun fichier reçu")
        for p in uploads:
            if p.suffix.lower() not in DATA_EXT:
                raise HTTPException(400, f"{p.name} : seuls .hyper et .tdsx sont acceptés")
            target = directory / p.name
            if not target.is_file():
                raise HTTPException(400, f"{p.name} n'existe pas dans ce classeur ; "
                                         "utilisez « Remplacer les fichiers » pour un nouveau jeu")
        for p in uploads:
            target = directory / p.name
            backup = target.with_name(target.name + ".bak." + token)
            try:
                os.link(target, backup)
            except OSError:
                shutil.copy2(target, backup)
            backups.append((target, backup))
            if p.suffix.lower() == ".tdsx":
                cache = directory / "_tdsx" / p.stem
                if cache.is_dir():
                    cb = cache.with_name(cache.name + ".bak." + token)
                    cache.rename(cb)
                    cache_backups.append((cache, cb))
        if render.exists():
            render_backup = render.with_name("render.json.bak." + token)
            shutil.copy2(render, render_backup)
        for p in uploads:
            os.replace(p, directory / p.name)
        core.drop_cache(wb["id"])
        engine = core.workbook(directory, wb["id"])
        payload = engine.render()
        payload["title"] = wb["name"]
        payload["files"] = sorted(x.name for x in directory.iterdir()
                                  if x.is_file() and x.name not in ("render.json", "meta.json")
                                  and ".bak." not in x.name and ".tmp." not in x.name)
        payload["version"] = core.CODE_VERSION
        new_render = directory / ("render.json.tmp." + token)
        new_render.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(new_render, render)
        size = _data_size(directory)
        used = store.user_used(user["id"]) - wb["size_bytes"]
        if used + size > int(user["quota_bytes"]):
            raise HTTPException(413, "Quota dépassé après mise à jour des données.")
        store.update_workbook(user["id"], wb["id"], files=payload["files"], size=size)
        for _, backup in backups:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
        for _, cb in cache_backups:
            shutil.rmtree(cb, ignore_errors=True)
        if render_backup:
            try:
                render_backup.unlink(missing_ok=True)
            except OSError:
                pass
        return store.get_workbook(user["id"], wb["id"])
    except Exception as exc:
        core.drop_cache(wb["id"])
        for target, backup in backups:
            if backup.exists():
                os.replace(backup, target)
        for cache, cb in cache_backups:
            shutil.rmtree(cache, ignore_errors=True)
            if cb.exists():
                cb.rename(cache)
        if render_backup and render_backup.exists():
            os.replace(render_backup, render)
        for leftover in directory.glob("*.bak." + token):
            leftover.unlink(missing_ok=True)
        for leftover in directory.glob("render.json.tmp." + token):
            leftover.unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, core.TWBXError):
            raise HTTPException(400, str(exc)) from exc
        raise


def _missing_payload(state: dict) -> dict:
    info = state.get("info") or {}
    missing = []
    for src in info.get("sources", []):
        if src.get("status") != "ok":
            missing.append({"source": src.get("caption"), "kind": src.get("kind"),
                            "required": src.get("required"), "status": src.get("status")})
    return {"error": state.get("error") or "fichiers manquants", "missing": missing}


def _pending_hosted(request: Request, user: dict, sid: str, state: dict,
                    message: str = "") -> HTMLResponse:
    info = state.get("info") or {}
    rows = []
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
    d = core.PENDING / sid
    received = ", ".join(sorted(p.name for p in d.iterdir()
                                if p.is_file() and p.name != "pending.json")) if d.is_dir() else "—"
    body = f"""<div class="card">
<h1>Il manque des fichiers</h1>
<p class="lede">Classeur <code class="mono">{ui.esc(info.get('twb') or '—')}</code>.
Ajoutez les fichiers attendus pour terminer l’hébergement.</p>
{('<p class="err">' + ui.esc(message) + '</p>') if message else ''}
<div class="tablewrap"><table><thead><tr><th>Source</th><th>Type</th><th>Fichier attendu</th><th>État</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<form method="post" action="/account/add/{ui.esc(sid)}" enctype="multipart/form-data"
      style="margin-top:18px">
{ui.csrf_input(_csrf(request))}
<div class="drop"><input type="file" name="files" multiple required>
<div class="selected-files" data-file-list aria-live="polite" aria-label="Fichiers sélectionnés" hidden></div>
<p class="small muted">Déjà reçu : {ui.esc(received)}</p></div>
<div class="actions" style="margin-top:12px">
<button class="btn" type="submit">Ajouter et terminer</button>
<a class="btn sec" href="/account">Annuler</a></div>
</form></div>"""
    return _page(request, "Fichiers manquants", body, user)


def _new_pending(user: dict, name: str, expires: float | None) -> str:
    sid = "h_" + secrets.token_urlsafe(10)
    d = core.PENDING / sid
    d.mkdir(parents=True)
    (d / "pending.json").write_text(json.dumps({
        "kind": "user", "user_id": user["id"], "name": name, "expires": expires,
    }), encoding="utf-8")
    return sid


def _advance_hosted(request: Request, user: dict, sid: str) -> Response:
    d = core.PENDING / sid
    if not d.is_dir():
        raise HTTPException(404, "dépôt expiré, recommencez")
    try:
        meta = json.loads((d / "pending.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    state = core.inspect(d)
    if not state.get("ok"):
        if state.get("error"):
            return _pending_hosted(request, user, sid, state, state["error"])
        return _pending_hosted(request, user, sid, state)
    try:
        wb = _finalize_hosted(user, d, state["name"], meta.get("name"),
                              meta.get("expires"))
    except HTTPException as exc:
        if d.is_dir():
            return _pending_hosted(request, user, sid, state, str(exc.detail))
        return HTMLResponse(ui.error_page(exc.status_code, "Dépôt impossible",
                                          str(exc.detail), user, _csrf(request)),
                            status_code=exc.status_code)
    return RedirectResponse(f"/account/workbooks/{wb['id']}?ok=Classeur+h%C3%A9berg%C3%A9",
                            status_code=303)


@router.post("/account/workbooks")
async def upload_workbook(request: Request, files: list[UploadFile] = File(...),
                          name: str = Form(""), expires: str = Form(""),
                          csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    sid = _new_pending(user, name.strip(), ui.parse_date(expires))
    d = core.PENDING / sid
    try:
        await core.save_files(files, d)
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return await run_in_threadpool(_advance_hosted, request, user, sid)


@router.post("/account/add/{sid}")
async def add_workbook(sid: str, request: Request, files: list[UploadFile] = File(...),
                       csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    d = core.PENDING / sid
    try:
        meta = json.loads((d / "pending.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "dépôt expiré, recommencez")
    if meta.get("kind") != "user" or meta.get("user_id") != user["id"]:
        raise HTTPException(404, "dépôt introuvable")
    try:
        await core.save_files(files, d)
    except HTTPException:
        raise
    return await run_in_threadpool(_advance_hosted, request, user, sid)


def _workbook_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()
                  if p.is_file() and p.name not in ("render.json", "meta.json")
                  and ".bak." not in p.name and ".tmp." not in p.name)


def _checked_file(wb: dict, filename: str) -> str:
    name = os.path.basename((filename or "").replace("\\", "/"))
    if name != filename or name not in _source_files(wb):
        raise HTTPException(404, "fichier introuvable")
    return name


def _mutate_files(user: dict, wb: dict, *, changes=(), deletes=(), do_render: bool = True) -> dict:
    with core.FINALIZE_LOCK:
        directory = Path(wb["dir"])
        token = secrets.token_urlsafe(6)
        backups: list[tuple[Path, Path]] = []
        added: list[Path] = []
        render_path = directory / "render.json"
        render_backup: Path | None = None
        try:
            for src, name in changes:
                target = directory / name
                if target.exists():
                    backup = target.with_name(name + ".bak." + token)
                    try:
                        os.link(target, backup)
                    except OSError:
                        shutil.copy2(target, backup)
                    backups.append((target, backup))
                os.replace(src, target)
                added.append(target)
            for name in deletes:
                target = directory / name
                backup = target.with_name(name + ".bak." + token)
                try:
                    os.link(target, backup)
                except OSError:
                    shutil.copy2(target, backup)
                backups.append((target, backup))
                target.unlink()
            shutil.rmtree(directory / "_tdsx", ignore_errors=True)
            core.drop_cache(wb["id"])
            if do_render:
                if render_path.exists():
                    render_backup = render_path.with_name("render.json.bak." + token)
                    shutil.copy2(render_path, render_backup)
                engine = core.workbook(directory, wb["id"])
                payload = engine.render()
                payload["title"] = wb["name"]
                payload["files"] = _workbook_files(directory)
                payload["version"] = core.CODE_VERSION
                core._write_json(render_path, payload)
            files = _workbook_files(directory)
            size = _data_size(directory)
            if do_render:
                used = store.user_used(user["id"]) - wb["size_bytes"]
                if used + size > int(user["quota_bytes"]):
                    raise HTTPException(413, "Quota dépassé après modification des fichiers.")
            store.update_workbook(user["id"], wb["id"], files=files, size=size)
            for _, backup in backups:
                backup.unlink(missing_ok=True)
            if render_backup:
                render_backup.unlink(missing_ok=True)
            return store.get_workbook(user["id"], wb["id"])
        except Exception as exc:
            core.drop_cache(wb["id"])
            for target, backup in backups:
                if backup.exists():
                    os.replace(backup, target)
            for target in added:
                if not any(target == t for t, _ in backups) and target.exists():
                    target.unlink()
            if render_backup and render_backup.exists():
                os.replace(render_backup, render_path)
            for leftover in directory.glob("*.bak." + token):
                leftover.unlink(missing_ok=True)
            for leftover in directory.glob("render.json.bak." + token):
                leftover.unlink(missing_ok=True)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------- #
# détail / actions
# --------------------------------------------------------------------------- #
def _wb_or_404(user: dict, wb_id: str) -> dict:
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    return wb


def _share_row(request: Request, s: dict) -> str:
    key_q = f"?key={quote(s['url_key'])}" if s.get("url_key") else ""
    url = ("" if s.get("iframe_only") else _url(request, f"/s/{s['token']}") + key_q)
    embed = (_url(request, f"/embed/{s['token']}") + key_q) if s["allow_embed"] else ""
    state = ('<span class="badge danger">expiré</span>'
             if s["expires_at"] and s["expires_at"] < time.time()
             else (f'<span class="badge">jusqu’au {ui.fmt_date(s["expires_at"])}</span>'
                   if s["expires_at"] else '<span class="badge ok">sans expiration</span>'))
    pw = '<span class="badge warn">mot de passe</span>' if s["pw_hash"] else ""
    emb = '<span class="badge blue">iframe</span>' if s["allow_embed"] else ""
    only = '<span class="badge blue">iframe uniquement</span>' if s.get("iframe_only") else ""
    ip = f'<span class="badge">IP : {ui.esc(s.get("ip_whitelist") or "")}</span>' if s.get("ip_whitelist") else ""
    keyb = '<span class="badge">clé URL</span>' if s.get("url_key") else ""
    links = ""
    if url:
        links += ('<div class="small muted" style="margin:6px 0 2px">Lien direct</div>'
                  f'<div class="codebox">{ui.esc(url)}</div>')
    if embed:
        links += ('<div class="small muted" style="margin:6px 0 2px">Intégration iframe</div>'
                  f'<div class="codebox">{ui.esc(ui.iframe_snippet(embed))}</div>')
    return f"""<tr><td><b>{ui.esc(s['label'] or 'Lien de partage')}</b>
<div class="small muted">créé le {ui.fmt_ts(s['created_at'])} · {s['views']} vue(s)</div></td>
<td>{state} {pw} {emb} {only} {ip} {keyb}</td>
<td class="num">{s['views']}</td>
<td><details><summary>Liens</summary>{links}</details></td>
<td><div class="actions">
<form method="post" action="/account/shares/{ui.esc(s['id'])}/delete"
      data-confirm="Révoquer ce lien ?">
{ui.csrf_input(_csrf(request))}<button class="btn sm danger" type="submit">Révoquer</button>
</form>
<details><summary>Modifier</summary>
<form method="post" action="/account/shares/{ui.esc(s['id'])}/settings">
{ui.csrf_input(_csrf(request))}
<label>Libellé</label><input name="label" value="{ui.esc(s['label'])}">
<label>Expiration</label><input type="date" name="expires" value="{ui.fmt_date(s['expires_at']) if s['expires_at'] else ''}">
<label>Nouveau mot de passe (laisser vide pour conserver)</label>
<input type="password" name="password" autocomplete="new-password">
<label class="checkline"><input type="checkbox" name="remove_password"> Retirer le mot de passe</label>
<label>Adresses IP autorisées (séparées par des virgules)</label>
<input name="ip_whitelist" value="{ui.esc(s.get('ip_whitelist') or '')}" placeholder="127.0.0.1, 10.0.0.0/8">
<label class="checkline"><input type="checkbox" name="allow_embed" {'checked' if s['allow_embed'] else ''}> Autoriser l’iframe</label>
<label class="checkline"><input type="checkbox" name="iframe_only" {'checked' if s.get('iframe_only') else ''}> Uniquement en iframe</label>
<label class="checkline"><input type="checkbox" name="require_key" {'checked' if s.get('url_key') else ''}> Exiger une clé dans l’URL</label>
<div class="actions" style="margin-top:10px"><button class="btn sec sm" type="submit">Enregistrer</button></div>
</form></details>
</div></td></tr>"""


def _detail_page(request: Request, user: dict, wb: dict, *, ok: str = "",
                 error: str = "") -> HTMLResponse:
    shares = store.list_shares(wb["id"])
    current_files = _source_files(wb)
    main_files = [f for f in current_files if f.lower().endswith((".twb", ".twbx"))]
    file_rows = []
    for idx, f in enumerate(current_files):
        try:
            fsize = (Path(wb["dir"]) / f).stat().st_size
        except OSError:
            fsize = 0
        is_main = f.lower().endswith((".twb", ".twbx"))
        can_delete = not (is_main and len(main_files) <= 1)
        file_rows.append(f"""<div class="managed-file">
<div class="managed-file-name"><span class="file-icon">{ui.icon("database" if f.lower().endswith((".hyper", ".tdsx")) else "chart", 16)}</span>
<div><code>{ui.esc(f)}</code><span class="small muted">{ui.fmt_bytes(fsize)}</span></div></div>
<div class="managed-file-actions">
<form method="post" action="/account/workbooks/{ui.esc(wb['id'])}/files/{quote(f, safe='')}/replace"
      enctype="multipart/form-data" data-replace>
{ui.csrf_input(_csrf(request))}
<input class="sr-file" id="rf-{idx}" type="file" name="file" data-auto-submit required>
<label class="btn sec sm" for="rf-{idx}" title="Le fichier doit porter le même nom">Remplacer</label>
</form>
{('<form method="post" action="/account/workbooks/' + ui.esc(wb['id']) + '/files/' + quote(f, safe='') + '/delete" data-confirm="Supprimer le fichier ' + ui.esc(f) + ' ?">' + ui.csrf_input(_csrf(request)) + '<button class="btn danger sm" type="submit">Supprimer</button></form>') if can_delete else '<span class="badge">fichier principal</span>'}
</div></div>""")
    expires = ui.fmt_date(wb["expires_at"]) if wb["expires_at"] else ""
    expired = wb["expires_at"] and wb["expires_at"] < time.time()
    share_rows = "".join(_share_row(request, s) for s in shares)
    view_url = _url(request, f"/account/workbooks/{wb['id']}/view")
    body = f"""<a class="backlink" href="/account">{ui.icon("arrow")} Mes classeurs</a>
<div class="pagehead">
<div><h1>{ui.esc(wb['name'])}</h1>
<p class="lede">{len(_source_files(wb))} fichier(s) ·
{ui.fmt_bytes(wb['size_bytes'])} · modifié le {ui.fmt_ts(wb['updated_at'])}</p></div>
<div class="actions">
  <a class="btn" href="/account/workbooks/{ui.esc(wb['id'])}/view">{ui.icon("external")} Ouvrir le classeur</a>
</div></div>
{('<p class="okbox">' + ui.esc(ok) + '</p>') if ok else ''}
{('<p class="err">' + ui.esc(error) + '</p>') if error else ''}
<p>{('<span class="badge danger">expiré</span>' if expired else
     ('<span class="badge ok">permanent</span>' if not wb['expires_at'] else
      '<span class="badge">expire le ' + ui.fmt_date(wb['expires_at']) + '</span>'))}
<span class="badge">{wb['views']} ouverture(s)</span></p>

<div class="grid2">
<div class="card">
<h2>Lien de partage</h2>
<p class="hint">Créez un lien public, éventuellement protégé et expirant.
L’iframe permet de l’intégrer dans un site.</p>
<form method="post" action="/account/workbooks/{ui.esc(wb['id'])}/shares">
{ui.csrf_input(_csrf(request))}
<label for="slabel">Libellé (optionnel)</label>
<input id="slabel" name="label" type="text" placeholder="Mon menu">
<p class="small muted">Le libellé personnalise le lien : <code>/s/…-mon-menu</code>.</p>
<div class="formrow">
  <div><label for="sexp">Expiration (optionnel)</label>
  <input id="sexp" name="expires" type="date"></div>
  <div><label for="spw">Mot de passe (optionnel)</label>
  <input id="spw" name="password" type="password" autocomplete="new-password"></div>
</div>
<label for="sips">Adresses IP autorisées (optionnel)</label>
<input id="sips" name="ip_whitelist" type="text" placeholder="127.0.0.1, 10.0.0.0/8">
<label class="checkline"><input type="checkbox" name="allow_embed" checked> Autoriser l’intégration iframe</label>
<label class="checkline"><input type="checkbox" name="iframe_only"> Uniquement en iframe</label>
<label class="checkline"><input type="checkbox" name="require_key"> Exiger une clé dans l’URL</label>
<div class="actions" style="margin-top:12px"><button class="btn" type="submit">Créer le lien</button></div>
</form>
</div>

<div class="card">
<h2>Fichiers du classeur</h2>
<p class="hint">Chaque fichier peut être remplacé ou supprimé individuellement. Pour remplacer,
le nouveau fichier doit porter exactement le même nom.</p>
<div class="managed-files">{''.join(file_rows)}</div>
<form method="post" action="/account/workbooks/{ui.esc(wb['id'])}/files/add"
      enctype="multipart/form-data" class="file-add">
{ui.csrf_input(_csrf(request))}
<div class="formrow">
  <div><label for="addfiles">Ajouter des fichiers</label>
    <input id="addfiles" type="file" name="files" accept=".twbx,.twb,.hyper,.tdsx" multiple required></div>
  <div style="flex:0 0 auto;align-self:end"><button class="btn" type="submit">Ajouter</button></div>
</div>
<p class="small muted">Exemples : <code>ventes.twbx</code> · <code>ventes.twb</code> +
<code>ventes.hyper</code> · <code>ventes.twb</code> + <code>ventes.tdsx</code>.</p>
</form>
<hr>
<h2>Paramètres</h2>
<form method="post" action="/account/workbooks/{ui.esc(wb['id'])}/settings">
{ui.csrf_input(_csrf(request))}
<div class="formrow">
  <div><label for="wbn">Nom affiché</label>
  <input id="wbn" name="name" type="text" value="{ui.esc(wb['name'])}"></div>
  <div><label for="wbe">Expiration</label>
  <input id="wbe" name="expires" type="date" value="{expires}"></div>
</div>
<p class="small muted">Laissez la date vide pour un classeur permanent.</p>
<div class="actions" style="margin-top:10px">
<button class="btn sec" type="submit">Enregistrer</button></div>
</form>
<hr>
<form method="post" action="/account/workbooks/{ui.esc(wb['id'])}/delete" data-confirm="Supprimer définitivement ce classeur et ses liens ?">
{ui.csrf_input(_csrf(request))}
<button class="btn danger" type="submit">Supprimer le classeur</button>
</form>
</div>
</div>

<div class="card" style="margin-top:18px">
<div class="card-head"><div><h2>Liens de partage</h2>
<p class="hint" style="margin:0">Chaque lien peut être révoqué à tout moment.</p></div>
<span class="badge">{len(shares)} lien(s)</span></div>
{('<div class="tablewrap"><table><thead><tr><th>Libellé</th><th>État</th><th class="num">Vues</th>'
  '<th>URL / iframe</th><th></th></tr></thead><tbody>' + share_rows +
  '</tbody></table></div>') if shares else
 '<div class="empty"><div class="empty-icon">' + ui.icon("link", 22) + '</div>'
 '<b>Aucun lien pour le moment</b><span>Créez un lien de partage ci-dessus.</span></div>'}
</div>

<div class="card" style="margin-top:18px">
<h2>API</h2>
<p class="hint">Remplacez <code class="mono">$TOKEN</code> par un jeton créé sur
la page « Mes classeurs ».</p>
<div class="codebox">curl -H "Authorization: Bearer $TOKEN" \\
  {ui.esc(_url(request, '/api/v1/workbooks'))}

curl -X POST -H "Authorization: Bearer $TOKEN" \\
  -F "files=@mon.twbx" \\
  {ui.esc(_url(request, '/api/v1/workbooks'))}</div>
<p class="small"><a href="/docs">Documentation OpenAPI</a> ·
vue directe : <span class="mono">{ui.esc(view_url)}</span></p>
</div>"""
    return _page(request, wb["name"], body, user)


@router.get("/account/workbooks/{wb_id}")
def workbook_detail(wb_id: str, request: Request):
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = _wb_or_404(user, wb_id)
    return _detail_page(request, user, wb, ok=request.query_params.get("ok", ""))


@router.post("/account/workbooks/{wb_id}/shares")
def create_share(wb_id: str, request: Request, label: str = Form(""),
                 expires: str = Form(""), password: str = Form(""),
                 allow_embed: str = Form(""), iframe_only: str = Form(""),
                 require_key: str = Form(""), ip_whitelist: str = Form(""),
                 csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = _wb_or_404(user, wb_id)
    try:
        ips = _validate_ip_whitelist(ip_whitelist)
        store.create_share(wb_id, label=label, expires_at=ui.parse_date(expires),
                           password=password.strip() or None,
                           allow_embed=allow_embed == "on",
                           iframe_only=iframe_only == "on",
                           ip_whitelist=ips,
                           require_key=require_key == "on")
    except ValueError as exc:
        return _detail_page(request, user, wb, error=str(exc))
    return RedirectResponse(f"/account/workbooks/{wb_id}?ok=Lien+cr%C3%A9%C3%A9", status_code=303)


@router.post("/account/shares/{share_id}/settings")
def update_share_settings(share_id: str, request: Request, label: str = Form(""),
                          expires: str = Form(""), password: str = Form(""),
                          remove_password: str = Form(""), allow_embed: str = Form(""),
                          iframe_only: str = Form(""), require_key: str = Form(""),
                          ip_whitelist: str = Form(""), csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    share = store.get_share_by_id(share_id)
    if not share:
        raise HTTPException(404, "lien introuvable")
    wb = store.get_workbook(user["id"], share["workbook_id"])
    if not wb:
        raise HTTPException(404, "lien introuvable")
    try:
        ips = _validate_ip_whitelist(ip_whitelist)
        kwargs = {
            "label": label,
            "expires_at": ui.parse_date(expires),
            "allow_embed": allow_embed == "on" or iframe_only == "on",
            "iframe_only": iframe_only == "on",
            "ip_whitelist": ips,
            "require_key": require_key == "on",
        }
        if remove_password == "on":
            kwargs["password"] = ""
        elif password.strip():
            kwargs["password"] = password.strip()
        store.update_share(share_id, **kwargs)
    except ValueError as exc:
        return _detail_page(request, user, wb, error=str(exc))
    return RedirectResponse(f"/account/workbooks/{wb['id']}?ok=Lien+mis+%C3%A0+jour",
                            status_code=303)


@router.post("/account/shares/{share_id}/delete")
def delete_share(share_id: str, request: Request, csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    share = store.get_share_by_id(share_id)
    if not share:
        raise HTTPException(404, "lien introuvable")
    wb = store.get_workbook(user["id"], share["workbook_id"])
    if not wb:
        raise HTTPException(404, "lien introuvable")
    store.delete_share(wb["id"], share_id)
    return RedirectResponse(f"/account/workbooks/{wb['id']}?ok=Lien+r%C3%A9voqu%C3%A9",
                            status_code=303)


@router.post("/account/workbooks/{wb_id}/settings")
def workbook_settings(wb_id: str, request: Request, name: str = Form(""),
                      expires: str = Form(""), csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = _wb_or_404(user, wb_id)
    exp = ui.parse_date(expires)
    store.update_workbook(user["id"], wb_id, name=name.strip() or wb["name"],
                          expires_at=exp if exp else 0)
    return RedirectResponse(f"/account/workbooks/{wb_id}?ok=Param%C3%A8tres+enregistr%C3%A9s",
                            status_code=303)


@router.post("/account/workbooks/{wb_id}/replace")
async def replace_workbook(wb_id: str, request: Request,
                           files: list[UploadFile] = File(...), csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = _wb_or_404(user, wb_id)
    sid = "h_" + secrets.token_urlsafe(10)
    d = core.PENDING / sid
    d.mkdir(parents=True)
    try:
        await core.save_files(files, d)
        state = await run_in_threadpool(core.inspect, d)
        if not state.get("ok"):
            shutil.rmtree(d, ignore_errors=True)
            err = state.get("error") or "fichiers manquants : " + ", ".join(
                s.get("required") or "?" for s in (state.get("info") or {}).get("sources", [])
                if s.get("status") != "ok")
            return _detail_page(request, user, wb, error=err)
        await run_in_threadpool(_finalize_hosted, user, d, state["name"], None,
                                wb["expires_at"], wb)
    except HTTPException as exc:
        shutil.rmtree(d, ignore_errors=True)
        return _detail_page(request, user, wb, error=str(exc.detail))
    return RedirectResponse(f"/account/workbooks/{wb_id}?ok=Fichiers+remplac%C3%A9s",
                            status_code=303)


@router.post("/account/workbooks/{wb_id}/data")
async def update_data_web(wb_id: str, request: Request,
                          files: list[UploadFile] = File(...), csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = _wb_or_404(user, wb_id)
    try:
        await _replace_data(user, wb, files)
    except HTTPException as exc:
        return _detail_page(request, user, wb, error=str(exc.detail))
    return RedirectResponse(f"/account/workbooks/{wb_id}?ok=Donn%C3%A9es+mises+%C3%A0+jour",
                            status_code=303)


@router.post("/account/workbooks/{wb_id}/files/add")
async def add_files(wb_id: str, request: Request, files: list[UploadFile] = File(...),
                    csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = _wb_or_404(user, wb_id)
    tmp = core.PENDING / ("f_" + secrets.token_urlsafe(10))
    try:
        await core.save_files(files, tmp)
        uploads = [p for p in tmp.iterdir() if p.is_file()]
        if not uploads:
            return _detail_page(request, user, wb, error="Aucun fichier reçu.")
        existing = set(_source_files(wb))
        clash = [p.name for p in uploads if p.name in existing]
        if clash:
            return _detail_page(request, user, wb,
                                error="Ces fichiers existent déjà, utilisez « Remplacer » : "
                                      + ", ".join(clash))
        changes = [(p, p.name) for p in uploads]
        wb = await run_in_threadpool(_mutate_files, user, wb, changes=changes)
    except HTTPException as exc:
        return _detail_page(request, user, wb, error=str(exc.detail))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return RedirectResponse(f"/account/workbooks/{wb_id}?ok=Fichiers+ajout%C3%A9s",
                            status_code=303)


@router.post("/account/workbooks/{wb_id}/files/{filename:path}/replace")
async def replace_file(wb_id: str, filename: str, request: Request,
                       file: UploadFile = File(...), csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = _wb_or_404(user, wb_id)
    try:
        name = _checked_file(wb, filename)
    except HTTPException:
        return _detail_page(request, user, wb, error="Fichier introuvable.")
    tmp = core.PENDING / ("f_" + secrets.token_urlsafe(10))
    try:
        await core.save_files([file], tmp)
        uploads = [p for p in tmp.iterdir() if p.is_file()]
        if not uploads:
            return _detail_page(request, user, wb, error="Aucun fichier reçu.")
        src = uploads[0]
        if src.name != name:
            return _detail_page(request, user, wb,
                                error=f"Le fichier doit porter le même nom : {name}")
        wb = await run_in_threadpool(_mutate_files, user, wb, changes=[(src, name)])
    except HTTPException as exc:
        return _detail_page(request, user, wb, error=str(exc.detail))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return RedirectResponse(f"/account/workbooks/{wb_id}?ok=Fichier+remplac%C3%A9",
                            status_code=303)


@router.post("/account/workbooks/{wb_id}/files/{filename:path}/delete")
def delete_file(wb_id: str, filename: str, request: Request, csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = _wb_or_404(user, wb_id)
    try:
        name = _checked_file(wb, filename)
    except HTTPException:
        return _detail_page(request, user, wb, error="Fichier introuvable.")
    remaining = [f for f in _source_files(wb) if f != name]
    is_main = name.lower().endswith((".twb", ".twbx"))
    if is_main and not any(f.lower().endswith((".twb", ".twbx")) for f in remaining):
        return _detail_page(request, user, wb,
                            error="Impossible de supprimer le dernier fichier principal (.twb/.twbx).")
    try:
        wb = _mutate_files(user, wb, deletes=[name], do_render=is_main)
    except HTTPException as exc:
        return _detail_page(request, user, wb, error=str(exc.detail))
    return RedirectResponse(f"/account/workbooks/{wb_id}?ok=Fichier+supprim%C3%A9",
                            status_code=303)


@router.post("/account/workbooks/{wb_id}/delete")
def delete_workbook(wb_id: str, request: Request, csrf: str = Form("")):
    _require_csrf(request, csrf)
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = store.delete_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    shutil.rmtree(wb["dir"], ignore_errors=True)
    core.drop_cache(wb["dir"])
    return RedirectResponse("/account?ok=Classeur+supprim%C3%A9", status_code=303)


# --------------------------------------------------------------------------- #
# rendu propriétaire
# --------------------------------------------------------------------------- #
def _payload_for(request: Request, wb: dict, *, mode: str, token: str = "",
                 embed: bool = False, share: dict | None = None) -> dict:
    data = core.read_payload(Path(wb["dir"]), wb["id"])
    data["id"] = wb["id"]
    data["title"] = wb["name"]
    data["map_style"] = core.MAP_STYLE
    data["embed"] = bool(embed)
    data["back"] = "/account" if mode == "owner" else ""
    data["backLabel"] = "← Mes classeurs"
    if share:
        data["expires"] = _iso(share["expires_at"])
        data["shareKey"] = share.get("url_key") or ""
    else:
        data["expires"] = _iso(wb["expires_at"])
    if mode == "owner":
        data["apiBase"] = f"/account/workbooks/{wb['id']}/api/sheet"
        data["csvBase"] = f"/account/workbooks/{wb['id']}/api/csv"
        data["assetBase"] = f"/account/workbooks/{wb['id']}/asset/"
        data["manageUrl"] = f"/account/workbooks/{wb['id']}"
    else:
        prefix = "embed" if embed else "s"
        data["apiBase"] = f"/{prefix}/{token}/api/sheet"
        data["csvBase"] = f"/{prefix}/{token}/api/csv"
        data["assetBase"] = f"/{prefix}/{token}/asset/"
    sheet = request.query_params.get("sheet")
    if sheet:
        data["openSheet"] = sheet
    return data


@router.get("/account/workbooks/{wb_id}/view")
def workbook_view(wb_id: str, request: Request):
    user = _user(request)
    if not user:
        return _login_redirect(request)
    wb = _wb_or_404(user, wb_id)
    store.touch_workbook(wb_id)
    data = _payload_for(request, wb, mode="owner")
    return core.view_response(data)


@router.post("/account/workbooks/{wb_id}/api/sheet")
def owner_sheet(wb_id: str, body: dict, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(401, "connexion requise")
    wb = _wb_or_404(user, wb_id)
    return _sheet_json(wb, body)


@router.get("/account/workbooks/{wb_id}/api/csv/{sheet}")
def owner_csv(wb_id: str, sheet: str, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(401, "connexion requise")
    wb = _wb_or_404(user, wb_id)
    return core.csv_response(wb, sheet)


@router.get("/account/workbooks/{wb_id}/asset/{path:path}")
def owner_asset(wb_id: str, path: str, request: Request):
    user = _user(request)
    if not user:
        raise HTTPException(401, "connexion requise")
    wb = _wb_or_404(user, wb_id)
    return core.asset_response(wb, path)


# --------------------------------------------------------------------------- #
# partages publics
# --------------------------------------------------------------------------- #
def _share_cookie_name(share_id: str) -> str:
    return "reader_shr_" + share_id


def _share_cookie_value(share: dict) -> str:
    msg = (share["id"] + ":" + (share["pw_hash"] or "")).encode()
    return hmac.new(core.SECRET, msg, hashlib.sha256).hexdigest()


def _share_unlocked(request: Request, share: dict) -> bool:
    value = request.cookies.get(_share_cookie_name(share["id"]))
    return bool(value) and hmac.compare_digest(value, _share_cookie_value(share))


def _validate_ip_whitelist(value: str) -> str:
    value = (value or "").strip()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ipaddress.ip_network(item, strict=False) if "/" in item else ipaddress.ip_address(item)
        except ValueError as exc:
            raise ValueError(f"adresse IP ou plage invalide : {item}") from exc
    return value


def _share_access_error(request: Request, share: dict, embed: bool) -> str | None:
    if embed and not share["allow_embed"]:
        return "L’intégration iframe est désactivée pour ce lien."
    if not embed and share.get("iframe_only"):
        return "Ce lien est réservé à une intégration iframe."
    whitelist = (share.get("ip_whitelist") or "").strip()
    if whitelist:
        host = _client_ip(request)
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return "Adresse IP non autorisée."
        allowed = False
        for item in whitelist.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                if "/" in item:
                    allowed = addr in ipaddress.ip_network(item, strict=False)
                else:
                    allowed = addr == ipaddress.ip_address(item)
            except ValueError:
                continue
            if allowed:
                break
        if not allowed:
            return "Votre adresse IP n’est pas autorisée pour ce lien."
    key = share.get("url_key")
    if key:
        given = request.query_params.get("key", "")
        if not given or not hmac.compare_digest(given, key):
            return "Clé d’accès manquante ou invalide."
    return None


def _resolve_share(request: Request, token: str, embed: bool = False):
    share = store.share_with_workbook(token)
    if not share:
        return None, "Lien de partage introuvable.", 404
    if share["expires_at"] and share["expires_at"] < time.time():
        return None, "Ce lien de partage a expiré.", 410
    if share.get("workbook_expires_at") and share["workbook_expires_at"] < time.time():
        return None, "Ce classeur a expiré.", 410
    access_error = _share_access_error(request, share, embed)
    if access_error:
        return None, access_error, 403
    if share["pw_hash"] and not _share_unlocked(request, share):
        return "locked", None, 401
    return share, None, 200


def _share_password_page(request: Request, token: str, embed: bool, error: str = "") -> HTMLResponse:
    key = request.query_params.get("key", "")
    action = f"/{'embed' if embed else 's'}/{token}/unlock" + (f"?key={quote(key)}" if key else "")
    body = f"""<div class="auth"><div class="card"><h1>Lien protégé</h1>
<p class="lede">Ce partage demande un mot de passe.</p>
{('<p class="err">' + ui.esc(error) + '</p>') if error else ''}
<form method="post" action="{action}">
{ui.csrf_input(_csrf(request))}
<label for="pw">Mot de passe</label>
<input id="pw" name="password" type="password" required autofocus>
<div class="actions" style="margin-top:14px"><button class="btn" type="submit">Ouvrir</button></div>
</form></div></div>"""
    return _page(request, "Lien protégé", body, _user(request), narrow=True)


@router.get("/s/{token}")
def share_view(token: str, request: Request):
    share, err, status = _resolve_share(request, token, embed=False)
    if share == "locked":
        return _share_password_page(request, token, False)
    if err:
        return HTMLResponse(ui.error_page(status, "Lien indisponible", err,
                                          _user(request), _csrf(request)),
                            status_code=status)
    wb = store.get_workbook_any(share["workbook_id"])
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    store.touch_share(share["id"])
    data = _payload_for(request, wb, mode="share", token=token, share=share)
    return core.view_response(data)


@router.get("/embed/{token}")
def embed_view(token: str, request: Request):
    share, err, status = _resolve_share(request, token, embed=True)
    if share == "locked":
        return _share_password_page(request, token, True)
    if err:
        return HTMLResponse(ui.error_page(status, "Lien indisponible", err,
                                          None, _csrf(request)), status_code=status)
    wb = store.get_workbook_any(share["workbook_id"])
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    store.touch_share(share["id"])
    data = _payload_for(request, wb, mode="embed", token=token, embed=True, share=share)
    return core.view_response(data, embed=True)


@router.post("/s/{token}/unlock")
def share_unlock(token: str, request: Request, password: str = Form(""), csrf: str = Form("")):
    _require_csrf(request, csrf)
    key = "share|" + token + "|" + _client_ip(request)
    if _rate_blocked(key):
        return _share_password_page(request, token, False,
                                    "Trop de tentatives. Réessayez dans 15 minutes.")
    share = store.share_with_workbook(token)
    if not share:
        raise HTTPException(404, "lien introuvable")
    if share["expires_at"] and share["expires_at"] < time.time():
        return HTMLResponse(ui.error_page(410, "Lien expiré",
                                          "Ce lien de partage a expiré.",
                                          _user(request), _csrf(request)), status_code=410)
    access_error = _share_access_error(request, share, False)
    if access_error:
        return HTMLResponse(ui.error_page(403, "Lien indisponible", access_error,
                                          _user(request), _csrf(request)), status_code=403)
    if not store.verify_password(password, share["pw_hash"]):
        _rate_fail(key)
        return _share_password_page(request, token, False, "Mot de passe incorrect.")
    _rate_clear(key)
    key = request.query_params.get("key", "")
    resp = RedirectResponse(f"/s/{token}" + (f"?key={quote(key)}" if key else ""), status_code=303)
    resp.set_cookie(_share_cookie_name(share["id"]), _share_cookie_value(share),
                    max_age=core.SHARE_TTL_HOURS * 3600, httponly=True,
                    samesite="lax", secure=core.COOKIE_SECURE, path="/")
    return resp


@router.post("/embed/{token}/unlock")
def embed_unlock(token: str, request: Request, password: str = Form(""), csrf: str = Form("")):
    _require_csrf(request, csrf)
    key = "embed|" + token + "|" + _client_ip(request)
    if _rate_blocked(key):
        return _share_password_page(request, token, True,
                                    "Trop de tentatives. Réessayez dans 15 minutes.")
    share = store.share_with_workbook(token)
    if not share:
        raise HTTPException(404, "lien introuvable")
    if share["expires_at"] and share["expires_at"] < time.time():
        return HTMLResponse(ui.error_page(410, "Lien expiré",
                                          "Ce lien de partage a expiré.",
                                          None, _csrf(request)), status_code=410)
    access_error = _share_access_error(request, share, True)
    if access_error:
        return HTMLResponse(ui.error_page(403, "Lien indisponible", access_error,
                                          None, _csrf(request)), status_code=403)
    if not store.verify_password(password, share["pw_hash"]):
        _rate_fail(key)
        return _share_password_page(request, token, True, "Mot de passe incorrect.")
    _rate_clear(key)
    key = request.query_params.get("key", "")
    resp = RedirectResponse(f"/embed/{token}" + (f"?key={quote(key)}" if key else ""), status_code=303)
    resp.set_cookie(_share_cookie_name(share["id"]), _share_cookie_value(share),
                    max_age=core.SHARE_TTL_HOURS * 3600, httponly=True,
                    samesite="lax", secure=core.COOKIE_SECURE, path="/")
    return resp


def _shared_workbook(token: str, embed: bool, request: Request) -> dict:
    share, err, status = _resolve_share(request, token, embed=embed)
    if err:
        raise HTTPException(status, err)
    if share == "locked":
        raise HTTPException(401, "mot de passe requis")
    wb = store.get_workbook_any(share["workbook_id"])
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    return wb


@router.post("/s/{token}/api/sheet")
def share_sheet(token: str, body: dict, request: Request):
    return _sheet_json(_shared_workbook(token, False, request), body)


@router.post("/embed/{token}/api/sheet")
def embed_sheet(token: str, body: dict, request: Request):
    return _sheet_json(_shared_workbook(token, True, request), body)


@router.get("/s/{token}/api/csv/{sheet}")
def share_csv(token: str, sheet: str, request: Request):
    return core.csv_response(_shared_workbook(token, False, request), sheet)


@router.get("/embed/{token}/api/csv/{sheet}")
def embed_csv(token: str, sheet: str, request: Request):
    return core.csv_response(_shared_workbook(token, True, request), sheet)


@router.get("/s/{token}/asset/{path:path}")
def share_asset(token: str, path: str, request: Request):
    return core.asset_response(_shared_workbook(token, False, request), path)


@router.get("/embed/{token}/asset/{path:path}")
def embed_asset(token: str, path: str, request: Request):
    return core.asset_response(_shared_workbook(token, True, request), path)


# --------------------------------------------------------------------------- #
# helpers rendu / fichiers
# --------------------------------------------------------------------------- #
def _sheet_json(wb: dict, body: dict):
    try:
        engine = core.workbook(Path(wb["dir"]), wb["id"])
        return engine.render_sheet_by_name(body.get("sheet"), body.get("params"),
                                           body.get("filters"), body.get("drill"))
    except core.TWBXError as exc:
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------- #
# API v1
# --------------------------------------------------------------------------- #
def api_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "jeton d’API requis (Authorization: Bearer …)")
    user = store.user_by_api_token(auth[7:].strip())
    if not user:
        raise HTTPException(401, "jeton d’API invalide")
    user["is_admin"] = bool(user.get("is_admin")) or user["email"].lower() in core.ADMIN_EMAILS
    return user


def _json_workbook(request: Request, wb: dict, with_shares: bool = False) -> dict:
    out = {
        "id": wb["id"],
        "name": wb["name"],
        "size_bytes": wb["size_bytes"],
        "files": _source_files(wb),
        "file_details": _file_details(wb),
        "created_at": _iso(wb["created_at"]),
        "updated_at": _iso(wb["updated_at"]),
        "expires_at": _iso(wb["expires_at"]),
        "views": wb["views"],
        "url": _url(request, f"/account/workbooks/{wb['id']}"),
        "view_url": _url(request, f"/account/workbooks/{wb['id']}/view"),
    }
    if with_shares:
        out["shares"] = [_json_share(request, s) for s in store.list_shares(wb["id"])]
    return out


def _json_share(request: Request, s: dict) -> dict:
    key_q = f"?key={quote(s['url_key'])}" if s.get("url_key") else ""
    direct = None if s.get("iframe_only") else _url(request, f"/s/{s['token']}") + key_q
    embed = (_url(request, f"/embed/{s['token']}") + key_q) if s["allow_embed"] else None
    return {
        "id": s["id"],
        "label": s["label"],
        "url": direct,
        "embed_url": embed,
        "iframe": ui.iframe_snippet(embed) if embed else None,
        "allow_embed": bool(s["allow_embed"]),
        "iframe_only": bool(s.get("iframe_only")),
        "ip_whitelist": s.get("ip_whitelist") or "",
        "key_required": bool(s.get("url_key")),
        "key": s.get("url_key"),
        "password": bool(s["pw_hash"]),
        "expires_at": _iso(s["expires_at"]),
        "created_at": _iso(s["created_at"]),
        "views": s["views"],
    }


@router.get("/api/v1/me")
def api_me(request: Request, user: dict = Depends(api_user)):
    """Informations du compte : identité, quota, stockage, nombre de classeurs et de liens."""
    used = store.user_used(user["id"])
    workbooks = store.list_workbooks(user["id"])
    shares = sum(len(store.list_shares(w["id"])) for w in workbooks)
    return {"id": user["id"], "email": user["email"], "name": user["name"],
            "is_admin": bool(user.get("is_admin")),
            "quota_bytes": user["quota_bytes"], "used_bytes": used,
            "workbooks": len(workbooks), "shares": shares,
            "tokens": len(store.list_api_tokens(user["id"]))}


@router.get("/api/v1/workbooks")
def api_workbooks(request: Request, user: dict = Depends(api_user)):
    return [_json_workbook(request, w) for w in store.list_workbooks(user["id"])]


@router.post("/api/v1/workbooks")
async def api_create_workbook(request: Request, files: list[UploadFile] = File(...),
                              name: str = Form(""), expires: str = Form(""),
                              user: dict = Depends(api_user)):
    sid = _new_pending(user, name.strip(), ui.parse_date(expires))
    d = core.PENDING / sid
    try:
        await core.save_files(files, d)
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    state = await run_in_threadpool(core.inspect, d)
    if not state.get("ok"):
        payload = _missing_payload(state)
        shutil.rmtree(d, ignore_errors=True)
        return JSONResponse(payload, status_code=400)
    try:
        wb = await run_in_threadpool(_finalize_hosted, user, d, state["name"],
                                     name.strip() or None, ui.parse_date(expires))
    except HTTPException as exc:
        shutil.rmtree(d, ignore_errors=True)
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
    return JSONResponse(_json_workbook(request, wb), status_code=201)


@router.get("/api/v1/workbooks/{wb_id}")
def api_workbook(wb_id: str, request: Request, user: dict = Depends(api_user)):
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    return _json_workbook(request, wb, with_shares=True)


@router.get("/api/v1/workbooks/{wb_id}/files")
def api_files(wb_id: str, request: Request, user: dict = Depends(api_user)):
    """Liste les fichiers du classeur avec leur taille et leur type."""
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    return {"workbook_id": wb_id, "files": _file_details(wb)}


@router.post("/api/v1/workbooks/{wb_id}/files")
async def api_add_files(wb_id: str, request: Request, files: list[UploadFile] = File(...),
                        user: dict = Depends(api_user)):
    """Ajoute un ou plusieurs fichiers au classeur, sans remplacer les existants."""
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    tmp = core.PENDING / ("a_" + secrets.token_urlsafe(10))
    try:
        await core.save_files(files, tmp)
        uploads = [p for p in tmp.iterdir() if p.is_file()]
        if not uploads:
            raise HTTPException(400, "aucun fichier reçu")
        existing = set(_source_files(wb))
        clash = [p.name for p in uploads if p.name in existing]
        if clash:
            raise HTTPException(400, "fichiers déjà présents : " + ", ".join(clash))
        wb = await run_in_threadpool(_mutate_files, user, wb,
                                     changes=[(p, p.name) for p in uploads])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _json_workbook(request, wb, with_shares=True)


@router.put("/api/v1/workbooks/{wb_id}/files/{filename:path}")
async def api_replace_file(wb_id: str, filename: str, request: Request,
                           file: UploadFile = File(...),
                           user: dict = Depends(api_user)):
    """Remplace un fichier existant. Le nouveau fichier doit avoir le même nom."""
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    name = _checked_file(wb, filename)
    tmp = core.PENDING / ("a_" + secrets.token_urlsafe(10))
    try:
        await core.save_files([file], tmp)
        uploads = [p for p in tmp.iterdir() if p.is_file()]
        if not uploads:
            raise HTTPException(400, "aucun fichier reçu")
        src = uploads[0]
        if src.name != name:
            raise HTTPException(400, f"le fichier doit porter le nom : {name}")
        wb = await run_in_threadpool(_mutate_files, user, wb, changes=[(src, name)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _json_workbook(request, wb, with_shares=True)


@router.delete("/api/v1/workbooks/{wb_id}/files/{filename:path}")
def api_delete_file(wb_id: str, filename: str, request: Request,
                    user: dict = Depends(api_user)):
    """Supprime un fichier. Le dernier .twb/.twbx ne peut pas être supprimé."""
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    name = _checked_file(wb, filename)
    remaining = [f for f in _source_files(wb) if f != name]
    is_main = name.lower().endswith((".twb", ".twbx"))
    if is_main and not any(f.lower().endswith((".twb", ".twbx")) for f in remaining):
        raise HTTPException(400, "impossible de supprimer le dernier fichier principal")
    wb = _mutate_files(user, wb, deletes=[name], do_render=is_main)
    return _json_workbook(request, wb, with_shares=True)


@router.put("/api/v1/workbooks/{wb_id}")
async def api_replace_workbook(wb_id: str, request: Request,
                               files: list[UploadFile] = File(...),
                               name: str = Form(""), expires: str = Form(""),
                               user: dict = Depends(api_user)):
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    sid = "h_" + secrets.token_urlsafe(10)
    d = core.PENDING / sid
    d.mkdir(parents=True)
    try:
        await core.save_files(files, d)
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    state = await run_in_threadpool(core.inspect, d)
    if not state.get("ok"):
        payload = _missing_payload(state)
        shutil.rmtree(d, ignore_errors=True)
        return JSONResponse(payload, status_code=400)
    exp = ui.parse_date(expires) if expires else wb["expires_at"]
    try:
        wb = await run_in_threadpool(_finalize_hosted, user, d, state["name"],
                                     name.strip() or None, exp, wb)
    except HTTPException as exc:
        shutil.rmtree(d, ignore_errors=True)
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
    return _json_workbook(request, wb, with_shares=True)


@router.put("/api/v1/workbooks/{wb_id}/data")
async def api_update_data(wb_id: str, request: Request,
                          files: list[UploadFile] = File(...),
                          user: dict = Depends(api_user)):
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    try:
        wb = await _replace_data(user, wb, files)
    except HTTPException as exc:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
    return _json_workbook(request, wb, with_shares=True)


@router.patch("/api/v1/workbooks/{wb_id}")
def api_patch_workbook(wb_id: str, request: Request, body: dict = Body(...),
                       user: dict = Depends(api_user)):
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    name = body.get("name")
    clean_name = str(name).strip()[:120] if name is not None else None
    if clean_name == "":
        clean_name = None
    exp = None
    if "expires_at" in body or "expires" in body:
        raw = body.get("expires_at", body.get("expires"))
        if raw in (None, "", False):
            exp = 0
        else:
            exp = ui.parse_date(str(raw)[:10])
            if exp is None:
                raise HTTPException(400, "date d’expiration invalide (AAAA-MM-JJ)")
    store.update_workbook(user["id"], wb_id,
                          name=clean_name,
                          expires_at=exp)
    return _json_workbook(request, store.get_workbook(user["id"], wb_id), with_shares=True)


@router.delete("/api/v1/workbooks/{wb_id}")
def api_delete_workbook(wb_id: str, request: Request, user: dict = Depends(api_user)):
    wb = store.delete_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    shutil.rmtree(wb["dir"], ignore_errors=True)
    core.drop_cache(wb["dir"])
    return {"deleted": wb_id}


@router.get("/api/v1/workbooks/{wb_id}/shares")
def api_shares(wb_id: str, request: Request, user: dict = Depends(api_user)):
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    return [_json_share(request, s) for s in store.list_shares(wb_id)]


@router.post("/api/v1/workbooks/{wb_id}/shares")
def api_create_share(wb_id: str, request: Request, body: dict = Body(default={}),
                     user: dict = Depends(api_user)):
    wb = store.get_workbook(user["id"], wb_id)
    if not wb:
        raise HTTPException(404, "classeur introuvable")
    raw = body.get("expires_at", body.get("expires"))
    exp = None
    if raw:
        exp = ui.parse_date(str(raw)[:10])
        if exp is None:
            raise HTTPException(400, "date d’expiration invalide (AAAA-MM-JJ)")
    password = body.get("password")
    label = str(body.get("label") or "")
    if password and len(str(password)) < 8:
        raise HTTPException(400, "mot de passe trop court (8 caractères minimum)")
    try:
        ips = _validate_ip_whitelist(str(body.get("ip_whitelist") or ""))
        share = store.create_share(wb_id, label=label, expires_at=exp,
                                   password=str(password) if password else None,
                                   allow_embed=bool(body.get("allow_embed", True)),
                                   iframe_only=bool(body.get("iframe_only", False)),
                                   ip_whitelist=ips,
                                   require_key=bool(body.get("require_key", False)))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(_json_share(request, share), status_code=201)


@router.patch("/api/v1/shares/{share_id}")
def api_update_share(share_id: str, request: Request, body: dict = Body(...),
                     user: dict = Depends(api_user)):
    """Modifie les conditions d'un lien : libellé, expiration, mot de passe, IP, iframe, clé."""
    share = store.get_share_by_id(share_id)
    if not share:
        raise HTTPException(404, "lien introuvable")
    wb = store.get_workbook(user["id"], share["workbook_id"])
    if not wb:
        raise HTTPException(404, "lien introuvable")
    kwargs = {}
    if "label" in body:
        kwargs["label"] = str(body.get("label") or "")
    if "expires_at" in body or "expires" in body:
        raw = body.get("expires_at", body.get("expires"))
        if raw in (None, "", False):
            kwargs["expires_at"] = None
        else:
            exp = ui.parse_date(str(raw)[:10])
            if exp is None:
                raise HTTPException(400, "date d’expiration invalide (AAAA-MM-JJ)")
            kwargs["expires_at"] = exp
    if body.get("remove_password"):
        kwargs["password"] = ""
    elif "password" in body:
        pw = body.get("password")
        kwargs["password"] = str(pw) if pw else ""
    if "allow_embed" in body:
        kwargs["allow_embed"] = bool(body.get("allow_embed"))
    if "iframe_only" in body:
        kwargs["iframe_only"] = bool(body.get("iframe_only"))
    if "require_key" in body:
        kwargs["require_key"] = bool(body.get("require_key"))
    try:
        if "ip_whitelist" in body:
            kwargs["ip_whitelist"] = _validate_ip_whitelist(str(body.get("ip_whitelist") or ""))
        share = store.update_share(share_id, **kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _json_share(request, share)


@router.get("/api/v1/shares")
def api_all_shares(request: Request, user: dict = Depends(api_user)):
    """Liste tous les liens de partage du compte, avec leur classeur."""
    out = []
    for wb in store.list_workbooks(user["id"]):
        for share in store.list_shares(wb["id"]):
            item = _json_share(request, share)
            item["workbook_id"] = wb["id"]
            item["workbook_name"] = wb["name"]
            out.append(item)
    return out


@router.delete("/api/v1/shares/{share_id}")
def api_delete_share(share_id: str, request: Request, user: dict = Depends(api_user)):
    share = store.get_share_by_id(share_id)
    if not share:
        raise HTTPException(404, "lien introuvable")
    wb = store.get_workbook(user["id"], share["workbook_id"])
    if not wb:
        raise HTTPException(404, "lien introuvable")
    store.delete_share(wb["id"], share_id)
    return {"deleted": share_id}
