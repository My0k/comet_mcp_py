from __future__ import annotations

import configparser
import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Flask, redirect, render_template_string, request, session, url_for
import requests


ROOT = Path(__file__).resolve().parent
_STORE_LOCK = threading.Lock()
_MESSAGE_STORE: dict[str, list[dict[str, str]]] = {}


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    api_key: str | None


def load_api_config(path: Path) -> ApiConfig:
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")

    ip = cp.get("IP", "local", fallback="127.0.0.1").strip()
    port = cp.getint("IP", "port", fallback=8787)

    api_key = None
    if cp.has_section("API"):
        api_key = (cp.get("API", "key", fallback="") or "").strip() or None

    api_key = api_key or os.environ.get("COMET_AUTO_API_KEY", "").strip() or None
    return ApiConfig(base_url=f"http://{ip}:{port}", api_key=api_key)


def api_post_ask(cfg: ApiConfig, prompt: str, new_chat: bool, timeout_s: float) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    payload = {"prompt": prompt, "new_chat": bool(new_chat), "timeout_s": float(timeout_s)}
    r = requests.post(f"{cfg.base_url}/ask", json=payload, headers=headers, timeout=float(timeout_s) + 20.0)
    try:
        data = r.json()
    except Exception:
        data = {"ok": False, "error": f"Respuesta no-JSON (HTTP {r.status_code})", "raw": r.text[:2000]}
    if not r.ok:
        data.setdefault("ok", False)
        data.setdefault("error", f"HTTP {r.status_code}")
    return data


HTML = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Comet Auto Client</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:#0b1220; color:#e6e9f2; margin:0; }
    .wrap { max-width: 980px; margin: 0 auto; padding: 18px; }
    .top { display:flex; gap:12px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
    .card { background:#111a2e; border:1px solid #223053; border-radius:12px; padding:14px; }
    .muted { color:#aab2c5; font-size: 13px; }
    .chat { height: 62vh; overflow:auto; padding: 10px; background:#0d1630; border-radius: 10px; border:1px solid #1f2d55; }
    .msg { margin: 10px 0; display:flex; }
    .msg.user { justify-content:flex-end; }
    .bubble { max-width: 82%; padding:10px 12px; border-radius:12px; line-height:1.35; white-space:pre-wrap; word-wrap:break-word; }
    .user .bubble { background:#2b5cff; color:#fff; border-top-right-radius:6px; }
    .bot .bubble { background:#16244a; border-top-left-radius:6px; }
    .row { display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }
    textarea { width: 100%; min-height: 74px; resize: vertical; padding: 10px; border-radius: 10px; border:1px solid #23335c; background:#0b1430; color:#e6e9f2; }
    input[type="number"] { width: 110px; }
    input, button { border-radius: 10px; border:1px solid #23335c; background:#0b1430; color:#e6e9f2; padding: 10px; }
    button { background:#1b2a55; cursor:pointer; }
    button.primary { background:#2b5cff; border-color:#2b5cff; }
    button.danger { background:#3a1b28; border-color:#5a2a3d; }
    .err { background:#3a1b28; border:1px solid #5a2a3d; padding: 10px; border-radius: 10px; margin-top: 12px; white-space: pre-wrap; }
    .ok { background:#10321f; border:1px solid #1c5a37; padding: 10px; border-radius: 10px; margin-top: 12px; white-space: pre-wrap; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <div style="font-weight:700; font-size: 18px;">Comet Auto Client</div>
        <div class="muted">API: {{ api_base }} {% if api_key %}(API key activa){% endif %}</div>
      </div>
      <form method="post" action="{{ url_for('clear') }}">
        <button class="danger" type="submit">Limpiar chat</button>
      </form>
    </div>

    <div class="card" style="margin-top: 14px;">
      <div id="chat" class="chat">
        {% for m in messages %}
          <div class="msg {{ m.role }}">
            <div class="bubble">{{ m.text }}</div>
          </div>
        {% endfor %}
      </div>

      <form method="post" action="{{ url_for('send') }}" style="margin-top: 12px;">
        <textarea name="prompt" placeholder="Escribe tu mensaje..." required autofocus>{{ draft }}</textarea>
        <div class="row">
          <label class="muted"><input type="checkbox" name="new_chat" {% if new_chat %}checked{% endif %} /> new_chat</label>
          <label class="muted">timeout_s <input type="number" name="timeout_s" value="{{ timeout_s }}" min="5" max="600" step="1" /></label>
          <button class="primary" type="submit">Enviar</button>
        </div>
      </form>

      {% if notice %}
        <div class="{{ 'ok' if notice_ok else 'err' }}">{{ notice }}</div>
      {% endif %}
    </div>
  </div>

  <script>
    const el = document.getElementById('chat');
    if (el) el.scrollTop = el.scrollHeight;
  </script>
</body>
</html>
"""


def create_app() -> Flask:
    cfg = load_api_config(ROOT / "config.conf")
    app = Flask(__name__)
    app.secret_key = os.environ.get("COMET_AUTO_CLIENT_SECRET", "dev-secret-change-me")

    def _sid() -> str:
        sid = str(session.get("sid") or "")
        if not sid:
            sid = secrets.token_urlsafe(16)
            session["sid"] = sid
        return sid

    def _msgs() -> list[dict[str, str]]:
        sid = _sid()
        with _STORE_LOCK:
            return list(_MESSAGE_STORE.get(sid, []))

    def _set_msgs(msgs: list[dict[str, str]]) -> None:
        sid = _sid()
        # Prevent unbounded memory growth
        trimmed: list[dict[str, str]] = []
        for m in msgs[-80:]:
            role = "user" if m.get("role") == "user" else "bot"
            text = (m.get("text") or "").strip()
            if len(text) > 8000:
                text = text[:8000] + "\n…(truncado)…"
            trimmed.append({"role": role, "text": text})
        with _STORE_LOCK:
            _MESSAGE_STORE[sid] = trimmed

    @app.get("/")
    def index() -> str:
        return render_template_string(
            HTML,
            api_base=cfg.base_url,
            api_key=bool(cfg.api_key),
            messages=_msgs(),
            notice=session.pop("notice", ""),
            notice_ok=bool(session.pop("notice_ok", False)),
            draft=session.pop("draft", ""),
            new_chat=bool(session.get("new_chat", False)),
            timeout_s=float(session.get("timeout_s", 120.0)),
        )

    @app.post("/send")
    def send() -> str:
        prompt = (request.form.get("prompt") or "").strip()
        new_chat = bool(request.form.get("new_chat"))
        timeout_s = float(request.form.get("timeout_s") or 120.0)
        session["new_chat"] = new_chat
        session["timeout_s"] = timeout_s
        session["draft"] = ""

        if not prompt:
            session["notice"] = "prompt vacío"
            session["notice_ok"] = False
            return redirect(url_for("index"))

        msgs = _msgs()
        msgs.append({"role": "user", "text": prompt})
        _set_msgs(msgs)

        try:
            data = api_post_ask(cfg, prompt=prompt, new_chat=new_chat, timeout_s=timeout_s)
        except Exception as e:
            session["notice"] = f"Error llamando API: {e}"
            session["notice_ok"] = False
            return redirect(url_for("index"))

        if not data.get("ok"):
            session["notice"] = json.dumps(data, ensure_ascii=False, indent=2)
            session["notice_ok"] = False
            return redirect(url_for("index"))

        resp = str(data.get("response") or "").strip()
        msgs = _msgs()
        msgs.append({"role": "bot", "text": resp or "(sin respuesta)"})
        _set_msgs(msgs)

        session["notice"] = f"OK (elapsed_s={data.get('elapsed_s')})"
        session["notice_ok"] = True
        return redirect(url_for("index"))

    @app.post("/clear")
    def clear() -> str:
        sid = _sid()
        with _STORE_LOCK:
            _MESSAGE_STORE[sid] = []
        session["notice"] = "Chat limpio."
        session["notice_ok"] = True
        return redirect(url_for("index"))

    return app


def main() -> int:
    host = os.environ.get("COMET_AUTO_CLIENT_HOST", "0.0.0.0")
    port = int(os.environ.get("COMET_AUTO_CLIENT_PORT", "5050"))
    app = create_app()
    app.run(host=host, port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
