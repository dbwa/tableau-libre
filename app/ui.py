"""Pages HTML communes (sans moteur de template externe)."""
from __future__ import annotations

import html
import time


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def csrf_input(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(token)}">'


def fmt_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unit == "To":
            return (f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}")
        n /= 1024
    return ""


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))


def fmt_date(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def parse_date(value: str | None, end_of_day: bool = True) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        t = time.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    ts = time.mktime(t)
    return ts + (86399 if end_of_day else 0)


def iframe_snippet(url: str, height: int = 600) -> str:
    return (f'<iframe src="{url}" width="100%" height="{height}" '
            'style="border:0;border-radius:10px" loading="lazy" allowfullscreen></iframe>')


# --------------------------------------------------------------------------- #
# petites icônes SVG (pas d'emoji, pas de dépendance)
# --------------------------------------------------------------------------- #
_ICONS = {
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "upload": '<path d="M12 16V4m0 0 4 4m-4-4-4 4"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.1.1l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1"/><path d="M14 11a5 5 0 0 0-7.1-.1l-2 2A5 5 0 0 0 12 20l1.1-1.1"/>',
    "key": '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 7.8-7.8M15 5l4 4M18 8l2-2"/>',
    "shield": '<path d="M12 3 5 6v5c0 4.5 3 8.5 7 10 4-1.5 7-5.5 7-10V6Z"/><path d="m9 12 2 2 4-4"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5 8.5 8.5 0 1 0 20.5 14.5Z"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M20 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h4"/>',
    "trash": '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v5M14 11v5"/>',
    "lock": '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    "spark": '<path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z"/>',
    "dot": '<circle cx="12" cy="12" r="1"/>',
}


def icon(name: str, size: int = 18) -> str:
    return (f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" '
            'fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f'{_ICONS.get(name, _ICONS["dot"])}</svg>')


def theme_button() -> str:
    return (
        '<button class="iconbtn theme" type="button" id="theme-toggle" '
        'aria-label="Basculer le thème clair/sombre" title="Thème clair / sombre" '
        'aria-pressed="false">'
        f'<span class="theme-sun">{icon("sun")}</span>'
        f'<span class="theme-moon">{icon("moon")}</span>'
        '</button>'
    )


# --------------------------------------------------------------------------- #
# feuille de style commune
# --------------------------------------------------------------------------- #
CSS = """
@font-face{font-family:'IBM Plex Serif';src:url('/static/fonts/ibm-plex-serif-400.woff2') format('woff2');font-weight:400;font-style:normal;font-display:swap}
@font-face{font-family:'IBM Plex Serif';src:url('/static/fonts/ibm-plex-serif-600.woff2') format('woff2');font-weight:600;font-style:normal;font-display:swap}
@font-face{font-family:'IBM Plex Sans';src:url('/static/fonts/ibm-plex-sans-400.woff2') format('woff2');font-weight:400;font-style:normal;font-display:swap}
@font-face{font-family:'IBM Plex Sans';src:url('/static/fonts/ibm-plex-sans-600.woff2') format('woff2');font-weight:600;font-style:normal;font-display:swap}
@font-face{font-family:'JetBrains Mono';src:url('/static/fonts/jetbrains-mono-400.woff2') format('woff2');font-weight:400;font-style:normal;font-display:swap}
@font-face{font-family:'JetBrains Mono';src:url('/static/fonts/jetbrains-mono-600.woff2') format('woff2');font-weight:600;font-style:normal;font-display:swap}
:root{
  color-scheme:light;
  --font-display:'IBM Plex Serif',Georgia,'Times New Roman',serif;
  --font-body:'IBM Plex Sans','Segoe UI',system-ui,-apple-system,sans-serif;
  --font-mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --bg:#f1e3ca;--surface:#fffaf0;--surface-2:#f8ecd8;--surface-3:#e9d3b5;
  --ink:#2a1a12;--ink-2:#4a3527;--muted:#7b6250;--faint:#a58a73;
  --bd:#c9ad8b;--bd-strong:#2a1a12;
  --accent:#c2410c;--accent-hover:#9a3412;--accent-ink:#fff7ed;
  --accent-soft:#fde7d2;--accent-border:#c2410c;
  --brand:#eab308;--brand-ink:#2a1a12;
  --ok:#166534;--ok-soft:#e7f5e9;--ok-border:#166534;
  --warn:#92400e;--warn-soft:#fef3c7;--warn-border:#92400e;
  --danger:#b91c1c;--danger-soft:#fee2e2;--danger-border:#b91c1c;
  --code-bg:#2a1a12;--code-ink:#f6e7d3;
  --shadow-sm:2px 2px 0 rgba(42,26,18,.16);
  --shadow-md:4px 4px 0 var(--ink);
  --radius:12px;--radius-sm:8px
}
html[data-theme=dark]{
  color-scheme:dark;
  --bg:#111820;--surface:#18212b;--surface-2:#202c39;--surface-3:#293748;
  --ink:#f3e9d8;--ink-2:#d8cbb8;--muted:#a7b4c3;--faint:#7f8ea0;
  --bd:#33465a;--bd-strong:#f3e9d8;
  --accent:#fb923c;--accent-hover:#fdba74;--accent-ink:#111820;
  --accent-soft:#3a2618;--accent-border:#fb923c;
  --brand:#facc15;--brand-ink:#111820;
  --ok:#86efac;--ok-soft:#12351f;--ok-border:#4ade80;
  --warn:#fcd34d;--warn-soft:#3a2a08;--warn-border:#fbbf24;
  --danger:#fca5a5;--danger-soft:#3a1515;--danger-border:#f87171;
  --code-bg:#0b1117;--code-ink:#f6e7d3;
  --shadow-sm:2px 2px 0 rgba(246,234,217,.18);
  --shadow-md:4px 4px 0 rgba(246,234,217,.65)
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
html{background:var(--bg)}
body{font-family:var(--font-body);font-size:15px;line-height:1.55;color:var(--ink);background:var(--bg);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:var(--accent);color:var(--accent-ink)}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,
summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.ic{display:block;flex:0 0 auto}
.wrap{max-width:1180px;margin:0 auto;padding:30px 24px 72px}
.wrap.narrow{max-width:760px}
.top{position:sticky;top:0;z-index:30;background:var(--surface);border-bottom:2px solid var(--ink);
  box-shadow:0 4px 0 var(--accent)}
.top .inner{max-width:1180px;margin:0 auto;padding:12px 24px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.brand{font-weight:700;font-size:15px;color:var(--ink);display:flex;align-items:center;gap:10px}
.brand:hover{text-decoration:none}
.brand .mark{width:34px;height:34px;border-radius:8px;background:var(--brand);color:var(--brand-ink);
  border:2px solid var(--ink);box-shadow:2px 2px 0 var(--ink);display:grid;place-items:center;flex:0 0 auto}
.brand-text{display:flex;flex-direction:column;line-height:1.05}
.brand-name{font-family:var(--font-display);font-size:20px;font-weight:600;line-height:1}
.top nav{margin-left:auto;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.navlink{padding:8px 10px;border-radius:6px;color:var(--ink-2);font-size:13px;font-weight:700}
.navlink:hover{background:var(--surface-2);color:var(--ink);text-decoration:none}
.who{font-family:var(--font-mono);color:var(--muted);font-size:11px;max-width:220px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;padding:0 6px}
.iconbtn{width:38px;height:38px;display:inline-grid;place-items:center;border:2px solid var(--ink);
  border-radius:8px;background:var(--surface);color:var(--ink-2);cursor:pointer;padding:0;
  box-shadow:2px 2px 0 var(--ink);transition:transform .12s,box-shadow .12s,background .12s}
.iconbtn:hover{background:var(--surface-2);color:var(--ink);transform:translate(1px,1px);box-shadow:1px 1px 0 var(--ink)}
.theme .theme-moon{display:none}
html[data-theme=dark] .theme .theme-sun{display:none}
html[data-theme=dark] .theme .theme-moon{display:block}
.theme-wash{position:fixed;inset:0;z-index:9999;pointer-events:none;background:var(--bg);
  box-shadow:-4px 0 0 var(--accent);transform:translateX(0);
  transition:transform .38s cubic-bezier(.4,0,.2,1);will-change:transform}
h1,h2,h3,.brand-name{font-family:var(--font-display);font-weight:600;text-wrap:balance}
p{text-wrap:pretty}
h1{font-size:clamp(34px,4.4vw,48px);line-height:1.08;margin:0 0 10px}
h2{font-size:24px;line-height:1.12;margin:0 0 6px}
h3{font-size:17px;line-height:1.18;margin:0 0 8px}
.lede{color:var(--muted);margin:0 0 22px;max-width:700px;font-size:16px}
.eyebrow{display:inline-flex;align-items:center;gap:7px;color:var(--ink);background:var(--accent-soft);
  border:1.5px solid var(--ink);border-radius:999px;padding:5px 10px;font-family:var(--font-mono);
  font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px;
  box-shadow:2px 2px 0 var(--ink)}
.eyebrow .ic{width:14px;height:14px;color:var(--accent)}
.hero{padding:44px 0 8px}
.hero h1{max-width:860px;font-size:clamp(38px,4.8vw,60px);line-height:1.06}
.hero .lede{font-size:17px}
.hero-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:20px}
.hero-points{display:flex;gap:16px;flex-wrap:wrap;margin-top:18px;color:var(--muted);font-size:13px}
.hero-points span{display:inline-flex;align-items:center;gap:6px}
.hero-points .ic{width:15px;height:15px;color:var(--ok)}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:20px}
.home-upload{padding:28px}
.home-upload .drop{padding:34px 24px}
.format-help{margin:14px 0 0;font-size:12px;color:var(--muted);line-height:1.7}
.format-help b{color:var(--ink-2)}
code{font-family:var(--font-mono);font-size:.92em;background:var(--surface-2);border:1px solid var(--bd);
  border-radius:4px;padding:1px 4px;color:var(--ink)}
.file-list{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 12px}
.managed-files{display:grid;gap:8px;margin:10px 0 16px}
.managed-file{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;
  padding:10px;border:1.5px solid var(--ink);border-radius:8px;background:var(--surface-2);
  box-shadow:1.5px 1.5px 0 var(--ink)}
.managed-file-name{display:flex;align-items:center;gap:8px;min-width:0}
.managed-file-name code{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:320px}
.managed-file-name .small{display:block;margin-top:2px}
.managed-file-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.managed-file-actions form{display:flex;gap:6px;align-items:center;margin:0}
.managed-file-actions input[type=file]{max-width:220px;padding:5px;font-size:11px}
.sr-file{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.file-add{margin-top:12px}
.quotaform{display:flex;gap:6px;align-items:center;margin:0}
.quotaform input{width:80px;min-height:34px;padding:5px 7px}
.host-strip{display:flex;gap:24px;align-items:center;flex-wrap:wrap;margin-top:20px;padding:20px 24px}
.host-copy{flex:1 1 280px}
.host-copy h2{font-size:22px;margin-bottom:4px}
.host-copy .hint{margin-bottom:0}
.host-benefits{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;flex:2 1 520px}
.host-benefit{display:flex;gap:10px;align-items:flex-start}
.host-benefit b{display:block;font-size:13px;font-weight:700;margin-bottom:1px}
.host-benefit span{display:block;font-size:12px;color:var(--muted);line-height:1.45}
.step{display:flex;gap:12px;align-items:flex-start;padding:16px;background:var(--surface);
  border:2px solid var(--ink);border-radius:10px;box-shadow:2px 2px 0 var(--ink)}
.step-num{width:30px;height:30px;border-radius:50%;background:var(--brand);color:var(--brand-ink);
  border:2px solid var(--ink);display:grid;place-items:center;font-family:var(--font-mono);
  font-size:11px;font-weight:600;flex:0 0 auto}
.step b{display:block;font-size:13px;font-weight:700;margin-bottom:1px}
.step div span{display:block;font-size:12px;color:var(--muted);line-height:1.45}
@media(max-width:900px){.steps{grid-template-columns:1fr}.host-benefits{grid-template-columns:1fr}}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:20px 0;align-items:start}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;align-items:start}
@media(max-width:900px){.grid2,.grid3{grid-template-columns:1fr}}
.card{background:var(--surface);border:1.5px solid var(--ink);border-radius:var(--radius);padding:24px;
  box-shadow:3px 3px 0 var(--ink)}
.card.tight{padding:18px}
.card.featured{border:2px solid var(--ink);box-shadow:5px 5px 0 var(--accent)}
.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px 16px;flex-wrap:wrap;margin-bottom:14px}
.card-head h2{margin:0}
.card h2{display:flex;align-items:center;gap:8px}
.card .hint{color:var(--muted);font-size:13px;margin:0 0 16px;line-height:1.5}
.muted{color:var(--muted)}
.small{font-size:12px}
.mono{font-family:var(--font-mono);font-size:12px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:42px;
  border:2px solid var(--ink);background:var(--accent);color:var(--accent-ink);border-radius:8px;
  padding:9px 14px;font-family:var(--font-body);font-size:14px;font-weight:700;line-height:1.2;
  cursor:pointer;white-space:nowrap;box-shadow:3px 3px 0 var(--ink);
  transition:transform .12s,box-shadow .12s,background .12s,color .12s}
.btn:hover{background:var(--accent-hover);text-decoration:none;color:var(--accent-ink);
  transform:translate(1px,1px);box-shadow:2px 2px 0 var(--ink)}
.btn:active{transform:translate(3px,3px);box-shadow:none}
.btn.sec{background:var(--surface);color:var(--ink);border-color:var(--ink);box-shadow:3px 3px 0 var(--ink)}
.btn.sec:hover{background:var(--surface-2);color:var(--ink);box-shadow:2px 2px 0 var(--ink)}
.btn.ghost{background:transparent;color:var(--ink-2);border-color:transparent;box-shadow:none;padding:7px 9px}
.btn.ghost:hover{background:var(--surface-2);color:var(--ink);transform:none;box-shadow:none}
.btn.ghost.sm{box-shadow:none}
.btn.danger{background:var(--danger-soft);color:var(--danger);border-color:var(--danger);
  box-shadow:3px 3px 0 var(--danger)}
.btn.danger:hover{background:var(--danger);border-color:var(--danger);color:#fff;box-shadow:2px 2px 0 var(--danger)}
.btn.sm{min-height:36px;padding:6px 10px;font-size:12.5px;border-radius:7px;box-shadow:2px 2px 0 var(--ink)}
.btn.sm:hover{box-shadow:1px 1px 0 var(--ink)}
.btn .ic{width:16px;height:16px}
.btn:disabled{opacity:.55;cursor:default;transform:none;box-shadow:none}
.btn:disabled:hover{background:var(--accent);color:var(--accent-ink)}
input[type=text],input[type=email],input[type=password],input[type=date],input[type=number],
input[type=search],select,textarea{width:100%;font-family:var(--font-body);font-size:14px;padding:9px 11px;
  border:2px solid var(--ink);border-radius:8px;background:var(--surface);color:var(--ink);min-height:42px}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:2px;border-color:var(--ink)}
input[type=checkbox]{width:16px;height:16px;accent-color:var(--accent);vertical-align:middle}
input[type=file]{width:100%;font-family:var(--font-body);font-size:13px;padding:9px;border:2px dashed var(--ink);
  border-radius:8px;background:var(--surface);color:var(--ink);cursor:pointer}
input[type=file]::file-selector-button{font-family:var(--font-mono);font-size:11px;font-weight:600;
  text-transform:uppercase;margin-right:10px;padding:6px 10px;border:2px solid var(--ink);border-radius:6px;
  background:var(--brand);color:var(--brand-ink);cursor:pointer;box-shadow:2px 2px 0 var(--ink)}
label{display:block;font-family:var(--font-mono);font-size:11px;color:var(--ink-2);margin:12px 0 5px;
  font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.formrow{display:flex;gap:12px;flex-wrap:wrap}
.formrow>*{flex:1 1 160px}
.checkline{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--ink);font-weight:400;margin-top:12px}
.checkline input{width:16px;height:16px;flex:0 0 auto}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--bd);vertical-align:middle}
th{font-family:var(--font-mono);font-size:10.5px;font-weight:600;color:var(--ink-2);background:var(--surface-2);
  border-bottom:2px solid var(--ink);text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--surface-2)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.tablewrap{overflow-x:auto;border:2px solid var(--ink);border-radius:9px;box-shadow:2px 2px 0 var(--ink)}
.tablewrap table{min-width:640px}
.tablewrap th:first-child,.tablewrap td:first-child{padding-left:16px}
.tablewrap th:last-child,.tablewrap td:last-child{padding-right:16px}
.codebox{background:var(--code-bg);color:var(--code-ink);border:2px solid var(--ink);border-radius:8px;
  padding:12px 14px;font-family:var(--font-mono);font-size:11.5px;line-height:1.55;overflow:auto;
  white-space:pre-wrap;word-break:break-all;margin:8px 0;box-shadow:2px 2px 0 var(--ink)}
.badge{display:inline-flex;align-items:center;gap:4px;border:1.5px solid var(--ink);border-radius:999px;
  padding:3px 9px;font-family:var(--font-mono);font-size:10px;font-weight:600;text-transform:uppercase;
  letter-spacing:.04em;color:var(--ink-2);background:var(--surface);white-space:nowrap;box-shadow:1.5px 1.5px 0 var(--ink)}
.badge.ok{color:var(--ok);border-color:var(--ok);background:var(--ok-soft)}
.badge.warn{color:var(--warn);border-color:var(--warn);background:var(--warn-soft)}
.badge.danger{color:var(--danger);border-color:var(--danger);background:var(--danger-soft)}
.badge.blue{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}
.err,.okbox{padding:11px 13px;border-radius:8px;margin:0 0 16px;font-size:14px;border:2px solid}
.err{background:var(--danger-soft);border-color:var(--danger);color:var(--danger)}
.okbox{background:var(--brand);border-color:var(--ink);color:var(--brand-ink);box-shadow:2px 2px 0 var(--ink)}
.quota{height:12px;background:var(--surface-3);border:2px solid var(--ink);border-radius:6px;overflow:hidden;margin:10px 0 5px}
.quota i{display:block;height:100%;background:var(--accent);border-radius:3px}
.empty{padding:42px 20px;text-align:center;color:var(--muted);font-size:14px}
.empty .empty-icon{width:46px;height:46px;border-radius:9px;background:var(--surface-2);border:2px solid var(--ink);
  box-shadow:2px 2px 0 var(--ink);color:var(--muted);display:grid;place-items:center;margin:0 auto 12px}
.empty b{display:block;color:var(--ink);font-size:16px;margin-bottom:4px}
.empty span{display:block;margin-bottom:4px}
.empty .btn{display:inline-flex;margin-top:16px}
.drop{border:2px dashed var(--ink);border-radius:9px;padding:26px 20px;text-align:center;
  background:var(--surface-2);box-shadow:3px 3px 0 var(--ink);transition:border-color .15s,background .15s,box-shadow .15s}
.drop:hover,.drop.drag{border-color:var(--accent);background:var(--accent-soft);box-shadow:3px 3px 0 var(--accent)}
.drop .drop-icon{width:48px;height:48px;border-radius:9px;background:var(--surface);color:var(--accent);
  border:2px solid var(--ink);box-shadow:2px 2px 0 var(--ink);display:grid;place-items:center;margin:0 auto 10px}
.drop p{margin:0 0 8px}
.drop input[type=file]{max-width:420px;margin:8px auto 0;text-align:left}
.selected-files{max-width:560px;margin:12px auto 0;text-align:left;display:grid;gap:6px;max-height:220px;overflow:auto}
.selected-files[hidden]{display:none}
.selected-files-head{display:flex;align-items:center;gap:8px;font-family:var(--font-mono);font-size:9.5px;
  text-transform:uppercase;letter-spacing:.04em;color:var(--muted);padding:0 2px}
.selected-files-head .add,.selected-files-head .clear{border:0;background:none;color:var(--accent);font:inherit;
  text-decoration:underline;cursor:pointer;padding:0}
.selected-files-head .add{margin-left:auto}
.selected-files-head .clear{margin-left:8px}
.drop.has-files input[type=file]{display:none}
.selected-file{display:flex;align-items:center;gap:8px;padding:7px 9px;background:var(--surface);
  border:1.5px solid var(--ink);border-radius:6px;box-shadow:1.5px 1.5px 0 var(--ink)}
.selected-file .name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-family:var(--font-mono);font-size:11px}
.selected-file .size{font-family:var(--font-mono);font-size:9.5px;color:var(--muted);white-space:nowrap}
.selected-file .remove{border:0;background:transparent;color:var(--danger);font-size:18px;line-height:1;
  cursor:pointer;padding:0 2px;box-shadow:none}
.selected-file .remove:hover{color:var(--ink)}
.bar{height:10px;background:var(--surface-3);border:2px solid var(--ink);border-radius:5px;overflow:hidden;margin:12px 0 0;display:none}
.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .15s}
.kv{display:grid;grid-template-columns:150px 1fr;gap:8px 14px;font-size:14px}
.kv dt{font-family:var(--font-mono);color:var(--muted);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.kv dd{margin:0}
.share{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:5px;border:1.5px solid var(--ink);border-radius:999px;
  padding:3px 9px;font-family:var(--font-mono);font-size:10px;text-transform:uppercase;letter-spacing:.04em;
  color:var(--muted);background:var(--surface)}
footer{margin-top:44px;color:var(--muted);font-family:var(--font-mono);font-size:10.5px;
  text-transform:uppercase;letter-spacing:.05em;text-align:center}
details summary{cursor:pointer;color:var(--ink-2);font-size:14px;font-weight:700}
details[open] summary{margin-bottom:16px}
hr{border:0;border-top:1px dashed var(--bd);margin:22px 0}
.pagehead{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap;margin-bottom:22px}
.pagehead h1{font-size:clamp(30px,3.4vw,40px);margin-bottom:4px}
.pagehead .lede{margin:0}
.backlink{display:inline-flex;align-items:center;gap:6px;font-family:var(--font-mono);font-size:10.5px;
  text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:16px}
.backlink:hover{color:var(--accent);text-decoration:none}
.backlink .ic{width:15px;height:15px;transform:rotate(180deg)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}
@media(max-width:900px){.stats{grid-template-columns:1fr 1fr}}
@media(max-width:520px){.stats{grid-template-columns:1fr}}
.stat{background:var(--surface);border:2px solid var(--ink);border-radius:10px;padding:16px 18px;
  box-shadow:3px 3px 0 var(--ink)}
.stat .label{display:flex;align-items:center;gap:7px;font-family:var(--font-mono);font-size:10.5px;
  color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.stat .value{font-family:var(--font-display);font-size:30px;font-weight:600;font-variant-numeric:tabular-nums;
  margin-top:8px;line-height:1}
.stat .meta{font-size:12px;color:var(--muted);margin-top:4px}
.stat .quota{margin-top:10px}
.workbook-cell{display:flex;align-items:center;gap:10px}
.file-icon{width:34px;height:34px;border-radius:7px;background:var(--accent-soft);color:var(--accent);
  border:2px solid var(--ink);box-shadow:1.5px 1.5px 0 var(--ink);display:grid;place-items:center;flex:0 0 auto}
.feature{display:flex;gap:12px;padding:12px 0;border-bottom:1px dashed var(--bd)}
.feature:last-child{border-bottom:0;padding-bottom:0}
.feature-icon{width:36px;height:36px;border-radius:8px;background:var(--accent-soft);color:var(--accent);
  border:2px solid var(--ink);box-shadow:2px 2px 0 var(--ink);display:grid;place-items:center;flex:0 0 auto}
.feature b{display:block;font-size:14px;font-weight:700;margin-bottom:1px}
.feature span{display:block;font-size:13px;color:var(--muted);line-height:1.45}
.details{box-shadow:3px 3px 0 var(--ink)}
.details>summary{list-style:none;display:flex;align-items:center;gap:10px;margin:0;flex-wrap:wrap}
.details>summary::-webkit-details-marker{display:none}
.details>summary .summary-meta{margin-left:auto;color:var(--muted);font-family:var(--font-mono);font-size:10px;
  font-weight:400;text-transform:uppercase;letter-spacing:.04em}
.details>summary .chev{width:16px;height:16px;margin-left:2px;color:var(--muted);transition:transform .15s}
.details[open]>summary .chev{transform:rotate(90deg)}
.details-body{padding-top:2px}
.auth{max-width:460px;margin:44px auto 0}
.auth .card{padding:28px}
.auth .auth-head{text-align:center;margin-bottom:20px}
.auth .auth-mark{width:48px;height:48px;border-radius:10px;background:var(--brand);color:var(--brand-ink);
  border:2px solid var(--ink);box-shadow:3px 3px 0 var(--ink);display:grid;place-items:center;margin:0 auto 12px}
.auth h1{font-size:34px;margin-bottom:4px}
.auth .lede{font-size:14px;margin:0}
.auth .auth-switch{text-align:center;font-size:13px;color:var(--muted);margin-top:18px}
.auth .auth-alt{text-align:center;font-size:12px;color:var(--muted);margin:16px 0 0}
.error-card{max-width:620px;margin:48px auto;text-align:center}
.error-card .empty-icon{width:54px;height:54px;margin-bottom:14px}
.error-card .actions{justify-content:center}
dialog.modal{border:2px solid var(--ink);border-radius:12px;padding:0;background:var(--surface);
  color:var(--ink);max-width:440px;width:calc(100% - 32px);box-shadow:6px 6px 0 var(--ink)}
dialog.modal::backdrop{background:rgba(17,24,32,.55)}
.modal-card{padding:24px}
.modal-card h2{margin-bottom:8px}
.modal-card p{color:var(--muted);margin:0 0 20px}
@media(max-width:720px){
  .wrap{padding:22px 16px 56px}
  .top .inner{padding:10px 16px;gap:10px}
  .who{display:none}
  .hero{padding:30px 0 4px}
  .card{padding:18px}
  .grid2,.grid3{gap:14px}
  .tablewrap table{min-width:560px}
  .auth{margin-top:24px}
  .btn{min-height:44px}
  .btn.sm{min-height:40px}
  .iconbtn{width:42px;height:42px}
  input[type=text],input[type=email],input[type=password],input[type=date],input[type=number],input[type=search],select,textarea{min-height:44px}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}
}
"""

_THEME_BOOT = """<script>
try{
  var saved=localStorage.getItem('tableau-libre-theme');
  var dark=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme=saved||(dark?'dark':'light');
}catch(e){}
</script>"""

_THEME_SCRIPT = """<script>
(function(){
  var btn=document.getElementById('theme-toggle');
  if(!btn)return;
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function sync(){
    var dark=document.documentElement.dataset.theme==='dark';
    btn.setAttribute('aria-pressed',dark?'true':'false');
    btn.title=dark?'Passer en thème clair':'Passer en thème sombre';
  }
  function apply(next){
    var root=document.documentElement;
    var old=getComputedStyle(root).backgroundColor;
    if(!old || old==='rgba(0, 0, 0, 0)') old=getComputedStyle(document.body).backgroundColor;
    var previous=document.querySelector('.theme-wash');
    if(previous) previous.remove();
    root.dataset.theme=next;
    try{localStorage.setItem('tableau-libre-theme',next);}catch(e){}
    sync();
    if(reduce) return;
    var wash=document.createElement('div');
    wash.className='theme-wash';
    wash.setAttribute('aria-hidden','true');
    wash.style.background=old;
    document.body.appendChild(wash);
    wash.getBoundingClientRect();
    wash.style.transform='translateX(100%)';
    var done=function(){ if(wash.parentNode) wash.parentNode.removeChild(wash); };
    wash.addEventListener('transitionend',done,{once:true});
    setTimeout(done,600);
  }
  sync();
  btn.addEventListener('click',function(){
    apply(document.documentElement.dataset.theme==='dark'?'light':'dark');
  });
})();
</script>"""


_FILES_SCRIPT = """<script>
(function(){
  function fmt(n){
    if(n < 1024) return n + ' o';
    if(n < 1024*1024) return (n/1024).toFixed(1) + ' Ko';
    if(n < 1024*1024*1024) return (n/1024/1024).toFixed(1) + ' Mo';
    return (n/1024/1024/1024).toFixed(1) + ' Go';
  }
  function init(drop){
    var input=drop.querySelector('input[type=file]');
    var list=drop.querySelector('[data-file-list]');
    if(!input || !list) return;
    var dt=new DataTransfer();
    function render(){
      while(list.firstChild) list.removeChild(list.firstChild);
      var n=dt.files.length;
      list.hidden = n===0;
      drop.classList.toggle('has-files', n>0);
      if(n){
        var head=document.createElement('div'); head.className='selected-files-head';
        var count=document.createElement('span');
        count.textContent=n+' fichier'+(n>1?'s':'')+' sélectionné'+(n>1?'s':'');
        head.appendChild(count);
        var addBtn=document.createElement('button'); addBtn.type='button'; addBtn.className='add';
        addBtn.textContent='Ajouter des fichiers'; addBtn.addEventListener('click',function(){ input.click(); });
        head.appendChild(addBtn);
        if(n>1){
          var clear=document.createElement('button'); clear.type='button'; clear.className='clear';
          clear.textContent='Tout retirer';
          clear.addEventListener('click',function(){ dt=new DataTransfer(); render(); });
          head.appendChild(clear);
        }
        list.appendChild(head);
        for(var i=0;i<n;i++){
          (function(index,file){
            var row=document.createElement('div'); row.className='selected-file';
            var name=document.createElement('span'); name.className='name';
            name.textContent=file.name; name.title=file.name;
            var size=document.createElement('span'); size.className='size';
            size.textContent=fmt(file.size);
            var rm=document.createElement('button'); rm.type='button'; rm.className='remove';
            rm.setAttribute('aria-label','Retirer '+file.name); rm.textContent='×';
            rm.addEventListener('click',function(){
              var next=new DataTransfer();
              for(var j=0;j<dt.files.length;j++){ if(j!==index) next.items.add(dt.files[j]); }
              dt=next; render();
            });
            row.appendChild(name); row.appendChild(size); row.appendChild(rm); list.appendChild(row);
          })(i,dt.files[i]);
        }
      }
      input.files=dt.files;
    }
    function add(files){
      var seen={};
      for(var j=0;j<dt.files.length;j++) seen[dt.files[j].name]=true;
      for(var i=0;i<files.length;i++){
        if(!seen[files[i].name]){ dt.items.add(files[i]); seen[files[i].name]=true; }
      }
      render();
    }
    input.addEventListener('change',function(){
      var files=[];
      for(var k=0;k<input.files.length;k++) files.push(input.files[k]);
      if(files.length){ input.value=''; add(files); }
    });
    drop.addEventListener('dragover',function(e){e.preventDefault();drop.classList.add('drag');});
    drop.addEventListener('dragleave',function(){drop.classList.remove('drag');});
    drop.addEventListener('drop',function(e){
      e.preventDefault(); drop.classList.remove('drag');
      if(e.dataTransfer.files.length) add(e.dataTransfer.files);
    });
    render();
  }
  document.addEventListener('change',function(e){
    var input=e.target.closest('input[type=file][data-auto-submit]');
    if(input && input.files.length && input.form){
      if(input.form.requestSubmit) input.form.requestSubmit(); else input.form.submit();
    }
  });
  var drops=document.querySelectorAll('.drop');
  for(var i=0;i<drops.length;i++) init(drops[i]);
})();
</script>"""


_MODAL_SCRIPT = """<script>
(function(){
  var dlg=document.getElementById('confirm-dialog');
  if(!dlg || !dlg.showModal) return;
  var pending=null;
  document.addEventListener('submit',function(e){
    var form=e.target.closest('form[data-confirm]');
    if(!form || form.dataset.confirmed==='1') return;
    e.preventDefault();
    pending=form;
    dlg.querySelector('[data-confirm-message]').textContent=form.dataset.confirm;
    dlg.showModal();
  });
  dlg.querySelector('[data-confirm-cancel]').addEventListener('click',function(){pending=null;dlg.close();});
  dlg.querySelector('[data-confirm-ok]').addEventListener('click',function(){
    if(!pending) return;
    var form=pending; pending=null; form.dataset.confirmed='1'; dlg.close();
    if(form.requestSubmit) form.requestSubmit(); else form.submit();
    setTimeout(function(){delete form.dataset.confirmed;},0);
  });
  dlg.addEventListener('cancel',function(){pending=null;});
})();
</script>"""


def _nav(user: dict | None, csrf: str) -> str:
    theme = theme_button()
    if user:
        return (
            '<nav>'
            '<a class="navlink" href="/account">Mes classeurs</a>'
            + ('<a class="navlink" href="/admin">Admin</a>' if user.get("is_admin") else '') +
            f'<span class="who" title="{esc(user["email"])}">{esc(user["email"])}</span>'
            '<form method="post" action="/logout" style="margin:0">'
            f'{csrf_input(csrf)}<button class="btn ghost sm" type="submit">Déconnexion</button>'
            f'</form>{theme}</nav>')
    return ('<nav><a class="navlink" href="/login">Se connecter</a>'
            '<a class="btn sm" href="/signup">Créer un compte</a>'
            f'{theme}</nav>')


def layout(title: str, body: str, user: dict | None = None, csrf: str = "",
           narrow: bool = False, head: str = "") -> str:
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — Tableau Libre</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E%3Crect%20width='32'%20height='32'%20rx='8'%20fill='%23eab308'/%3E%3Cpath%20d='M9%2023V15M16%2023V9M23%2023v-5'%20stroke='%232a1a12'%20stroke-width='3'%20stroke-linecap='round'/%3E%3C/svg%3E">
{_THEME_BOOT}
<style>{CSS}</style>{head}</head><body>
<header class="top"><div class="inner">
  <a class="brand" href="/" aria-label="Tableau Libre, accueil">
    <span class="mark">{icon("chart", 20)}</span>
    <span class="brand-text"><span class="brand-name">Tableau Libre</span></span>
  </a>
  {_nav(user, csrf)}
</div></header>
<main class="wrap{' narrow' if narrow else ''}">{body}
<footer>Lecteur Tableau libre · lecture seule · aucune donnée envoyée à Tableau
 · <a href="/credits">Crédits &amp; licences</a></footer>
</main>
<dialog id="confirm-dialog" class="modal" aria-labelledby="confirm-title">
<div class="modal-card"><h2 id="confirm-title">Confirmer</h2>
<p data-confirm-message></p>
<div class="actions"><button class="btn sec" type="button" data-confirm-cancel>Annuler</button>
<button class="btn danger" type="button" data-confirm-ok>Confirmer</button></div></div>
</dialog>
{_THEME_SCRIPT}{_FILES_SCRIPT}{_MODAL_SCRIPT}</body></html>"""


def error_page(status: int, title: str, message: str, user: dict | None = None,
               csrf: str = "") -> str:
    body = f"""<div class="card error-card">
<div class="empty-icon">{icon("lock" if status in (401, 403) else "chart", 24)}</div>
<h1>{esc(title)}</h1><p class="lede" style="margin:0 auto 20px">{esc(message)}</p>
<div class="actions"><a class="btn" href="/">Retour à l'accueil</a>
<a class="btn sec" href="/account">Mes classeurs</a></div></div>"""
    return layout(title, body, user, csrf, narrow=True)
