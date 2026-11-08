"""Real localhost HTTP smoke through the CLI, Waitress, database and assets."""
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import requests


def test_demo_server_login_mail_watch_settings_and_assets(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(40)
    env = {**os.environ, "STEPH_DASHBOARD_TOKEN": token,
           "STEPH_SESSION_SECRET": secrets.token_urlsafe(40),
           "STEPH_COOKIE_SECURE": "false", "STEPH_DATA_DIR": str(tmp_path / "data")}
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen([sys.executable, "-m", "steph_email", "demo", "--port", str(port)],
                               cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    http = requests.Session()
    http.trust_env = False
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError("Demo server exited during startup")
            try:
                if http.get(base + "/health", timeout=0.5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("Demo did not become ready")
        assert http.get(base + "/api/emails", timeout=2).status_code == 401
        login = http.get(base + "/login", timeout=2)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text)[1]
        signed_in = http.post(base + "/login", data={"token": token, "csrf_token": csrf}, timeout=2)
        assert signed_in.status_code == 200
        assert "Keep the work moving" in signed_in.text
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', signed_in.text)[1]
        headers = {"X-CSRF-Token": csrf}
        emails = http.get(base + "/api/emails", timeout=2).json()
        assert len(emails) == 5 and all(e["account"] == "demo" for e in emails)
        assert http.get(base + "/static/style.css", timeout=2).status_code == 200
        assert http.get(base + "/static/dashboard.js", timeout=2).status_code == 200
        created = http.post(base + "/api/expectations", json={"sender": "supplier@example.com", "subject": "credit", "hours": 72, "notify": "sms"}, headers=headers, timeout=2)
        assert created.status_code == 201
        saved = http.post(base + "/api/settings", json={"urgency_threshold": 60}, headers=headers, timeout=2)
        assert saved.status_code == 200 and saved.json()["urgency_threshold"] == 60
        assert http.post(base + "/api/settings", json={"dry_run": False}, headers=headers, timeout=2).status_code == 400
        previews = http.get(base + "/api/notifications", timeout=2).json()
        assert previews and all(n["status"] == "preview" for n in previews)
        assert http.post(base + "/logout", data={"csrf_token": csrf}, timeout=2).status_code == 200
        assert http.get(base + "/api/emails", timeout=2).status_code == 401
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
        http.close()
