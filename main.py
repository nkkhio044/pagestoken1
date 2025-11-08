#!/usr/bin/env python3
"""
Offline Flask Hosting Server (Persistent User VENV Edition)
Run: python newhosting.py
Open: http://localhost:5000

Features:
1. Script and Package list paste input.
2. Packages install once per user (in user_venvs/[username]/venv).
3. Automatic port correction in host_app.py.
4. Pre-deployment dependency checking.
5. DEPLOYMENT LIMIT: Max 10 apps per user, hosted for 6 months.
"""
import os
import sqlite3
import uuid
import shutil
import subprocess
import threading
import time
import datetime
import sys
import re
from pathlib import Path
from flask import Flask, request, redirect, url_for, render_template, send_from_directory, abort

BASE = Path(__file__).parent.resolve()
HOSTED = BASE / "hosted_apps"
DB = BASE / "apps.db"
USER_VENVS = BASE / "user_venvs"
PORT_START = 6000
PORT_MAX = 9999

for d in (HOSTED, USER_VENVS):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(BASE / "templates"), static_folder=str(BASE / "static"))

LOCK = threading.Lock()
PROCS = {}

# ---- DB helpers ----
def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS apps (
            id TEXT PRIMARY KEY,
            username TEXT,
            name TEXT,
            folder TEXT,
            port INTEGER,
            status TEXT,
            created_at TEXT,
            expires_at TEXT,
            last_error TEXT
        )
    """)
    con.commit(); con.close()

def add_record(app_id, username, name, folder, port, status, created_at, expires_at):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("INSERT INTO apps (id,username,name,folder,port,status,created_at,expires_at,last_error) VALUES (?,?,?,?,?,?,?,?,?)",
                (app_id, username, name, str(folder), port, status, created_at, expires_at, ""))
    con.commit(); con.close()

def update_status(app_id, status, last_error=""):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("UPDATE apps SET status=?, last_error=? WHERE id=?", (status, last_error, app_id))
    con.commit(); con.close()

def get_all():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT id,username,name,folder,port,status,created_at,expires_at,last_error FROM apps")
    rows = cur.fetchall(); con.close()
    apps = []
    for r in rows:
        apps.append({
            "id": r[0], "username": r[1], "name": r[2], "folder": r[3],
            "port": r[4], "status": r[5], "created_at": r[6], "expires_at": r[7], "last_error": r[8]
        })
    return apps

def get_record(app_id):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT id,username,name,folder,port,status,created_at,expires_at,last_error FROM apps WHERE id=?", (app_id,))
    r = cur.fetchone(); con.close()
    if not r: return None
    return {"id": r[0], "username": r[1], "name": r[2], "folder": r[3], "port": r[4],
            "status": r[5], "created_at": r[6], "expires_at": r[7], "last_error": r[8]}

# ---- port allocator ----
def pick_port():
    used = set()
    for a in get_all():
        if a["port"]:
            used.add(int(a["port"]))
    p = PORT_START
    while p in used and p < PORT_MAX:
        p += 1
    if p >= PORT_MAX:
        raise RuntimeError("No ports available")
    return p

# ---- VENV/Package Management ----
def get_user_venv_path(username):
    return USER_VENVS / username / "venv"

def get_venv_python(username):
    venv_path = get_user_venv_path(username)
    return venv_path / "bin" / "python"

def check_package_installed(venv_python_path, package_name):
    try:
        result = subprocess.run(
            [str(venv_python_path).strip(), "-m", "pip", "show", package_name.split('=')[0].split('<')[0].split('>')[0]],
            capture_output=True, text=True, check=True
        )
        return "Name:" in result.stdout
    except subprocess.CalledProcessError:
        return False
    except FileNotFoundError:
        return False

def install_user_packages(username, packages_to_install):
    venv_dir = get_user_venv_path(username)
    python_bin = sys.executable
    
    if not venv_dir.exists():
        venv_dir.mkdir(parents=True, exist_ok=True)
        subprocess.check_call([python_bin, "-m", "venv", str(venv_dir)])
    
    pip = venv_dir / "bin" / "pip"
    
    if packages_to_install:
        subprocess.check_call([str(pip), "install", "-U", "pip"])
        install_command = [str(pip), "install"] + packages_to_install
        subprocess.check_call(install_command)
    
    return True

# ---- start/stop app process ----
def start_app(app_id):
    rec = get_record(app_id)
    if not rec:
        return False, "not found"
    if rec["status"] == "expired":
        return False, "expired"
        
    folder = Path(rec["folder"])
    entry = folder / "host_app.py"
    if not entry.exists():
        update_status(app_id, "missing entry")
        return False, "missing entry host_app.py"

    venv_python = get_venv_python(rec["username"])
    if not venv_python.exists():
        update_status(app_id, "venv missing")
        return False, "User VENV not created. Install packages first."
        
    env = os.environ.copy()
    env["PORT"] = str(rec["port"])
    
    stdout = open(folder / "host.stdout.log", "ab")
    stderr = open(folder / "host.stderr.log", "ab")
    p = subprocess.Popen([str(venv_python), str(entry)], cwd=str(folder), env=env, stdout=stdout, stderr=stderr)
    
    with LOCK:
        PROCS[app_id] = p
    update_status(app_id, "running")
    
    try:
        (folder / "host.pid").write_text(str(p.pid))
    except Exception:
        pass
    return True, ""

def stop_app(app_id):
    rec = get_record(app_id)
    if not rec:
        return False
    folder = Path(rec["folder"])
    pidfile = folder / "host.pid"
    try:
        with LOCK:
            p = PROCS.get(app_id)
            if p:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                del PROCS[app_id]
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            try:
                os.kill(pid, 15)
            except Exception:
                pass
            try:
                pidfile.unlink()
            except Exception:
                pass
    except Exception:
        pass
    update_status(app_id, "stopped")
    return True

# ---- deploy pipeline for script/deps (MODIFIED) ----
def deploy_script_from_text(script_content, requirements_content, username, display_name):
    app_id = str(uuid.uuid4())
    dest = HOSTED / username / app_id
    
    # 1. Deployment Limit Check (Max 10 per user)
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM apps WHERE username=? AND status != 'expired'", (username,))
    count = cur.fetchone()[0]
    con.close()
    
    if count >= 10:
        return False, f"Deployment limit reached: User '{username}' already has {count} active apps (Max 10).", None

    # 2. Dependency Check
    packages = [pkg.strip() for pkg in requirements_content.splitlines() if pkg.strip()]
    venv_python = get_venv_python(username)
    
    if not venv_python.exists():
        return False, f"User VENV for '{username}' not found. Please install required packages first.", None
        
    missing_packages = []
    for pkg in packages:
        pkg_name = pkg.split('=')[0].split('<')[0].split('>')[0]
        if not check_package_installed(venv_python, pkg_name):
            missing_packages.append(pkg)

    if missing_packages:
        error_msg = f"Deployment failed: Missing packages in user VENV: {', '.join(missing_packages)}. Install them first."
        # Add record with status 'requires_install'
        port = pick_port()
        created = datetime.datetime.utcnow().isoformat()
        expires = (datetime.datetime.utcnow() + datetime.timedelta(days=30*6)).isoformat()
        add_record(app_id, username, display_name, dest, port, "requires_install", created, expires)
        return False, error_msg, app_id


    # 3. Port Auto-Correction and Write Script
    try:
        PORT_REPLACEMENT_PATTERN = r'PORT\s*=\s*\d+'
        NEW_PORT_LINE = 'PORT = int(os.environ.get("PORT", 5000))'
        
        if re.search(PORT_REPLACEMENT_PATTERN, script_content):
             script_content = re.sub(PORT_REPLACEMENT_PATTERN, NEW_PORT_LINE, script_content, 1)
             
    except Exception as e:
        print(f"[{app_id[:8]}] Auto-correction failed: {e}")
        
    # 4. Allocate Port and Add DB record (Status='deploying')
    try:
        port = pick_port()
    except Exception:
        return False, "no port available", None
        
    created = datetime.datetime.utcnow().isoformat()
    # Deployment for 6 months (approx 180 days)
    expires = (datetime.datetime.utcnow() + datetime.timedelta(days=30*6)).isoformat()
    add_record(app_id, username, display_name, dest, port, "deploying", created, expires)
    
    # 5. Create destination folder and host_app.py
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "host_app.py").write_text(script_content)
    except Exception as e:
        update_status(app_id, "file_write_failed", str(e))
        shutil.rmtree(dest, ignore_errors=True)
        return False, "file write failed", app_id
        
    # 6. Start app
    ok, err = start_app(app_id)
    if not ok:
        if err != "expired":
            update_status(app_id, "start_failed", err)
        return False, f"start failed: {err}", app_id
        
    return True, app_id, app_id

# ---- monitor thread (Ensures apps stay running unless expired or crashed) ----
def monitor_loop():
    while True:
        try:
            time.sleep(5)
            rows = get_all()
            for r in rows:
                aid = r["id"]
                # 6 Month Expiry Check: only changes status, does NOT delete files
                if r["expires_at"]:
                    try:
                        exp = datetime.datetime.fromisoformat(r["expires_at"])
                        if datetime.datetime.utcnow() > exp and r["status"] != "expired":
                            stop_app(aid)
                            update_status(aid, "expired")
                            continue
                    except Exception:
                        pass
                # Restart crashed processes: keeps the app "hosted" for 6 months
                with LOCK:
                    p = PROCS.get(aid)
                if p:
                    if p.poll() is not None:
                        update_status(aid, "crashed")
                        rec = get_record(aid)
                        if rec and rec["status"] not in ("expired", "stopped"):
                            try:
                                start_app(aid)
                            except Exception:
                                pass
                else:
                    if r["status"] == "running":
                        try:
                            start_app(rec["id"]) # Fix: use rec["id"] or aid
                        except Exception:
                            pass
        except Exception:
            time.sleep(1)

# ---- Flask routes ----
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    username = request.form.get("username", "anonymous").strip() or "anonymous"
    display_name = request.form.get("name", "") or "app"
    script_content = request.form.get("script_content", "").strip()
    requirements_content = request.form.get("requirements_content", "").strip()

    if not script_content:
        return "Script content is required (host_app.py)", 400

    def _bg():
        ok, info, app_id = deploy_script_from_text(
            script_content, requirements_content, username, display_name
        )
        if not ok:
            print(f"Deploy failed for {app_id}: {info}")
            
    threading.Thread(target=_bg, daemon=True).start()
    return redirect(url_for("dashboard"))

@app.route("/install", methods=["POST"])
def install_packages_route():
    username = request.form.get("username", "anonymous").strip() or "anonymous"
    requirements_content = request.form.get("requirements_content", "").strip()
    
    if not requirements_content:
        return "Package list is required for installation.", 400

    packages_to_install = [pkg.strip() for pkg in requirements_content.splitlines() if pkg.strip()]

    def _bg_install():
        try:
            print(f"Starting installation for user '{username}': {packages_to_install}")
            install_user_packages(username, packages_to_install)
            print(f"Installation successful for user '{username}'.")
        except subprocess.CalledProcessError as e:
            error_msg = f"Installation failed for user '{username}': {e.stderr.splitlines()[-1] if e.stderr else str(e)}"
            print(error_msg)
        except Exception as e:
            print(f"Installation failed with general error: {e}")

    threading.Thread(target=_bg_install, daemon=True).start()
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
def dashboard():
    apps = get_all()
    # This generates the user-facing URL: http://localhost:[Port]
    for a in apps:
        a["local_url"] = f"http://localhost:{a['port']}"
    
    # Get status of user VENV
    user_envs = {}
    for user_dir in USER_VENVS.iterdir():
        if user_dir.is_dir() and (user_dir / "venv").exists():
             user_envs[user_dir.name] = "Ready"
        elif user_dir.is_dir():
             user_envs[user_dir.name] = "VENV Missing"

    return render_template("dashboard.html", apps=apps, user_envs=user_envs)

@app.route("/start/<app_id>", methods=["POST"])
def route_start(app_id):
    start_app(app_id)
    return redirect(url_for("dashboard"))

@app.route("/stop/<app_id>", methods=["POST"])
def route_stop(app_id):
    stop_app(app_id)
    return redirect(url_for("dashboard"))

@app.route("/delete/<app_id>", methods=["POST"])
def route_delete(app_id):
    rec = get_record(app_id)
    if not rec:
        return redirect(url_for("dashboard"))
    stop_app(app_id)
    try:
        shutil.rmtree(rec["folder"])
    except Exception:
        pass
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("DELETE FROM apps WHERE id=?", (app_id,))
    con.commit(); con.close()
    with LOCK:
        if app_id in PROCS:
            try:
                del PROCS[app_id]
            except Exception:
                pass
    return redirect(url_for("dashboard"))

@app.route("/logs/<app_id>")
def route_logs(app_id):
    rec = get_record(app_id)
    if not rec:
        return "Not found", 404
    folder = Path(rec["folder"])
    out = ""
    for fname in ("host.stdout.log", "host.stderr.log"):
        f = folder / fname
        if f.exists():
            try:
                with open(f, "r", errors="ignore") as fh:
                    out += "\n\n==== " + fname + " ====\n" + fh.read()[-20000:]
            except Exception:
                out += f"\n\n==== {fname} ====\n(Error reading file)"
    return "<pre>" + out + "</pre>"

@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(str(BASE / "static"), p)

# ---- startup ----
if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    for rec in get_all():
        if rec["status"] == "running":
            try:
                start_app(rec["id"])
            except Exception:
                pass
    app.run(host="0.0.0.0", port=5000, debug=False)
