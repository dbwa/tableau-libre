"""Stockage SQLite et primitives de sécurité (sans dépendance externe)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from pathlib import Path

from core import DATA, DEFAULT_QUOTA

DB_PATH = Path(os.environ.get("HOSTED_DB", str(DATA / "hosted.db")))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  id          TEXT PRIMARY KEY,
  email       TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL DEFAULT '',
  pw_hash     TEXT NOT NULL,
  quota_bytes INTEGER NOT NULL,
  is_admin    INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL,
  last_seen  REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS api_tokens (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  prefix     TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_used  REAL
);
CREATE INDEX IF NOT EXISTS api_tokens_user ON api_tokens(user_id);

CREATE TABLE IF NOT EXISTS workbooks (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  dir         TEXT NOT NULL,
  files       TEXT NOT NULL DEFAULT '[]',
  size_bytes  INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  expires_at  REAL,
  views       INTEGER NOT NULL DEFAULT 0,
  last_viewed REAL
);
CREATE INDEX IF NOT EXISTS workbooks_user ON workbooks(user_id);

CREATE TABLE IF NOT EXISTS shares (
  id          TEXT PRIMARY KEY,
  workbook_id TEXT NOT NULL REFERENCES workbooks(id) ON DELETE CASCADE,
  token       TEXT NOT NULL UNIQUE,
  label       TEXT NOT NULL DEFAULT '',
  pw_hash     TEXT,
  allow_embed INTEGER NOT NULL DEFAULT 1,
  iframe_only INTEGER NOT NULL DEFAULT 0,
  ip_whitelist TEXT,
  url_key     TEXT,
  created_at  REAL NOT NULL,
  expires_at  REAL,
  views       INTEGER NOT NULL DEFAULT 0,
  last_viewed REAL
);
CREATE INDEX IF NOT EXISTS shares_workbook ON shares(workbook_id);

CREATE TABLE IF NOT EXISTS public_workbooks (
  id           TEXT PRIMARY KEY,
  session_hash TEXT NOT NULL,
  name         TEXT NOT NULL,
  dir          TEXT NOT NULL,
  size_bytes   INTEGER NOT NULL DEFAULT 0,
  created_at   REAL NOT NULL,
  last_seen    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS public_session ON public_workbooks(session_hash);
"""


# --------------------------------------------------------------------------- #
# base
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        share_cols = {r["name"] for r in conn.execute("PRAGMA table_info(shares)")}
        if "iframe_only" not in share_cols:
            conn.execute("ALTER TABLE shares ADD COLUMN iframe_only INTEGER NOT NULL DEFAULT 0")
        if "ip_whitelist" not in share_cols:
            conn.execute("ALTER TABLE shares ADD COLUMN ip_whitelist TEXT")
        if "url_key" not in share_cols:
            conn.execute("ALTER TABLE shares ADD COLUMN url_key TEXT")
    try:
        DB_PATH.chmod(0o600)
    except OSError:
        pass


def _now() -> float:
    return time.time()


def _one(sql: str, args: tuple = ()) -> dict | None:
    with connect() as conn:
        row = conn.execute(sql, args).fetchone()
        return dict(row) if row else None


def _all(sql: str, args: tuple = ()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# --------------------------------------------------------------------------- #
# mots de passe
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("mot de passe trop court (8 caractères minimum)")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1${}${}".format(
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n),
                            r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise ValueError("adresse e-mail invalide")
    return email


# --------------------------------------------------------------------------- #
# comptes et sessions
# --------------------------------------------------------------------------- #
def create_user(email: str, password: str, name: str = "") -> dict:
    email = normalize_email(email)
    pw_hash = hash_password(password)
    uid = "u_" + secrets.token_urlsafe(12)
    now = _now()
    try:
        with connect() as conn:
            first = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
            conn.execute(
                "INSERT INTO users(id,email,name,pw_hash,quota_bytes,is_admin,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (uid, email, (name or "").strip()[:80], pw_hash, DEFAULT_QUOTA,
                 1 if first else 0, now))
    except sqlite3.IntegrityError as exc:
        raise ValueError("un compte existe déjà avec cette adresse") from exc
    return get_user(uid)


def get_user(user_id: str) -> dict | None:
    return _one("SELECT * FROM users WHERE id=?", (user_id,))


def get_user_by_email(email: str) -> dict | None:
    return _one("SELECT * FROM users WHERE email=?", (normalize_email(email),))


def authenticate(email: str, password: str) -> dict | None:
    try:
        user = get_user_by_email(email)
    except ValueError:
        return None
    if user and verify_password(password, user["pw_hash"]):
        return user
    return None


def set_password(user_id: str, password: str) -> None:
    pw_hash = hash_password(password)
    with connect() as conn:
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, user_id))


def delete_user(user_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def list_users() -> list[dict]:
    return _all(
        "SELECT u.*, COUNT(w.id) AS workbook_count,"
        " COALESCE(SUM(w.size_bytes),0) AS used_bytes"
        " FROM users u LEFT JOIN workbooks w ON w.user_id=u.id"
        " GROUP BY u.id ORDER BY u.created_at")


def set_user_quota(user_id: str, quota_bytes: int) -> None:
    if quota_bytes <= 0:
        raise ValueError("quota invalide")
    with connect() as conn:
        conn.execute("UPDATE users SET quota_bytes=? WHERE id=?", (quota_bytes, user_id))


def set_user_admin(user_id: str, is_admin: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin else 0, user_id))


def admin_stats() -> dict:
    with connect() as conn:
        users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        workbooks = conn.execute("SELECT COUNT(*) AS n FROM workbooks").fetchone()["n"]
        storage = conn.execute("SELECT COALESCE(SUM(size_bytes),0) AS n FROM workbooks").fetchone()["n"]
        shares = conn.execute("SELECT COUNT(*) AS n FROM shares").fetchone()["n"]
        tokens = conn.execute("SELECT COUNT(*) AS n FROM api_tokens").fetchone()["n"]
        public = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS b FROM public_workbooks").fetchone()
    return {"users": int(users), "workbooks": int(workbooks), "storage": int(storage),
            "shares": int(shares), "tokens": int(tokens),
            "public": int(public["n"]), "public_storage": int(public["b"])}


def all_workbooks() -> list[dict]:
    return _all(
        "SELECT w.*, u.email AS owner_email,"
        " (SELECT COUNT(*) FROM shares s WHERE s.workbook_id=w.id) AS share_count"
        " FROM workbooks w JOIN users u ON u.id=w.user_id"
        " ORDER BY w.updated_at DESC LIMIT 200")


def create_session(user_id: str, days: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,last_seen,expires_at) VALUES(?,?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), user_id, now, now, now + days * 86400))
    return token


def get_session_user(token: str | None) -> dict | None:
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    row = _one("SELECT * FROM sessions WHERE token_hash=?", (th,))
    if not row:
        return None
    now = _now()
    if row["expires_at"] < now:
        delete_session(token)
        return None
    with connect() as conn:
        conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, th))
    return get_user(row["user_id"])


def delete_session(token: str | None) -> None:
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?",
                     (hashlib.sha256(token.encode()).hexdigest(),))


def delete_user_sessions(user_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


# --------------------------------------------------------------------------- #
# jetons d'API
# --------------------------------------------------------------------------- #
def create_api_token(user_id: str, name: str) -> tuple[dict, str]:
    token = "tbr_" + secrets.token_urlsafe(32)
    row = {
        "id": "k_" + secrets.token_urlsafe(10),
        "user_id": user_id,
        "name": (name or "API").strip()[:60],
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "prefix": token[:12],
        "created_at": _now(),
        "last_used": None,
    }
    with connect() as conn:
        conn.execute(
            "INSERT INTO api_tokens(id,user_id,name,token_hash,prefix,created_at,last_used)"
            " VALUES(?,?,?,?,?,?,?)",
            (row["id"], row["user_id"], row["name"], row["token_hash"],
             row["prefix"], row["created_at"], None))
    return row, token


def list_api_tokens(user_id: str) -> list[dict]:
    return _all("SELECT id,name,prefix,created_at,last_used FROM api_tokens"
                " WHERE user_id=? ORDER BY created_at DESC", (user_id,))


def delete_api_token(user_id: str, token_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM api_tokens WHERE id=? AND user_id=?", (token_id, user_id))


def user_by_api_token(token: str | None) -> dict | None:
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    row = _one("SELECT * FROM api_tokens WHERE token_hash=?", (th,))
    if not row:
        return None
    with connect() as conn:
        conn.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (_now(), row["id"]))
    return get_user(row["user_id"])


# --------------------------------------------------------------------------- #
# classeurs hébergés
# --------------------------------------------------------------------------- #
def user_used(user_id: str) -> int:
    row = _one("SELECT COALESCE(SUM(size_bytes),0) AS n FROM workbooks WHERE user_id=?", (user_id,))
    return int(row["n"]) if row else 0


def list_workbooks(user_id: str) -> list[dict]:
    rows = _all(
        "SELECT w.*, (SELECT COUNT(*) FROM shares s WHERE s.workbook_id=w.id) AS share_count"
        " FROM workbooks w WHERE w.user_id=? ORDER BY w.updated_at DESC", (user_id,))
    for r in rows:
        r["files"] = json.loads(r["files"] or "[]")
    return rows


def get_workbook(user_id: str, workbook_id: str) -> dict | None:
    row = _one("SELECT * FROM workbooks WHERE id=? AND user_id=?", (workbook_id, user_id))
    if row:
        row["files"] = json.loads(row["files"] or "[]")
    return row


def get_workbook_any(workbook_id: str) -> dict | None:
    row = _one("SELECT * FROM workbooks WHERE id=?", (workbook_id,))
    if row:
        row["files"] = json.loads(row["files"] or "[]")
    return row


def create_workbook(user_id: str, workbook_id: str, name: str, directory: str,
                    files: list[str], size: int, expires_at: float | None = None) -> dict:
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO workbooks(id,user_id,name,dir,files,size_bytes,created_at,updated_at,expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (workbook_id, user_id, name[:120], directory, json.dumps(files), size, now, now, expires_at))
    return get_workbook(user_id, workbook_id)


def update_workbook(user_id: str, workbook_id: str, *,
                    name: str | None = None, files: list[str] | None = None,
                    size: int | None = None, expires_at: float | None = None,
                    touch: bool = True) -> None:
    sets, args = [], []
    if name is not None:
        sets.append("name=?")
        args.append(name[:120])
    if files is not None:
        sets.append("files=?")
        args.append(json.dumps(files))
    if size is not None:
        sets.append("size_bytes=?")
        args.append(size)
    if expires_at is not None:
        sets.append("expires_at=?")
        args.append(expires_at if expires_at > 0 else None)
    if touch:
        sets.append("updated_at=?")
        args.append(_now())
    if not sets:
        return
    args.extend([workbook_id, user_id])
    with connect() as conn:
        conn.execute(f"UPDATE workbooks SET {', '.join(sets)} WHERE id=? AND user_id=?", args)


def delete_workbook(user_id: str, workbook_id: str) -> dict | None:
    row = get_workbook(user_id, workbook_id)
    if not row:
        return None
    with connect() as conn:
        conn.execute("DELETE FROM workbooks WHERE id=? AND user_id=?", (workbook_id, user_id))
    return row


def delete_workbook_any(workbook_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM workbooks WHERE id=?", (workbook_id,))


def touch_workbook(workbook_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE workbooks SET views=views+1,last_viewed=? WHERE id=?",
                     (_now(), workbook_id))


def expired_workbooks(now: float | None = None) -> list[dict]:
    now = now or _now()
    return _all("SELECT * FROM workbooks WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))


def all_workbook_dirs() -> set[str]:
    return {r["dir"] for r in _all("SELECT dir FROM workbooks")}


# --------------------------------------------------------------------------- #
# partages
# --------------------------------------------------------------------------- #
def _slug(label: str) -> str:
    text = unicodedata.normalize("NFKD", label or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:40] or "lien"


def create_share(workbook_id: str, *, label: str = "", expires_at: float | None = None,
                 password: str | None = None, allow_embed: bool = True,
                 iframe_only: bool = False, ip_whitelist: str = "",
                 require_key: bool = False) -> dict:
    label = (label or "").strip()[:80]
    token = secrets.token_urlsafe(12) + ("-" + _slug(label) if label else "")
    sid = "s_" + secrets.token_urlsafe(10)
    pw_hash = hash_password(password) if password else None
    url_key = secrets.token_urlsafe(16) if require_key else None
    if iframe_only:
        allow_embed = True
    with connect() as conn:
        conn.execute(
            "INSERT INTO shares(id,workbook_id,token,label,pw_hash,allow_embed,iframe_only,"
            "ip_whitelist,url_key,created_at,expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, workbook_id, token, label, pw_hash,
             1 if allow_embed else 0, 1 if iframe_only else 0,
             (ip_whitelist or "").strip()[:500], url_key, _now(), expires_at))
    return get_share_by_id(sid)


_UNSET = object()


def update_share(share_id: str, *, label=_UNSET, expires_at=_UNSET, password=_UNSET,
                 allow_embed=None, iframe_only=None, ip_whitelist=None,
                 require_key=None) -> dict | None:
    share = get_share_by_id(share_id)
    if not share:
        return None
    sets, args = [], []
    if label is not _UNSET:
        sets.append("label=?")
        args.append((str(label) if label is not None else "").strip()[:80])
    if expires_at is not _UNSET:
        sets.append("expires_at=?")
        args.append(expires_at or None)
    if password is not _UNSET:
        sets.append("pw_hash=?")
        args.append(hash_password(str(password)) if password else None)
    if allow_embed is not None:
        sets.append("allow_embed=?")
        args.append(1 if allow_embed else 0)
        if not allow_embed:
            sets.append("iframe_only=0")
    if iframe_only is not None:
        sets.append("iframe_only=?")
        args.append(1 if iframe_only else 0)
        if iframe_only:
            sets.append("allow_embed=1")
    if ip_whitelist is not None:
        sets.append("ip_whitelist=?")
        args.append((str(ip_whitelist) or "").strip()[:500])
    if require_key is not None:
        if require_key and not share.get("url_key"):
            sets.append("url_key=?")
            args.append(secrets.token_urlsafe(16))
        elif not require_key:
            sets.append("url_key=NULL")
    if not sets:
        return share
    args.append(share_id)
    with connect() as conn:
        conn.execute(f"UPDATE shares SET {', '.join(sets)} WHERE id=?", args)
    return get_share_by_id(share_id)


def get_share_by_id(share_id: str) -> dict | None:
    return _one("SELECT * FROM shares WHERE id=?", (share_id,))


def get_share_by_token(token: str) -> dict | None:
    return _one("SELECT * FROM shares WHERE token=?", (token,))


def share_with_workbook(token: str) -> dict | None:
    return _one(
        "SELECT s.*, w.name AS workbook_name, w.user_id AS owner_id, w.dir AS workbook_dir,"
        " w.expires_at AS workbook_expires_at"
        " FROM shares s JOIN workbooks w ON w.id=s.workbook_id WHERE s.token=?", (token,))


def list_shares(workbook_id: str) -> list[dict]:
    return _all("SELECT * FROM shares WHERE workbook_id=? ORDER BY created_at DESC", (workbook_id,))


def delete_share(workbook_id: str, share_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM shares WHERE id=? AND workbook_id=?", (share_id, workbook_id))
        return cur.rowcount > 0


def touch_share(share_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE shares SET views=views+1,last_viewed=? WHERE id=?",
                     (_now(), share_id))


# --------------------------------------------------------------------------- #
# lecteur public éphémère
# --------------------------------------------------------------------------- #
def public_add(workbook_id: str, session_hash: str, name: str, directory: str,
               size: int) -> dict:
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO public_workbooks(id,session_hash,name,dir,size_bytes,created_at,last_seen)"
            " VALUES(?,?,?,?,?,?,?)",
            (workbook_id, session_hash, name[:120], directory, size, now, now))
    return public_get(workbook_id)


def public_get(workbook_id: str) -> dict | None:
    return _one("SELECT * FROM public_workbooks WHERE id=?", (workbook_id,))


def public_touch(workbook_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE public_workbooks SET last_seen=? WHERE id=?", (_now(), workbook_id))


def public_list(session_hash: str) -> list[dict]:
    return _all("SELECT * FROM public_workbooks WHERE session_hash=? ORDER BY created_at DESC",
                (session_hash,))


def public_total() -> int:
    row = _one("SELECT COALESCE(SUM(size_bytes),0) AS n FROM public_workbooks")
    return int(row["n"]) if row else 0


def public_delete(workbook_id: str) -> dict | None:
    row = public_get(workbook_id)
    if row:
        with connect() as conn:
            conn.execute("DELETE FROM public_workbooks WHERE id=?", (workbook_id,))
    return row


def public_end_session(session_hash: str) -> list[dict]:
    rows = public_list(session_hash)
    with connect() as conn:
        conn.execute("DELETE FROM public_workbooks WHERE session_hash=?", (session_hash,))
    return rows


def public_expired(now: float | None = None, ttl_hours: float = 4) -> list[dict]:
    now = now or _now()
    return _all("SELECT * FROM public_workbooks WHERE last_seen < ?", (now - ttl_hours * 3600,))


def all_public_dirs() -> set[str]:
    return {r["dir"] for r in _all("SELECT dir FROM public_workbooks")}


def cleanup() -> None:
    now = _now()
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
