from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .cdp import CDPClient, CDPError
from .config import AppConfig


Status = Literal["idle", "working", "completed"]


@dataclass
class AgentStatus:
    status: Status
    steps: list[str]
    current_step: str
    response: str
    has_stop_button: bool
    is_stable: bool


def _http_json(url: str, method: str = "GET", timeout_s: float = 5.0) -> Any:
    req = urllib.request.Request(url=url, method=method)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    return json.loads(data)


def _port_ready(port: int) -> bool:
    try:
        _http_json(f"http://127.0.0.1:{port}/json/version", timeout_s=1.5)
        return True
    except Exception:
        return False


def _default_comet_paths() -> list[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    candidates = [
        str(Path(local) / "Perplexity" / "Comet" / "Application" / "comet.exe"),
        str(Path(roaming) / "Perplexity" / "Comet" / "Application" / "comet.exe"),
        r"C:\Program Files\Perplexity\Comet\Application\comet.exe",
        r"C:\Program Files (x86)\Perplexity\Comet\Application\comet.exe",
    ]
    return [p for p in candidates if p and Path(p).exists()]


class CometController:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.cdp = CDPClient()
        self._active_target_id: str | None = None
        self._last_response_text = ""
        self._stable_count = 0
        self._stability_threshold = 2

    @staticmethod
    def detect_comet_exe() -> str | None:
        paths = _default_comet_paths()
        return paths[0] if paths else None

    def start_comet(self) -> None:
        if _port_ready(self.cfg.debug_port):
            return

        if not self.cfg.auto_launch:
            raise RuntimeError(
                f"Comet no está accesible en el puerto {self.cfg.debug_port}. "
                "Activa auto_launch o abre Comet con --remote-debugging-port."
            )

        exe = self.cfg.comet_exe
        if not Path(exe).exists():
            raise RuntimeError(f"No existe comet.exe en: {exe}")

        if self.cfg.restart_if_no_debug_port:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "comet.exe"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                pass

        creationflags = 0
        if hasattr(subprocess, "DETACHED_PROCESS") and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        subprocess.Popen(
            [exe, f"--remote-debugging-port={self.cfg.debug_port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        deadline = time.time() + 20
        while time.time() < deadline:
            if _port_ready(self.cfg.debug_port):
                return
            time.sleep(0.5)
        raise RuntimeError("Timeout esperando a que Comet exponga el puerto de debug.")

    def list_targets(self) -> list[dict[str, Any]]:
        return _http_json(f"http://127.0.0.1:{self.cfg.debug_port}/json/list")

    def new_tab(self, url: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(url, safe="")
        return _http_json(f"http://127.0.0.1:{self.cfg.debug_port}/json/new?{encoded}", method="PUT")

    def connect_best_tab(self) -> None:
        self.start_comet()
        targets = self.list_targets()

        page_targets = [t for t in targets if t.get("type") == "page"]
        if not page_targets:
            t = self.new_tab(self.cfg.perplexity_url)
            page_targets = [t]

        perplexity = next((t for t in page_targets if "perplexity.ai" in (t.get("url") or "")), None)
        chosen = perplexity or page_targets[0]

        ws_url = chosen.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("No webSocketDebuggerUrl en el target seleccionado.")

        self.cdp.connect(ws_url)
        self._active_target_id = chosen.get("id")

        for method in ["Page.enable", "Runtime.enable", "DOM.enable", "Network.enable"]:
            try:
                self.cdp.call(method, timeout_s=10)
            except CDPError:
                pass

        if "perplexity.ai" not in (chosen.get("url") or ""):
            self.navigate(self.cfg.perplexity_url, wait_for_load=True)

    def ensure_connected(self) -> None:
        try:
            self.cdp.call("Runtime.evaluate", {"expression": "1+1", "returnByValue": True}, timeout_s=3)
        except Exception:
            self.connect_best_tab()

    def navigate(self, url: str, wait_for_load: bool = True) -> None:
        self.ensure_connected()
        self.cdp.call("Page.navigate", {"url": url}, timeout_s=10)
        if wait_for_load:
            try:
                self.cdp.wait_for_event("Page.loadEventFired", timeout_s=15)
            except Exception:
                pass

    def _eval(self, expression: str, timeout_s: float = 15.0) -> Any:
        self.ensure_connected()
        result = self.cdp.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout_s=timeout_s,
        )
        return result.get("result", {}).get("value")

    def reset_stability(self) -> None:
        self._last_response_text = ""
        self._stable_count = 0

    def _update_stability(self, response: str) -> bool:
        if response and len(response) > 50:
            if response == self._last_response_text:
                self._stable_count += 1
            else:
                self._stable_count = 0
                self._last_response_text = response
            return self._stable_count >= self._stability_threshold
        return False

    def send_prompt(self, prompt: str) -> None:
        self.ensure_connected()
        typed = self._eval(
            f"""
            (() => {{
              const prompt = {json.dumps(prompt)};
              const el = document.querySelector('[contenteditable="true"]');
              if (el) {{
                el.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, prompt);
                return true;
              }}
              const ta = document.querySelector('textarea');
              if (ta) {{
                ta.focus();
                ta.value = prompt;
                ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                return true;
              }}
              return false;
            }})()
            """
        )
        if typed is not True:
            raise RuntimeError("No se encontró input para escribir el prompt. ¿Estás en Perplexity?")

        time.sleep(0.3)
        self._eval(
            """
            (() => {
              const el = document.querySelector('[contenteditable="true"]') || document.querySelector('textarea');
              if (!el) return false;
              el.focus();
              const enterDown = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true });
              el.dispatchEvent(enterDown);
              const enterUp = new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true });
              el.dispatchEvent(enterUp);
              return true;
            })()
            """
        )

        time.sleep(0.8)
        submitted = self._eval(
            """
            (() => {
              const el = document.querySelector('[contenteditable="true"]');
              if (el && el.innerText.trim().length < 5) return true;
              const hasLoading = document.querySelector('[class*="animate-spin"], [class*="animate-pulse"]') !== null;
              const hasThinking = document.body && document.body.innerText.includes('Thinking');
              return hasLoading || hasThinking;
            })()
            """
        )
        if submitted:
            return

        clicked = self._eval(
            """
            (() => {
              const selectors = [
                'button[aria-label*="Submit"]',
                'button[aria-label*="Send"]',
                'button[aria-label*="Ask"]',
                'button[type="submit"]',
              ];
              for (const sel of selectors) {
                const btn = document.querySelector(sel);
                if (btn && !btn.disabled && btn.offsetParent !== null) {
                  btn.click();
                  return true;
                }
              }
              return false;
            })()
            """
        )
        if not clicked:
            raise RuntimeError("No se pudo enviar el prompt (Enter/click fallaron).")

    def get_agent_status(self) -> AgentStatus:
        self.ensure_connected()
        payload = self._eval(
            """
            (() => {
              const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';

              let hasStop = false;
              for (const btn of document.querySelectorAll('button')) {
                const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                const txt = (btn.innerText || '').toLowerCase();
                const isStop = aria.includes('stop') || aria.includes('cancel') || txt === 'stop' || btn.querySelector('svg rect');
                if (isStop && btn.offsetParent !== null && !btn.disabled) { hasStop = true; break; }
              }

              const hasLoading = document.querySelector('[class*="animate-spin"], [class*="animate-pulse"], [class*="loading"], [class*="thinking"]') !== null;
              const hasFollowup = bodyText.includes('Ask a follow-up') || bodyText.includes('Ask follow-up');

              let status = 'idle';
              if (hasStop || hasLoading) status = 'working';

              // Extract response from prose blocks (most reliable generic fallback)
              const proseEls = [...document.querySelectorAll('[class*="prose"]')];
              const texts = proseEls
                .filter(el => {
                  if (el.closest('nav, aside, header, footer, form, [contenteditable]')) return false;
                  const t = (el.innerText || '').trim();
                  if (!t) return false;
                  const uiStarts = ['Library','Discover','Spaces','Finance','Account','Upgrade','Home','Search'];
                  if (uiStarts.some(s => t.startsWith(s))) return false;
                  return t.length > 30;
                })
                .map(el => el.innerText.trim());

              let response = '';
              if (texts.length > 0) response = texts.slice(-3).join('\\n\\n');

              // Basic "steps" scraping (best-effort)
              const steps = [];
              const stepCandidates = ['Preparing', 'Navigating', 'Clicking', 'Scrolling', 'Reading', 'Extracting', 'Answering'];
              for (const s of stepCandidates) {
                if (bodyText.includes(s)) steps.push(s);
              }

              // Completion heuristic
              if (!hasStop && !hasLoading && response && response.length > 50 && hasFollowup) status = 'completed';
              return { status, steps, currentStep: steps.length ? steps[steps.length-1] : '', response, hasStopButton: hasStop };
            })()
            """
        )
        if not isinstance(payload, dict):
            payload = {}

        response = str(payload.get("response") or "").strip()
        is_stable = self._update_stability(response)

        status: Status = payload.get("status") if payload.get("status") in ("idle", "working", "completed") else "working"
        if is_stable and response and not bool(payload.get("hasStopButton")):
            status = "completed"

        steps = payload.get("steps") or []
        if not isinstance(steps, list):
            steps = []
        steps = [str(s) for s in steps][-5:]

        current_step = str(payload.get("currentStep") or "")
        has_stop = bool(payload.get("hasStopButton"))

        return AgentStatus(
            status=status,
            steps=steps,
            current_step=current_step,
            response=response[:8000],
            has_stop_button=has_stop,
            is_stable=is_stable,
        )

    def ask(self, prompt: str, new_chat: bool = False, timeout_s: float = 120.0) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt vacío")

        self.reset_stability()
        self.ensure_connected()
        if new_chat:
            self.navigate(self.cfg.perplexity_url, wait_for_load=True)
            time.sleep(1.0)

        self.send_prompt(prompt)

        deadline = time.time() + timeout_s
        last_activity = time.time()
        prev_response = ""
        saw_response = False

        while time.time() < deadline:
            st = self.get_agent_status()
            if st.response and st.response != prev_response:
                prev_response = st.response
                last_activity = time.time()
                saw_response = True

            if st.status == "completed" and saw_response and st.response:
                return st.response

            if st.is_stable and saw_response and st.response and not st.has_stop_button:
                return st.response

            if time.time() - last_activity > 6 and saw_response and st.response and len(st.response) > 100 and not st.has_stop_button:
                return st.response

            time.sleep(1.0)

        # timeout: return best effort
        st = self.get_agent_status()
        if st.response:
            return st.response
        raise RuntimeError("Timeout sin respuesta.")
