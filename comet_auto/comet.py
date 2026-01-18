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
    has_loading: bool
    has_followup_ui: bool
    error_type: str
    error_text: str
    has_retry_button: bool
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


def _is_internal_url(url: str) -> bool:
    u = (url or "").lower().strip()
    return u.startswith(("chrome://", "edge://", "devtools://", "about:", "chrome-extension://"))


class CometController:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.cdp = CDPClient()
        self._active_target_id: str | None = None
        self._last_response_text = ""
        self._stable_count = 0
        self._stability_threshold = 3

    @staticmethod
    def detect_comet_exe() -> str | None:
        paths = _default_comet_paths()
        return paths[0] if paths else None

    def start_comet(self, force_restart: bool = False) -> None:
        if _port_ready(self.cfg.debug_port):
            if not force_restart:
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

        args = [
            exe,
            f"--remote-debugging-port={self.cfg.debug_port}",
            "--remote-allow-origins=*",
        ]
        subprocess.Popen(
            args,
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

        page_targets = [t for t in targets if t.get("type") == "page" and not _is_internal_url(str(t.get("url") or ""))]
        if not page_targets:
            t = self.new_tab(self.cfg.perplexity_url)
            page_targets = [t]

        perplexity = next((t for t in page_targets if "perplexity.ai" in (t.get("url") or "")), None)
        chosen = perplexity or page_targets[0]

        ws_url = chosen.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("No webSocketDebuggerUrl en el target seleccionado.")

        try:
            self.cdp.connect(ws_url)
        except Exception as e:
            msg = str(e)
            looks_like_allow_origins = (
                "Handshake status 403" in msg
                or "403 Forbidden" in msg
                or "Rejected an incoming WebSocket connection" in msg
                or "remote-allow-origins" in msg
            )
            if looks_like_allow_origins and self.cfg.restart_if_no_debug_port and self.cfg.auto_launch:
                # Comet was started without --remote-allow-origins, restart with required flag.
                self.start_comet(force_restart=True)
                targets = self.list_targets()
                page_targets = [t for t in targets if t.get("type") == "page"]
                perplexity = next((t for t in page_targets if "perplexity.ai" in (t.get("url") or "")), None)
                chosen = perplexity or (page_targets[0] if page_targets else self.new_tab(self.cfg.perplexity_url))
                ws_url = chosen.get("webSocketDebuggerUrl")
                if not ws_url:
                    raise RuntimeError("No webSocketDebuggerUrl en el target seleccionado tras reinicio.") from e
                self.cdp.connect(ws_url)
                self._active_target_id = chosen.get("id")
            else:
                raise
        for method in ["Page.enable", "Runtime.enable", "DOM.enable", "Network.enable"]:
            try:
                self.cdp.call(method, timeout_s=10)
            except CDPError:
                pass

        self._active_target_id = chosen.get("id")
        self.ensure_perplexity_ready(fresh=False)

    def ensure_connected(self) -> None:
        try:
            self.cdp.call("Runtime.evaluate", {"expression": "1+1", "returnByValue": True}, timeout_s=3)
        except Exception:
            self.connect_best_tab()

    def ensure_perplexity_ready(self, fresh: bool) -> None:
        self.ensure_connected()

        # Ensure we're on Perplexity
        try:
            url = str(self._eval("window.location.href", timeout_s=5) or "")
        except Exception:
            url = ""
        if fresh:
            # For "new chat", always go back to Perplexity home to reset UI state.
            self.navigate(self.cfg.perplexity_url, wait_for_load=True)
        elif "perplexity.ai" not in url:
            self.navigate(self.cfg.perplexity_url, wait_for_load=True)

        # Wait until an input is present and visible
        deadline = time.time() + 20
        last_info = ""
        while time.time() < deadline:
            info = self._eval(
                """
                (() => {
                  const url = window.location.href;
                  const ready = document.readyState;
                  const selectors = [
                    '[contenteditable="true"]',
                    'textarea[placeholder*="Ask"]',
                    'textarea[placeholder*="Search"]',
                    'textarea[placeholder*="¿Qué"]',
                    'textarea',
                    'input[type="text"]'
                  ];
                  let sel = null;
                  let visible = false;
                  for (const s of selectors) {
                    const el = document.querySelector(s);
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const isVisible = r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    if (isVisible) { sel = s; visible = true; break; }
                  }
                  return { url, ready, sel, visible };
                })()
                """,
                timeout_s=5,
            )
            if isinstance(info, dict):
                last_info = json.dumps(info, ensure_ascii=False)
                if info.get("visible") and info.get("sel"):
                    break
            time.sleep(0.5)
        else:
            # Try a fresh tab as last resort
            t = self.new_tab(self.cfg.perplexity_url)
            ws_url = t.get("webSocketDebuggerUrl")
            if not ws_url:
                raise RuntimeError(f"No pude encontrar el input de Perplexity. Estado: {last_info}")
            self.cdp.connect(ws_url)
            for method in ["Page.enable", "Runtime.enable", "DOM.enable", "Network.enable"]:
                try:
                    self.cdp.call(method, timeout_s=10)
                except CDPError:
                    pass

        if fresh:
            # Clear any residual text in the input
            self._eval(
                """
                (() => {
                  const el = document.querySelector('[contenteditable="true"]');
                  if (el) {
                    el.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, '');
                    return true;
                  }
                  const ta = document.querySelector('textarea') || document.querySelector('input[type="text"]');
                  if (ta) {
                    ta.focus();
                    ta.value = '';
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    return true;
                  }
                  return false;
                })()
                """,
                timeout_s=5,
            )

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
        prompt_json = json.dumps(prompt)
        typed = self._eval(
            f"""
            (() => {{
              const prompt = {prompt_json};
              const selectors = [
                '[contenteditable="true"]',
                'textarea[placeholder*="Ask"]',
                'textarea[placeholder*="Search"]',
                'textarea[placeholder*="¿Qué"]',
                'textarea',
                'input[type="text"]'
              ];
              let target = null;
              for (const s of selectors) {{
                const el = document.querySelector(s);
                if (!el) continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                target = el;
                break;
              }}
              if (!target) return {{ ok: false, reason: 'no input' }};

              target.focus();

              // Prefer execCommand for contenteditable (works with React/Vue), fallback to direct assignment + input event
              if (target.getAttribute && target.getAttribute('contenteditable') === 'true') {{
                try {{
                  document.execCommand('selectAll', false, null);
                  document.execCommand('insertText', false, prompt);
                }} catch {{
                  target.innerText = '';
                  target.innerText = prompt;
                  target.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: prompt }}));
                }}
              }} else {{
                target.value = prompt;
                target.dispatchEvent(new Event('input', {{ bubbles: true }}));
              }}

              // Verify content exists
              const hasText = (() => {{
                const ce = document.querySelector('[contenteditable="true"]');
                if (ce && ce.innerText && ce.innerText.trim().length > 0) return true;
                const ta = document.querySelector('textarea');
                if (ta && ta.value && ta.value.trim().length > 0) return true;
                const it = document.querySelector('input[type="text"]');
                if (it && it.value && it.value.trim().length > 0) return true;
                return false;
              }})();
              return {{ ok: hasText }};
            }})()
            """,
            timeout_s=10,
        )
        if typed is not True:
            if isinstance(typed, dict) and typed.get("ok") is True:
                pass
            else:
                raise RuntimeError("No se encontró input para escribir el prompt. ¿Estás en Perplexity?")

        time.sleep(0.3)
        self._eval(
            """
            (() => {
              const el = document.querySelector('[contenteditable="true"]') ||
                         document.querySelector('textarea') ||
                         document.querySelector('input[type="text"]');
              if (!el) return false;
              el.focus();
              const down = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true });
              el.dispatchEvent(down);
              const up = new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true });
              el.dispatchEvent(up);
              return true;
            })()
            """,
            timeout_s=5,
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
              // Position-based fallback: rightmost visible button near input
              const inputEl = document.querySelector('[contenteditable="true"]') ||
                              document.querySelector('textarea') ||
                              document.querySelector('input[type="text"]');
              if (inputEl) {
                let parent = inputEl.parentElement;
                const candidates = [];
                for (let i = 0; i < 6 && parent; i++) {
                  for (const btn of parent.querySelectorAll('button')) {
                    if (btn.disabled || btn.offsetParent === null) continue;
                    const rect = btn.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                    const txt = (btn.innerText || '').toLowerCase();
                    if (aria.includes('search') || aria.includes('research') || aria.includes('labs') || aria.includes('learn')) continue;
                    if (aria.includes('attach') || aria.includes('voice') || aria.includes('menu') || aria.includes('more')) continue;
                    if (txt.includes('attach') || txt.includes('voice')) continue;
                    candidates.push({ btn, x: rect.right, y: rect.top });
                  }
                  parent = parent.parentElement;
                }
                if (candidates.length) {
                  candidates.sort((a, b) => b.x - a.x);
                  candidates[0].btn.click();
                  return true;
                }
              }
              return false;
            })()
            """
        )
        if not clicked:
            # Last resort: try dispatching a submit event
            self._eval(
                """
                (() => {
                  const form = document.querySelector('form');
                  if (form) {
                    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                    return true;
                  }
                  return false;
                })()
                """,
                timeout_s=5,
            )

    def get_agent_status(self) -> AgentStatus:
        self.ensure_connected()
        payload = self._eval(
            """
            (() => {
              const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';
              const main = document.querySelector('main') || document.querySelector('[role="main"]') || document.body;
              const mainText = (main && main.innerText) ? main.innerText : bodyText;
              const tailText = mainText.slice(-2500);

              let hasStop = false;
              for (const btn of document.querySelectorAll('button')) {
                const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                const title = (btn.getAttribute('title') || '').toLowerCase();
                const testid = (btn.getAttribute('data-testid') || '').toLowerCase();
                const txt = (btn.innerText || '').toLowerCase();
                const isStop =
                  aria.includes('stop') || aria.includes('cancel') ||
                  title.includes('stop') || title.includes('cancel') ||
                  testid.includes('stop') ||
                  aria.includes('detener') || aria.includes('cancelar') ||
                  title.includes('detener') || title.includes('cancelar') ||
                  txt === 'stop' || txt === 'detener' || txt === 'cancelar' ||
                  btn.querySelector('svg rect');
                if (isStop && btn.offsetParent !== null && !btn.disabled) { hasStop = true; break; }
              }

              const hasLoading =
                document.querySelector('[class*="animate-spin"], [class*="animate-pulse"], [class*="loading"], [class*="thinking"]') !== null ||
                /\\b(thinking|searching|researching|analyzing|loading)\\b/i.test(bodyText) ||
                /\\b(pensando|buscando|investigando|analizando|cargando)\\b/i.test(bodyText);

              const hasFollowup =
                bodyText.includes('Ask a follow-up') ||
                bodyText.includes('Ask follow-up') ||
                bodyText.includes('Ask anything') ||
                bodyText.includes('Type a message') ||
                bodyText.includes('Add details') ||
                bodyText.includes('Preguntar algo') ||
                bodyText.includes('Pregunta algo') ||
                bodyText.includes('Escribe un mensaje') ||
                bodyText.includes('Añadir detalles') ||
                bodyText.includes('Agregar detalles') ||
                bodyText.includes('Pregunta de seguimiento');

              // Detect "omitted" / error states
              let errorType = '';
              let errorText = '';
              // IMPORTANT: only look at the tail of the page so old errors don't trigger on new prompts.
              if (
                /respuesta omitida/i.test(tailText) ||
                /response omitted/i.test(tailText) ||
                /answer omitted/i.test(tailText) ||
                /output omitted/i.test(tailText)
              ) {
                errorType = 'omitted';
                errorText = 'Respuesta omitida';
              } else if (
                /something went wrong/i.test(tailText) ||
                /network error/i.test(tailText) ||
                (/error/i.test(tailText) && /try again|retry/i.test(tailText))
              ) {
                errorType = 'retryable_error';
                errorText = 'Error (reintentar)';
              }

              let hasRetryButton = false;
              if (errorType) {
                for (const btn of document.querySelectorAll('button')) {
                  if (btn.offsetParent === null || btn.disabled) continue;
                  const t = ((btn.innerText || '') + ' ' + (btn.getAttribute('aria-label') || '')).toLowerCase();
                  if (t.includes('try again') || t.includes('retry') || t.includes('regenerate') ||
                      t.includes('reintentar') || t.includes('intentar de nuevo') || t.includes('regenerar')) {
                    hasRetryButton = true;
                    break;
                  }
                }
              }

              let status = 'idle';
              if (hasStop || hasLoading) status = 'working';

              // Extract response
              let response = '';

              // Strategy 1: Content after "X steps completed" marker (agentic final answer)
              const stepsMatch = bodyText.match(/\\d+\\s+(steps?|pasos?)\\s+(completed|completad[oa]s?)/i);
              if (stepsMatch) {
                const markerIndex = bodyText.indexOf(stepsMatch[0]);
                if (markerIndex !== -1) {
                  let after = bodyText.substring(markerIndex + stepsMatch[0].length).trim();
                  after = after.replace(/^[>›→\\s]+/, '').trim();
                  const endMarkers = [
                    'Ask anything', 'Ask a follow-up', 'Ask follow-up', 'Add details', 'Type a message',
                    'Preguntar algo', 'Escribe un mensaje', 'Añadir detalles', 'Agregar detalles', 'Pregunta de seguimiento'
                  ];
                  let endIndex = after.length;
                  for (const m of endMarkers) {
                    const idx = after.indexOf(m);
                    if (idx !== -1 && idx < endIndex) endIndex = idx;
                  }
                  response = after.substring(0, endIndex).trim();
                }
              }

              // Strategy 2: Content after "Reviewed X sources" marker
              if (!response || response.length < 80) {
                const sourcesMatch = bodyText.match(/Reviewed\\s+\\d+\\s+sources?/i);
                if (sourcesMatch) {
                  const markerIndex = bodyText.indexOf(sourcesMatch[0]);
                  if (markerIndex !== -1) {
                    let after = bodyText.substring(markerIndex + sourcesMatch[0].length).trim();
                    const endMarkers = [
                      'Ask anything', 'Ask a follow-up', 'Ask follow-up', 'Add details', 'Type a message',
                      'Preguntar algo', 'Escribe un mensaje', 'Añadir detalles', 'Agregar detalles', 'Pregunta de seguimiento'
                    ];
                    let endIndex = after.length;
                    for (const m of endMarkers) {
                      const idx = after.indexOf(m);
                      if (idx !== -1 && idx < endIndex) endIndex = idx;
                    }
                    response = after.substring(0, endIndex).trim();
                  }
                }
              }

              // Strategy 3: Fallback to prose blocks
              const selectors = [
                '[class*="prose"]',
                '[class*="Prose"]',
                '[class*="markdown"]',
                '[class*="Markdown"]',
                '[data-testid*="answer"]',
                '[class*="answer"]'
              ];
              const candidateEls = [];
              for (const sel of selectors) {
                try { candidateEls.push(...main.querySelectorAll(sel)); } catch {}
              }

              const texts = [...new Set(candidateEls)]
                .filter(el => {
                  if (el.closest('nav, aside, header, footer, form, [contenteditable]')) return false;
                  const t = (el.innerText || '').trim();
                  if (!t) return false;
                  const uiStarts = ['Library','Discover','Spaces','Finance','Account','Upgrade','Home','Search'];
                  if (uiStarts.some(s => t.startsWith(s))) return false;
                  return t.length > 10;
                })
                .map(el => el.innerText.trim());

              if ((!response || response.length < 120) && texts.length > 0) {
                // Take more blocks: answers often split headings + bullet lists
                response = texts.slice(-12).join('\\n\\n');
              }

              // Strategy 4: As a last resort, use main text (trimmed)
              if ((!response || response.length < 120) && main && main.innerText) {
                response = main.innerText.trim();
              }

              // Clean response a bit (UI artifacts)
              if (response) {
                response = response
                  .replace(/View All/gi, '')
                  .replace(/Show more/gi, '')
                  .replace(/Ask a follow-up/gi, '')
                  .replace(/Ask anything\\.*/gi, '')
                  .replace(/Type a message\\.*/gi, '')
                  .replace(/Add details\\.*/gi, '')
                  .replace(/\\n{3,}/g, '\\n\\n')
                  .trim();
              }

              // Basic "steps" scraping (best-effort)
              const steps = [];
              const stepCandidates = ['Preparing', 'Navigating', 'Clicking', 'Scrolling', 'Reading', 'Extracting', 'Answering'];
              for (const s of stepCandidates) {
                if (bodyText.includes(s)) steps.push(s);
              }

              // Completion heuristic (signal-only; stability handled in Python loop)
              if (!errorType && !hasStop && !hasLoading && response && response.length > 120 && hasFollowup) status = 'completed';
              return { status, steps, currentStep: steps.length ? steps[steps.length-1] : '', response, hasStopButton: hasStop, hasLoading, hasFollowup, errorType, errorText, hasRetryButton };
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
        has_loading = bool(payload.get("hasLoading"))
        has_followup_ui = bool(payload.get("hasFollowup"))
        error_type = str(payload.get("errorType") or "")
        error_text = str(payload.get("errorText") or "")
        has_retry_button = bool(payload.get("hasRetryButton"))

        return AgentStatus(
            status=status,
            steps=steps,
            current_step=current_step,
            response=response[:8000],
            has_stop_button=has_stop,
            has_loading=has_loading,
            has_followup_ui=has_followup_ui,
            error_type=error_type,
            error_text=error_text,
            has_retry_button=has_retry_button,
            is_stable=is_stable,
        )

    def click_retry(self) -> bool:
        try:
            return bool(
                self._eval(
                    """
                    (() => {
                      for (const btn of document.querySelectorAll('button')) {
                        if (btn.offsetParent === null || btn.disabled) continue;
                        const t = ((btn.innerText || '') + ' ' + (btn.getAttribute('aria-label') || '')).toLowerCase();
                        if (t.includes('try again') || t.includes('retry') || t.includes('regenerate') ||
                            t.includes('reintentar') || t.includes('intentar de nuevo') || t.includes('regenerar')) {
                          btn.click();
                          return true;
                        }
                      }
                      return false;
                    })()
                    """,
                    timeout_s=5,
                )
            )
        except Exception:
            return False

    def _normalize_prompt(self, prompt: str) -> str:
        # Similar to example_mcp_comet: collapse bullets/newlines for browser input reliability
        p = prompt.strip()
        p = "\n".join(line.lstrip("-*• ").rstrip() for line in p.splitlines())
        p = " ".join(p.split())
        return p.strip()

    def _maybe_make_agentic(self, prompt: str) -> str:
        has_url = "http://" in prompt or "https://" in prompt
        has_website_ref = any(
            w in prompt.lower()
            for w in ["go to", "visit", "navigate", "open", "browse", "check", "look at", "click", "fill", "submit", "login", "sign in"]
        )
        has_site_names = any(s in prompt.lower() for s in [".com", ".org", ".io", ".net", ".ai", "website", "webpage", "page", "site"])
        needs_agentic = has_url or has_website_ref or has_site_names
        if not needs_agentic:
            return prompt

        lower = prompt.lower()
        already_agentic = lower.startswith(("use your browser", "using your browser", "open a browser", "navigate to", "browse to"))
        if already_agentic:
            return prompt

        if has_url:
            # Extract first URL and reframe
            for token in prompt.split():
                if token.startswith("http://") or token.startswith("https://"):
                    url = token
                    rest = prompt.replace(url, "").strip()
                    return f"Use your browser to navigate to {url} and {rest or 'tell me what you find there'}"
        return f"Use your browser to {prompt}"

    def ask(self, prompt: str, new_chat: bool = False, timeout_s: float = 120.0) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt vacío")

        prompt = self._maybe_make_agentic(self._normalize_prompt(prompt))

        self.reset_stability()
        self.ensure_perplexity_ready(fresh=new_chat)
        if new_chat:
            # give the UI a moment to settle
            time.sleep(0.8)

        baseline = self.get_agent_status()
        baseline_response = baseline.response

        self.send_prompt(prompt)

        deadline = time.time() + timeout_s
        sent_at = time.time()
        last_activity = time.time()
        prev_response = ""
        saw_response = False
        done_candidate_at: float | None = None
        done_candidate_response: str = ""
        grace_s = 3.0
        seen_working = False
        resubmit_attempted = False

        while time.time() < deadline:
            st = self.get_agent_status()
            if st.has_loading or st.has_stop_button:
                seen_working = True

            if st.response and st.response != prev_response:
                prev_response = st.response
                last_activity = time.time()
                saw_response = True
                done_candidate_at = None

            # Handle omitted / retryable errors
            should_consider_error = seen_working or saw_response or (st.response and st.response != baseline_response)
            if st.error_type and should_consider_error:
                if st.has_retry_button and self.click_retry():
                    self.reset_stability()
                    prev_response = ""
                    saw_response = False
                    done_candidate_at = None
                    done_candidate_response = ""
                    last_activity = time.time()
                    seen_working = False
                    time.sleep(1.0)
                    continue
                raise RuntimeError(f"Perplexity devolvió '{st.error_text or st.error_type}'.")

            # If nothing seems to start (common on first send / new chat), try one resubmit.
            if not seen_working and not saw_response and not resubmit_attempted and (time.time() - sent_at) > 6:
                try:
                    self._eval(
                        """
                        (() => {
                          const el = document.querySelector('[contenteditable="true"]') ||
                                     document.querySelector('textarea') ||
                                     document.querySelector('input[type="text"]');
                          if (!el) return false;
                          el.focus();
                          const down = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true });
                          el.dispatchEvent(down);
                          const up = new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true });
                          el.dispatchEvent(up);

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
                              break;
                            }
                          }
                          return true;
                        })()
                        """,
                        timeout_s=5,
                    )
                except Exception:
                    pass
                resubmit_attempted = True

            now = time.time()
            idle_s = now - last_activity

            # Candidate "done" conditions (don't return immediately; confirm with grace window)
            is_done_signal = (
                st.status == "completed"
                or (st.is_stable and len(st.response) > 120)
                or (idle_s > 8 and len(st.response) > 200)
            )
            if saw_response and st.response and not st.has_stop_button and not st.has_loading and is_done_signal:
                if done_candidate_at is None or done_candidate_response != st.response:
                    done_candidate_at = now
                    done_candidate_response = st.response
                elif now - done_candidate_at >= grace_s:
                    return st.response
            else:
                done_candidate_at = None

            time.sleep(1.0)

        # timeout: return best effort
        st = self.get_agent_status()
        if not saw_response and not seen_working:
            raise RuntimeError("No se detectó actividad ni respuesta nueva (posible fallo al enviar el prompt).")
        if st.response and (saw_response or st.response != baseline_response):
            return st.response
        raise RuntimeError("Timeout sin respuesta.")
