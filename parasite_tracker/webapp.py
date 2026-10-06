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
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import analysis, db, tracker
from .config import config_from_db, parse_keywords_text
from .dataforseo import DataForSEO, DataForSEOError
from .detector import DEFAULT_ENABLED, host_of, root_domain
from .report import render_html

DB_PATH = os.environ.get("DB_PATH", "parasite.db")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_SECONDS = 14 * 24 * 3600
e = html.escape

LOCATIONS = {2840: "United States", 2826: "United Kingdom", 2124: "Canada", 2036: "Australia",
             2554: "New Zealand", 2372: "Ireland", 2276: "Germany", 2250: "France", 2724: "Spain",
             2380: "Italy", 2528: "Netherlands", 2076: "Brazil", 2484: "Mexico", 2356: "India",
             2710: "South Africa", 2702: "Singapore", 2784: "United Arab Emirates",
             2586: "Pakistan", 2608: "Philippines"}
CATEGORIES = {
    "ugc_blog": "Free blogs (Medium, Blogspot, Substack, WordPress.com…)",
    "forum_qa": "Forums & Q&A (Reddit, Quora…)",
    "docs_hosting": "Document hosting (Google Docs, Scribd, SlideShare…)",
    "code_static_hosting": "Free site hosting (github.io, netlify.app, pages.dev…)",
    "professional_social": "Profiles/articles (LinkedIn Pulse, Pinterest, about.me…)",
    "news_contributor": "Contributor & press-release sites (Forbes Councils, PRNewswire…)",
    "social_video": "Social & video (YouTube, Facebook, TikTok…) – not classic parasites",
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

# ---------------------------------------------------------------- background job
class Job:
    lock = threading.Lock()
    running = False
    error = ""
    note = ""


def start_job(resume: bool = False) -> str | None:
    """Start a run in a background thread. Returns an error string, or None if started."""
    if not Job.lock.acquire(blocking=False):
        return "A check is already running."
    conn = db.connect(DB_PATH)
    cfg = config_from_db(conn)
    if not cfg.login or not cfg.password:
        Job.lock.release()
        return "Add your DataForSEO login and API password in Settings first."
    if not conn.execute("SELECT 1 FROM keywords WHERE active=1").fetchone():
        Job.lock.release()
        return "Add some keywords first."
    Job.running, Job.error, Job.note = True, "", "Starting…"

    def work():
        c = db.connect(DB_PATH)
        try:
            api = DataForSEO(cfg.login, cfg.password)
            run_id = tracker.latest_open_run(c) if resume else None
            if run_id:
                c.execute("UPDATE tasks SET task_id=NULL, status='pending', error=NULL "
                          "WHERE run_id=? AND status='error'", (run_id,))
                c.commit()
            else:
                run_id = tracker.start_run(c, cfg)
            Job.note = "Sending keywords to DataForSEO…"
            tracker.submit(c, api, cfg, run_id)
            Job.note = "Waiting for Google results…"
            if not tracker.collect(c, api, cfg, run_id):
                Job.error = "Some keywords failed (see Retry). Results that worked are in the report."
        except (DataForSEOError, OSError) as ex:
            Job.error = str(ex)
        except Exception as ex:  # noqa: BLE001 - surface anything to the UI
            Job.error = f"Unexpected error: {ex}"
        finally:
            Job.running, Job.note = False, ""
            Job.lock.release()

    threading.Thread(target=work, daemon=True).start()
    return None


def scheduler() -> None:
    while True:
        time.sleep(600)
        try:
            conn = db.connect(DB_PATH)
            days = config_from_db(conn).schedule_days
            if days > 0 and not Job.running:
                due = conn.execute("SELECT NOT EXISTS(SELECT 1 FROM runs WHERE started_at > "
                                   "datetime('now', ?))", (f"-{days} days",)).fetchone()[0]
                if due:
                    start_job()
        except Exception as ex:  # noqa: BLE001
            print("scheduler:", ex, file=sys.stderr)


def status(conn) -> dict:
    run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    done = total = errors = 0
    if run:
        done, total, errors = conn.execute(
            "SELECT COALESCE(SUM(status='done'),0), COUNT(*), COALESCE(SUM(status='error'),0) "
            "FROM tasks WHERE run_id=?", (run["id"],)).fetchone()
    return {"running": Job.running, "note": Job.note, "error": Job.error, "done": done,
            "total": total, "errors": errors, "run_id": run["id"] if run else None,
            "cost": round(run["cost"], 4) if run else 0,
            "can_resume": bool(tracker.latest_open_run(conn)) and not Job.running}


# ---------------------------------------------------------------- html helpers
def page(title: str, body: str, active: str = "", flash: str = "", kind: str = "ok") -> str:
    links = [("/", "Dashboard"), ("/keywords", "Keywords"), ("/settings", "Settings"), ("/domains", "Parasite list")]
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


DASH_JS = """
<script>
const $=s=>document.querySelector(s);let was=false;
async function tick(){try{const s=await (await fetch('/status.json')).json();
 $('#bar').style.width=(s.total?100*s.done/s.total:0)+'%';
 $('#txt').textContent=s.running?`${s.note} ${s.done}/${s.total} keywords done`:(s.total?`Last run #${s.run_id}: ${s.done}/${s.total} keywords, cost $${s.cost}`:'No run yet.');
 $('#err').textContent=s.error||'';$('#go').disabled=s.running;$('#retry').style.display=s.can_resume?'inline-block':'none';$('#retry').disabled=s.running;
 if(was&&!s.running)$('#frame').src='/report?'+Date.now();was=s.running}catch(e){}}
setInterval(tick,3000);tick();
async function go(resume){if(!resume&&!confirm('Start a check now? Estimated cost: $'+$('#go').dataset.cost+' (billed by DataForSEO)'))return;
 const r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:resume?'resume=1':''});
 const j=await r.json();if(j.error)$('#err').textContent=j.error;tick()}
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
        routes = {("GET", "/"): self.dashboard, ("GET", "/report"): self.report,
                  ("GET", "/keywords"): self.keywords, ("POST", "/keywords"): self.keywords_post,
                  ("GET", "/settings"): self.settings, ("POST", "/settings"): self.settings_post,
                  ("GET", "/domains"): self.domains, ("POST", "/domains"): self.domains_post,
                  ("POST", "/run"): self.run_post, ("GET", "/status.json"): self.status_json,
                  ("GET", "/export.csv"): self.export, ("POST", "/logout"): self.logout}
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
        n = conn.execute("SELECT COUNT(*) FROM keywords WHERE active=1").fetchone()[0]
        cost = tracker.estimate_cost(n, cfg.depth, cfg.priority)
        has_data = analysis.build(conn, cfg.top_n) is not None
        todo = []
        if not (cfg.login and cfg.password):
            todo.append('<a href="/settings">1. Enter your DataForSEO login</a>')
        if not n:
            todo.append('<a href="/keywords">2. Add your keywords</a>')
        guide = ('<div class="card"><b>Getting started</b><br>' + "<br>".join(todo) + "</div>") if todo else ""
        frame = ('<iframe id="frame" src="/report"></iframe>' if has_data else
                 '<iframe id="frame" hidden></iframe><p class="hint">The report appears here after the first finished run (usually 2–10 minutes).</p>')
        body = f"""<h1>Dashboard</h1>{guide}
<div class="card"><div class="bar"><i id="bar"></i></div><p id="txt" class="hint"></p><p id="err" class="err"></p>
<button id="go" data-cost="{cost:.2f}" onclick="go(false)">Check now ({n} keywords · ~${cost:.2f})</button>
<button id="retry" class="sec" style="display:none" onclick="go(true)">Retry / resume last run</button>
<a class="btn sec" href="/export.csv" style="background:var(--card);color:var(--fg);border:1px solid var(--line)">Export CSV</a></div>
{frame}{DASH_JS}"""
        self._send(page("Dashboard", body, "/", *self._flash()))

    def report(self, conn):
        cfg = config_from_db(conn)
        data = analysis.build(conn, cfg.top_n)
        self._send(render_html(data) if data else "<p style='font:14px sans-serif;padding:20px'>No finished run yet.</p>")

    def keywords(self, conn):
        rows = conn.execute("SELECT * FROM keywords WHERE active=1 ORDER BY niche, keyword").fetchall()
        trs = "".join(f'<tr><td><input type="checkbox" name="del" value="{r["id"]}"></td><td>{e(r["keyword"])}</td>'
                      f'<td>{e(r["niche"])}</td><td>{LOCATIONS.get(r["location_code"], r["location_code"])}</td></tr>' for r in rows)
        del_btn = ('<button class="sec" onclick="return confirm(' + "'Delete ticked keywords?'" + ')">Delete ticked</button>') if rows else ""
        empty = '<tr><td colspan=4 class="hint">None saved yet.</td></tr>'
        body = f"""<h1>Keywords <span class="hint">({len(rows)} saved)</span></h1>
<form method="post" class="card"><label>Paste keywords <span class="hint">– one per line. Optional: <code>keyword, niche</code>. Pasting two columns from Excel/Google Sheets works too.</span></label>
<textarea name="text" placeholder="best vpn for netflix, vpn&#10;best crm for small business, saas"></textarea>
<p><label class="cb"><input type="radio" name="mode" value="add" checked> Add to my saved list</label>
<label class="cb"><input type="radio" name="mode" value="replace"> Replace my whole list</label></p>
<input type="hidden" name="action" value="save"><button>Save keywords</button>
<input type="file" id="f" accept=".csv,.txt" style="width:auto;margin-left:10px"></form>
<script>document.getElementById('f').onchange=e=>{{const r=new FileReader();r.onload=()=>document.querySelector('textarea').value=r.result;r.readAsText(e.target.files[0])}}</script>
<form method="post"><input type="hidden" name="action" value="delete"><div class="card" style="max-height:480px;overflow:auto">
<table><tr><th></th><th>Keyword</th><th>Niche</th><th>Location</th></tr>{trs or empty}</table></div>
{del_btn}</form>"""
        self._send(page("Keywords", body, "/keywords", *self._flash()))

    def keywords_post(self, conn):
        f, cfg = self._form(), config_from_db(conn)
        if f.get("action", [""])[0] == "delete":
            ids = [int(x) for x in f.get("del", []) if x.isdigit()]
            conn.executemany("UPDATE keywords SET active=0 WHERE id=?", [(i,) for i in ids])
            conn.commit()
            return self._redirect(f"/keywords?m=Deleted {len(ids)} keywords.")
        rows = parse_keywords_text(f.get("text", [""])[0], cfg)
        if not rows:
            return self._redirect("/keywords?m=Nothing to save - paste some keywords first.&k=err")
        tracker.add_keywords(conn, rows, replace=f.get("mode", ["add"])[0] == "replace")
        self._redirect(f"/keywords?m=Saved {len(rows)} keywords.")

    def settings(self, conn):
        cfg = config_from_db(conn)
        env_creds = bool(os.environ.get("DATAFORSEO_LOGIN"))
        loc = "".join(f'<option value="{k}" {"selected" if k == cfg.location_code else ""}>{v}</option>' for k, v in LOCATIONS.items())
        cats = "".join(f'<label class="cb"><input type="checkbox" name="cat" value="{k}" {"checked" if k in cfg.enabled_categories else ""}> {e(v)}</label>' for k, v in CATEGORIES.items())
        sel = lambda cur, opts: "".join(f'<option value="{k}" {"selected" if str(k) == str(cur) else ""}>{v}</option>' for k, v in opts)
        ph = "saved - leave blank to keep" if cfg.password else ""
        if env_creds:
            cred = '<p class="ok">Credentials are set through server environment variables.</p>'
        else:
            cred = (f'<div class="row"><div><label>Login (email)</label><input name="login" value="{e(cfg.login)}" autocomplete="off"></div>'
                    f'<div><label>API password</label><input type="password" name="password" placeholder="{ph}" autocomplete="new-password"></div></div>')
        body = f"""<h1>Settings</h1><form method="post">
<div class="card"><b>DataForSEO</b> <span class="hint">– use the <u>API password</u> from app.dataforseo.com/api-access (not your account password)</span>
{cred}</div>
<div class="card"><b>Search</b><div class="row">
<div><label>Country</label><select name="location_code">{loc}</select></div>
<div><label>Language code</label><input name="language_code" value="{e(cfg.language_code)}"></div>
<div><label>Device</label><select name="device">{sel(cfg.device, [("desktop", "Desktop"), ("mobile", "Mobile")])}</select></div>
<div><label>How deep to check</label><select name="depth">{sel(cfg.depth, [(10, "Top 10 (cheapest)"), (20, "Top 20"), (30, "Top 30"), (50, "Top 50")])}</select></div>
<div><label>Speed</label><select name="priority">{sel(cfg.priority, [(1, "Normal (cheapest, ~1-5 min)"), (2, "High priority (2x cost, faster)")])}</select></div>
<div><label>"Visible" means top…</label><select name="top_n">{sel(cfg.top_n, [(3, "3"), (5, "5"), (10, "10"), (20, "20")])}</select></div>
<div><label>Auto-check</label><select name="schedule_days">{sel(cfg.schedule_days, [(0, "Off (manual only)"), (1, "Every day"), (3, "Every 3 days"), (7, "Every week"), (14, "Every 2 weeks")])}</select></div></div></div>
<div class="card"><b>What counts as a parasite</b>{cats}</div><button>Save settings</button></form>"""
        self._send(page("Settings", body, "/settings", *self._flash()))

    def settings_post(self, conn):
        f = self._form()
        g = lambda k: f.get(k, [""])[0].strip()
        for k in ("location_code", "language_code", "device", "depth", "priority", "top_n", "schedule_days"):
            if g(k):
                db.set_setting(conn, k, g(k))
        if "login" in f:
            db.set_setting(conn, "login", g("login"))
        if g("password"):
            db.set_setting(conn, "password", f["password"][0])
        cats = [c for c in f.get("cat", []) if c in CATEGORIES]
        db.set_setting(conn, "enabled_categories", json.dumps(cats))
        n = tracker.reclassify(conn, config_from_db(conn))
        self._redirect(f"/settings?m=Saved. Re-checked {n} stored results.")

    def domains(self, conn):
        rows = conn.execute("SELECT * FROM overrides ORDER BY kind, domain").fetchall()
        trs = "".join(f'<tr><td>{e(r["domain"])}</td><td>{r["kind"]}</td><td><form method="post"><input type="hidden" name="action" value="remove">'
                      f'<input type="hidden" name="domain" value="{e(r["domain"])}"><button class="sec">Remove</button></form></td></tr>' for r in rows)
        none = '<tr><td colspan=3 class="hint">Nothing custom yet.</td></tr>'
        body = f"""<h1>Your parasite list</h1><p class="hint">The built-in list covers ~150 platforms. Add your own, or mark a wrong detection as "not a parasite".
Suggestions the tool found in your results are at the bottom of the Dashboard report.</p>
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
        tracker.reclassify(conn, config_from_db(conn))
        self._redirect("/domains?m=Updated.")

    def run_post(self, conn):
        err = start_job(resume="resume" in self._form())
        self._send(json.dumps({"error": err}), ctype="application/json")

    def status_json(self, conn):
        self._send(json.dumps(status(conn)), ctype="application/json")

    def export(self, conn):
        run = analysis.finished_runs(conn, 1)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["keyword", "niche", "rank", "host", "url", "title", "is_parasite", "platform", "category", "confidence"])
        if run:
            w.writerows(tuple(r) for r in conn.execute(
                """SELECT k.keyword, k.niche, r.rank_group, r.host, r.url, r.title, r.is_parasite, r.platform,
                   r.category, r.confidence FROM results r JOIN keywords k ON k.id=r.keyword_id
                   WHERE r.run_id=? ORDER BY k.keyword, r.rank_group""", (run[-1]["id"],)))
        self._send(out.getvalue(), ctype="text/csv; charset=utf-8",
                   headers=[("Content-Disposition", 'attachment; filename="serps.csv"')])


def main() -> None:
    if not APP_PASSWORD:
        sys.exit("Set the APP_PASSWORD environment variable (the password you will use to log in).")
    db.connect(DB_PATH).close()
    threading.Thread(target=scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print(f"Open http://localhost:{port}", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
