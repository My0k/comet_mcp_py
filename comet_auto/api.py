from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .comet import CometController
from .config import AppConfig, load_config, save_config


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(data)


def _read_json(handler: BaseHTTPRequestHandler, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    if length > max_bytes:
        raise ValueError("Body too large")
    raw = handler.rfile.read(length)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Invalid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("JSON root must be an object")
    return obj


def _get_api_key(handler: BaseHTTPRequestHandler) -> str:
    auth = (handler.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (handler.headers.get("X-API-Key") or "").strip()


def _prompt_config_interactive() -> AppConfig:
    detected = CometController.detect_comet_exe() or ""
    print("No existe config.json. Configuración inicial (API).")
    comet_exe = input(f"Ruta a comet.exe [{detected}]: ").strip() or detected
    if not comet_exe:
        raise SystemExit("Ruta comet.exe requerida.")

    port_raw = input("Puerto debug [9223]: ").strip()
    debug_port = int(port_raw) if port_raw else 9223

    auto_launch_raw = input("Auto-lanzar Comet? [Y/n]: ").strip().lower()
    auto_launch = auto_launch_raw not in ("n", "no", "0", "false")

    restart_raw = input("Reiniciar Comet si falta debug-port/flags? [Y/n]: ").strip().lower()
    restart = restart_raw not in ("n", "no", "0", "false")

    return AppConfig(
        comet_exe=comet_exe,
        debug_port=debug_port,
        auto_launch=auto_launch,
        restart_if_no_debug_port=restart,
    )


class _State:
    def __init__(self, comet: CometController, api_key: str | None) -> None:
        self.comet = comet
        self.lock = threading.Lock()
        self.api_key = api_key


def make_handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CometAutoAPI/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Quieter by default; enable via COMET_AUTO_API_LOG=1
            if os.environ.get("COMET_AUTO_API_LOG", "").strip() not in ("", "0", "false", "False"):
                super().log_message(fmt, *args)

        def do_OPTIONS(self) -> None:
            _json_response(self, 200, {"ok": True})

        def _auth_ok(self) -> bool:
            if not state.api_key:
                return True
            return _get_api_key(self) == state.api_key

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/health":
                if not self._auth_ok():
                    _json_response(self, 401, {"ok": False, "error": "unauthorized"})
                    return
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "comet_auto",
                        "host": self.server.server_address[0],  # type: ignore[attr-defined]
                        "port": self.server.server_address[1],  # type: ignore[attr-defined]
                    },
                )
                return

            _json_response(self, 404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            if not self._auth_ok():
                _json_response(self, 401, {"ok": False, "error": "unauthorized"})
                return

            if self.path.rstrip("/") != "/ask":
                _json_response(self, 404, {"ok": False, "error": "not_found"})
                return

            try:
                body = _read_json(self)
                prompt = str(body.get("prompt") or "").strip()
                if not prompt:
                    raise ValueError("prompt requerido")
                new_chat = bool(body.get("new_chat", False))
                timeout_s = float(body.get("timeout_s", 120.0))
                if timeout_s <= 0:
                    timeout_s = 120.0
            except Exception as e:
                _json_response(self, 400, {"ok": False, "error": str(e)})
                return

            started = time.time()
            with state.lock:
                try:
                    response = state.comet.ask(prompt, new_chat=new_chat, timeout_s=timeout_s)
                except Exception as e:
                    _json_response(
                        self,
                        500,
                        {
                            "ok": False,
                            "error": str(e),
                            "elapsed_s": round(time.time() - started, 3),
                        },
                    )
                    return

            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "response": response,
                    "completed": True,
                    "elapsed_s": round(time.time() - started, 3),
                },
            )

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Comet Auto API (LAN)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    parser.add_argument("--api-key", default="", help="API key (optional). If set, requires Bearer/X-API-Key.")
    parser.add_argument("--setup", action="store_true", help="Force interactive config setup")
    args = parser.parse_args(argv)

    cfg = load_config()
    if cfg is None or args.setup:
        cfg = _prompt_config_interactive()
        save_config(cfg)

    if not cfg.comet_exe:
        raise SystemExit("Config inválida: comet_exe vacío.")

    comet = CometController(cfg)

    api_key = args.api_key.strip() or os.environ.get("COMET_AUTO_API_KEY", "").strip() or None
    state = _State(comet, api_key)

    httpd = ThreadingHTTPServer((args.host, int(args.port)), make_handler(state))

    bind_host, bind_port = httpd.server_address
    if api_key:
        print(f"[api] API key enabled (COMET_AUTO_API_KEY).")
    print(f"[api] Listening on http://{bind_host}:{bind_port}")
    print(f"[api] Endpoints: GET /health, POST /ask")
    print(f"[api] Example: curl -X POST http://{bind_host}:{bind_port}/ask -H \"Content-Type: application/json\" -d \"{{\\\"prompt\\\":\\\"hola\\\"}}\"")

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
