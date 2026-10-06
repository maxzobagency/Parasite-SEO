"""Password-protected web UI: paste keywords, enter DataForSEO details, click Check, watch progress.

    APP_PASSWORD=secret DB_PATH=/var/data/parasite.db python -m parasite_tracker.webapp
Standard library only (no Flask needed).
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import os
import secrets
import sys
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import analysis, db, live
from .config import config_from_db, parse_keywords_text
from .dataforseo import DataForSEO
from .detector import host_of, root_domain
from .report import render_html

DB_PATH = os.environ.get("DB_PATH", "parasite.db")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_SECONDS = 14 * 24 * 3600
e = html.escape

# DataForSEO location code -> (name, default language). Pick any; the language fills in automatically.
LOCATIONS = {
    2840: ("United States", "en"), 2826: ("United Kingdom", "en"), 2124: ("Canada", "en"),
    2036: ("Australia", "en"), 2554: ("New Zealand", "en"), 2372: ("Ireland", "en"),
    2356: ("India", "en"), 2710: ("South Africa", "en"), 2702: ("Singapore", "en"),
    2608: ("Philippines", "en"), 2586: ("Pakistan", "en"), 2566: ("Nigeria", "en"),
    2404: ("Kenya", "en"), 2458: ("Malaysia", "en"), 2344: ("Hong Kong", "en"),
    2784: ("United Arab Emirates", "en"), 2276: ("Germany", "de"), 2040: ("Austria", "de"),
    2756: ("Switzerland", "de"), 2250: ("France", "fr"), 2056: ("Belgium", "fr"),
    2724: ("Spain", "es"), 2484: ("Mexico", "es"), 2032: ("Argentina", "es"),
    2170: ("Colombia", "es"), 2152: ("Chile", "es"), 2380: ("Italy", "it"),
    2528: ("Netherlands", "nl"), 2076: ("Brazil", "pt"), 2620: ("Portugal", "pt"),
    2752: ("Sweden", "sv"), 2578: ("Norway", "no"), 2208: ("Denmark", "da"),
    2616: ("Poland", "pl"), 2792: ("Turkey", "tr"), 2360: ("Indonesia", "id"),
    2764: ("Thailand", "th"), 2704: ("Vietnam", "vi"), 2392: ("Japan", "ja"),
    2410: ("South Korea", "ko"), 2682: ("Saudi Arabia", "ar"), 2818: ("Egypt", "ar")}
CATEGORIES = {
    "ugc_blog": "Free blogs (Medium, Blogspot, Substack, WordPress.com…)",
    "forum_qa": "Forums & Q&A (Reddit, Quora…)",
    "docs_hosting": "Document hosting (Google Docs, Scribd, SlideShare…)",
    "code_static_hosting": "Free site hosting (github.io, netlify.app, pages.dev…)",
    "professional_social": "Profiles/articles (LinkedIn Pulse, Pinterest, about.me…)",
    "news_contributor": "Contributor & press-release sites (Forbes Councils, PRNewswire…)",
    "social_video": "Social & video (YouTube, Facebook, TikTok…) – not classic parasites",
    "suspected": "Smart detection: unknown sites whose URL/title look user-generated (shown with ?)",
    "edu_gov": "Hijacked .edu / .gov pages (low confidence)",
}

CSS = """
:root{--bg:#fff;--fg:#1a1d23;--mut:#6b7280;--card:#f6f7f9;--line:#e3e6ea;--acc:#5b4bdb;--good:#15803d;--bad:#b91c1c}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8eaed;--mut:#9aa0aa;--card:#1d2026;--line:#2c3038;--acc:#8b7dff;--good:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
nav{display:flex;gap:4px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line);padding:8px 16px}
nav b{margin-right:12px}nav a{color:var(--fg);text-decoration:none;padding:5px 10px;border-radius:6px}
nav a.on{background:var(--card);font-weight:600}nav form{margin-left:auto}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}h1{font-size:19px;margin:0 0 12px}h2{font-size:15px;margin:24px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
label{display:block;font-weight:600;margin:10px 0 3px}.hint{color:var(--mut);font-size:12px;font-weight:400}
input,select,textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font:inherit}
textarea{min-height:180px;font-family:ui-monospace,monospace;font-size:13px}
input[type=checkbox],input[type=radio]{width:auto}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
button,.btn{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 16px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
button.sec{background:var(--card);color:var(--fg);border:1px solid var(--line)}button:disabled{opacity:.5}
.msg{padding:9px 12px;border-radius:6px;margin-bottom:12px;background:var(--card);border:1px solid var(--line)}.err{color:var(--bad)}.ok{color:var(--good)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}
.bar{height:10px;background:var(--line);border-radius:5px;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc);width:0;transition:width .4s}
iframe{width:100%;height:1100px;border:1px solid var(--line);border-radius:10px}.cb{font-weight:400;margin:4px 0}
.login{max-width:340px;margin:12vh auto}
"""

# ---------------------------------------------------------------- html helpers
def page(title: str, body: str, active: str = "", flash: str = "", kind: str = "ok") -> str:
    links = [("/", "Search"), ("/settings", "Settings"), ("/domains", "Parasite list")]
    nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{n}</a>' for h, n in links)
    msg = f'<div class="msg {kind}">{e(flash)}</div>' if flash else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{e(title)} · Parasite SERP Tracker</title><style>{CSS}</style></head><body>'
            f'<nav><b>Parasite Tracker</b>{nav}<form method="post" action="/logout"><button class="sec">Log out</button></form></nav>'
            f'<main>{msg}{body}</main></body></html>')


def login_page(err: str = "") -> str:
    msg = f'<div class="msg err">{e(err)}</div>' if err else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Log in</title><style>{CSS}</style></head><body><div class="login card"><h1>Parasite SERP Tracker</h1>{msg}'
            '<form method="post" action="/login"><label>Password</label><input type="password" name="password" autofocus>'
            '<p><button>Log in</button></p></form></div></body></html>')


SEARCH_JS = """
<script>
const $=s=>document.querySelector(s);let job=null,timer=null,lastShown=-1,lastRefresh=0;
const LS=k=>{try{return localStorage.getItem(k)||''}catch(e){return ''}};
$('#loc').onchange=()=>{$('#lang').value=$('#loc').selectedOptions[0].dataset.lang};
$('#login').value=LS('dfs_login');$('#password').value=LS('dfs_pass');
function est(){const n=$('#text').value.split('\\n').filter(l=>l.trim()&&l.trim().toLowerCase()!='keyword').length;
 $('#est').textContent=n?`${n} keywords · about $${(n*Math.ceil($('#depth').value/10)*0.002).toFixed(2)} (billed by DataForSEO)`:''}
['#text','#depth'].forEach(i=>$(i).addEventListener('input',est));est();
$('#f').onchange=e=>{const r=new FileReader();r.onload=()=>{$('#text').value=r.result;est()};r.readAsText(e.target.files[0])};
async function go(){
 if(!$('#text').value.trim()){$('#err').textContent='Paste some keywords first.';return}
 if(!confirm('Search now? '+$('#est').textContent))return;
 const fd=new URLSearchParams(new FormData($('#form')));
 if($('#remember').checked){try{localStorage.setItem('dfs_login',$('#login').value);localStorage.setItem('dfs_pass',$('#password').value)}catch(e){}}
 else{try{localStorage.removeItem('dfs_login');localStorage.removeItem('dfs_pass')}catch(e){}}
 $('#err').textContent='';$('#go').disabled=true;lastShown=-1;
 const j=await (await fetch('/search',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:fd})).json();
 if(j.error){$('#err').textContent=j.error;$('#go').disabled=false;return}
 job=j.job;$('#res').hidden=false;clearInterval(timer);timer=setInterval(tick,1500);tick()}
async function tick(){const s=await (await fetch('/job/'+job+'.json')).json();
 $('#bar').style.width=(s.total?100*s.done/s.total:0)+'%';
 $('#txt').textContent=`${s.done}/${s.total} keywords · cost so far $${s.cost}`+(s.finished?' · done':'');
 $('#err').textContent=s.fatal||(s.n_errors?`${s.n_errors} keyword(s) failed, e.g. ${s.errors[0]}`:'');
 const now=Date.now();
 if(s.done!==lastShown&&(s.finished||now-lastRefresh>6000)){lastShown=s.done;lastRefresh=now;$('#frame').src='/job/'+job+'/report?'+now;$('#dl').href='/job/'+job+'/export.csv';$('#dl').hidden=false}
 if(s.finished||s.fatal){clearInterval(timer);$('#go').disabled=false}}
</script>"""


# ---------------------------------------------------------------- request handler
class Handler(BaseHTTPRequestHandler):
    server_version = "ParasiteTracker"

    def log_message(self, fmt, *args):  # quieter logs, never log bodies
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    # -- auth
    def _secret(self, conn) -> bytes:
        s = db.get_setting(conn, "session_secret")
        if not s:
            s = secrets.token_hex(32)
            db.set_setting(conn, "session_secret", s)
        return s.encode()

    def _sign(self, conn, exp: str) -> str:
        return exp + "." + hmac.new(self._secret(conn), exp.encode(), hashlib.sha256).hexdigest()

    def _authed(self, conn) -> bool:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        v = c["session"].value if "session" in c else ""
        exp, _, sig = v.partition(".")
        return bool(exp.isdigit() and int(exp) > time.time() and
                    hmac.compare_digest(self._sign(conn, exp), f"{exp}.{sig}"))

    # -- io
    def _send(self, body: str | bytes, status=200, ctype="text/html; charset=utf-8", headers=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str, headers=()):
        self._send("", 303, headers=[("Location", to), *headers])

    def _form(self) -> dict[str, list[str]]:
        n = min(int(self.headers.get("Content-Length") or 0), 5_000_000)
        return parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)

    def _flash(self) -> tuple[str, str]:
        q = parse_qs(urlparse(self.path).query)
        return (q.get("m", [""])[0], q.get("k", ["ok"])[0])

    # -- routing
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        path = urlparse(self.path).path
        conn = db.connect(DB_PATH)
        if path == "/healthz":
            return self._send("ok", ctype="text/plain")
        if path == "/login":
            return self._login(conn, method)
        if not self._authed(conn):
            return self._redirect("/login")
        if method == "POST" and not self._same_origin():
            return self._send("bad origin", 403)
        routes = {("GET", "/"): self.dashboard, ("POST", "/search"): self.search_post,
                  ("GET", "/settings"): self.settings, ("POST", "/settings"): self.settings_post,
                  ("GET", "/domains"): self.domains, ("POST", "/domains"): self.domains_post,
                  ("POST", "/logout"): self.logout}
        if path.startswith("/job/"):
            fn = (self.job_json if path.endswith(".json") else self.job_report if path.endswith("/report")
                  else self.job_export if path.endswith("/export.csv") else None)
            return fn(conn) if fn and method == "GET" else self._send("not found", 404)
        fn = routes.get((method, path))
        if not fn:
            return self._send("not found", 404)
        try:
            fn(conn)
        except BrokenPipeError:
            pass

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == self.headers.get("Host")

    def _login(self, conn, method):
        if method == "GET":
            return self._send(login_page())
        pw = self._form().get("password", [""])[0]
        if APP_PASSWORD and hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
            exp = str(int(time.time()) + SESSION_SECONDS)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return self._redirect("/", [("Set-Cookie", f"session={self._sign(conn, exp)}; Path=/; HttpOnly; "
                                                      f"SameSite=Strict; Max-Age={SESSION_SECONDS}{secure}")])
        time.sleep(1.5)  # slow down guessing
        self._send(login_page("Wrong password."), 401)

    def logout(self, conn):
        self._redirect("/login", [("Set-Cookie", "session=; Path=/; Max-Age=0")])

    # -- pages
    def dashboard(self, conn):
        cfg = config_from_db(conn)
        env_creds = bool(cfg.login and cfg.password and os.environ.get("DATAFORSEO_LOGIN"))
        loc = "".join(f'<option value="{k}" data-lang="{v[1]}" {"selected" if k == cfg.location_code else ""}>{v[0]}</option>' for k, v in LOCATIONS.items())
        sel = lambda cur, opts: "".join(f'<option value="{k}" {"selected" if str(k) == str(cur) else ""}>{v}</option>' for k, v in opts)
        if env_creds:
            cred = '<p class="ok">DataForSEO details are set on the server.</p><input type="hidden" id="login" name="login"><input type="hidden" id="password" name="password">'
            remember = ""
        else:
            cred = ('<div class="row"><div><label>DataForSEO login (email)</label><input id="login" name="login" autocomplete="off"></div>'
                    '<div><label>API password <span class="hint">(from app.dataforseo.com/api-access)</span></label><input id="password" type="password" name="password" autocomplete="off"></div></div>')
            remember = '<label class="cb"><input type="checkbox" id="remember" checked> Remember my DataForSEO details in <b>this browser only</b> (never stored on the server)</label>'
        if env_creds:
            remember = '<input type="checkbox" id="remember" hidden>'
        body = f"""<h1>Search</h1>
<form id="form" class="card" onsubmit="return false">{cred}{remember}
<label>Keywords <span class="hint">– one per line, optionally <code>keyword, niche</code>. Not saved anywhere.</span></label>
<textarea id="text" name="text" placeholder="best vpn for netflix, vpn&#10;best crm for small business, saas"></textarea>
<div class="row"><div><label>Country <span class="hint">(search results as seen from here)</span></label><select id="loc" name="location_code">{loc}</select></div>
<div><label>Language code</label><input id="lang" name="language_code" value="{e(cfg.language_code)}"></div>
<div><label>Device</label><select name="device">{sel(cfg.device, [("desktop", "Desktop"), ("mobile", "Mobile")])}</select></div>
<div><label>How deep <span class="hint">(parasites are found within these results)</span></label><select id="depth" name="depth">{sel(min(cfg.depth, 30), [(10, "Top 10 (cheapest)"), (20, "Top 20 (recommended)"), (30, "Top 30")])}</select></div></div>
<p><button id="go" onclick="go()">Search now</button> <span id="est" class="hint"></span>
<input type="file" id="f" accept=".csv,.txt" style="width:auto;margin-left:10px"></p><p id="err" class="err"></p></form>
<div id="res" hidden><div class="card"><div class="bar"><i id="bar"></i></div><p id="txt" class="hint"></p>
<a id="dl" class="btn" hidden href="#" style="background:var(--card);color:var(--fg);border:1px solid var(--line)">Download CSV</a>
<span class="hint"> Results are kept in memory for about an hour, so download the CSV if you need them.</span></div>
<iframe id="frame"></iframe></div>{SEARCH_JS}"""
        self._send(page("Search", body, "/"))

    def search_post(self, conn):
        f = self._form()
        g = lambda k: f.get(k, [""])[0].strip()
        cfg = config_from_db(conn)
        login, password = g("login") or cfg.login, f.get("password", [""])[0] or cfg.password
        if not login or not password:
            return self._json({"error": "Enter your DataForSEO login and API password."})
        for k in ("location_code", "depth"):
            if g(k).isdigit():
                setattr(cfg, k, int(g(k)))
        cfg.depth = min(max(cfg.depth, 10), 50)
        cfg.language_code = g("language_code") or cfg.language_code
        cfg.device = g("device") if g("device") in ("desktop", "mobile") else cfg.device
        rows = parse_keywords_text(f.get("text", [""])[0], cfg)
        if not rows:
            return self._json({"error": "Paste some keywords first."})
        if len(rows) > live.MAX_KEYWORDS:
            return self._json({"error": f"Max {live.MAX_KEYWORDS} keywords per search (you pasted {len(rows)})."})
        ov = {r["domain"]: r["kind"] for r in conn.execute("SELECT domain, kind FROM overrides")}
        job = live.start(rows, cfg, DataForSEO(login, password), ov)
        self._json({"job": job.id})

    def _job(self, path: str):
        jid = path.split("/")[2].removesuffix(".json")
        return live.get(jid)

    def job_json(self, conn):
        job = self._job(urlparse(self.path).path)
        self._json(job.snapshot() if job else {"error": "expired"}, 200 if job else 404)

    def job_report(self, conn):
        job = self._job(urlparse(self.path).path)
        if not job:
            return self._send("<p style='font:14px sans-serif;padding:20px'>These results expired. Run the search again.</p>", 404)
        with job.lock:
            data = analysis.build(job.conn, job.cfg.top_n)
        if data:
            data["web"], data["job"] = True, job.id
        self._send(render_html(data) if data else "<p style='font:14px sans-serif;padding:20px'>Waiting for the first results…</p>")

    def job_export(self, conn):
        job = self._job(urlparse(self.path).path)
        if not job:
            return self._send("expired", 404)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["keyword", "niche", "rank", "host", "url", "title", "is_parasite", "platform", "category", "confidence"])
        with job.lock:
            w.writerows(tuple(r) for r in job.conn.execute(
                """SELECT k.keyword, k.niche, r.rank_group, r.host, r.url, r.title, r.is_parasite, r.platform,
                   r.category, r.confidence FROM results r JOIN keywords k ON k.id=r.keyword_id
                   ORDER BY k.keyword, r.rank_group"""))
        self._send(out.getvalue(), ctype="text/csv; charset=utf-8",
                   headers=[("Content-Disposition", 'attachment; filename="serps.csv"')])

    def _json(self, obj, status=200):
        self._send(json.dumps(obj), status, ctype="application/json")

    def settings(self, conn):
        cfg = config_from_db(conn)
        loc = "".join(f'<option value="{k}" data-lang="{v[1]}" {"selected" if k == cfg.location_code else ""}>{v[0]}</option>' for k, v in LOCATIONS.items())
        cats = "".join(f'<label class="cb"><input type="checkbox" name="cat" value="{k}" {"checked" if k in cfg.enabled_categories else ""}> {e(v)}</label>' for k, v in CATEGORIES.items())
        sel = lambda cur, opts: "".join(f'<option value="{k}" {"selected" if str(k) == str(cur) else ""}>{v}</option>' for k, v in opts)
        body = f"""<h1>Settings</h1><form method="post">
<div class="card"><b>Defaults for the Search page</b><div class="row">
<div><label>Country</label><select name="location_code">{loc}</select></div>
<div><label>Language code</label><input name="language_code" value="{e(cfg.language_code)}"></div>
<div><label>Device</label><select name="device">{sel(cfg.device, [("desktop", "Desktop"), ("mobile", "Mobile")])}</select></div>
<div><label>How deep</label><select name="depth">{sel(min(cfg.depth, 30), [(10, "Top 10"), (20, "Top 20"), (30, "Top 30")])}</select></div>
<div><label>"Visible" means top…</label><select name="top_n">{sel(cfg.top_n, [(3, "3"), (5, "5"), (10, "10"), (20, "20")])}</select></div></div></div>
<div class="card"><b>What counts as a parasite</b>{cats}</div><button>Save settings</button></form>"""
        self._send(page("Settings", body, "/settings", *self._flash()))

    def settings_post(self, conn):
        f = self._form()
        g = lambda k: f.get(k, [""])[0].strip()
        for k in ("location_code", "language_code", "device", "depth", "top_n"):
            if g(k):
                db.set_setting(conn, k, g(k))
        cats = [c for c in f.get("cat", []) if c in CATEGORIES]
        db.set_setting(conn, "enabled_categories", json.dumps(cats))
        self._redirect("/settings?m=Saved.")

    def domains(self, conn):
        rows = conn.execute("SELECT * FROM overrides ORDER BY kind, domain").fetchall()
        trs = "".join(f'<tr><td>{e(r["domain"])}</td><td>{r["kind"]}</td><td><form method="post"><input type="hidden" name="action" value="remove">'
                      f'<input type="hidden" name="domain" value="{e(r["domain"])}"><button class="sec">Remove</button></form></td></tr>' for r in rows)
        none = '<tr><td colspan=3 class="hint">Nothing custom yet.</td></tr>'
        body = f"""<h1>Your parasite list</h1><p class="hint">The built-in list covers ~150 platforms, and every search also looks at your top results for <i>new</i> ones
(sites marked <b>?</b>, plus a candidates table under the report with one-click buttons). Add your own here, or mark a wrong detection as "not a parasite".</p>
<form method="post" class="card"><input type="hidden" name="action" value="add"><div class="row">
<div><label>Domain</label><input name="domain" placeholder="example.com or forbes.com/sites"></div>
<div><label>Treat as</label><select name="kind"><option value="parasite">Parasite</option><option value="ignore">Not a parasite (ignore)</option></select></div></div>
<p><button>Add</button></p></form><div class="card"><table><tr><th>Domain</th><th>Rule</th><th></th></tr>{trs or none}</table></div>"""
        self._send(page("Parasite list", body, "/domains", *self._flash()))

    def domains_post(self, conn):
        f = self._form()
        d = f.get("domain", [""])[0].strip().lower()
        if f.get("action", [""])[0] == "remove":
            conn.execute("DELETE FROM overrides WHERE domain=?", (d,))
        elif d and f.get("kind", [""])[0] in ("parasite", "ignore"):
            d = d if "/" in d.replace("://", "") and "." in d else root_domain(host_of(d))
            conn.execute("INSERT OR REPLACE INTO overrides VALUES(?,?)", (d, f["kind"][0]))
        conn.commit()
        job = live.get(f.get("job", [""])[0])
        if job and f.get("action", [""])[0] == "add" and d and f.get("kind", [""])[0] in ("parasite", "ignore"):
            live.apply_override(job, d, f["kind"][0])
        self._redirect("/domains?m=Updated. It applies to your next search.")


def main() -> None:
    if not APP_PASSWORD:
        sys.exit("Set the APP_PASSWORD environment variable (the password you will use to log in).")
    db.connect(DB_PATH).close()
    port = int(os.environ.get("PORT", "8000"))
    print(f"Open http://localhost:{port}", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
