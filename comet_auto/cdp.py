from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from queue import Queue, Empty
from typing import Any

import websocket


@dataclass(frozen=True)
class CDPError(Exception):
    message: str
    method: str | None = None

    def __str__(self) -> str:
        if self.method:
            return f"{self.method}: {self.message}"
        return self.message


class CDPClient:
    def __init__(self) -> None:
        self._ws: websocket.WebSocket | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._responses: dict[int, dict[str, Any]] = {}
        self._response_cv = threading.Condition()
        self._events: "Queue[dict[str, Any]]" = Queue()
        self._reader: threading.Thread | None = None
        self._closed = True

    def connect(self, ws_url: str, timeout_s: float = 10.0) -> None:
        self.close()
        self._ws = websocket.create_connection(ws_url, timeout=timeout_s)
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None

    def call(self, method: str, params: dict[str, Any] | None = None, timeout_s: float = 15.0) -> dict[str, Any]:
        if self._ws is None or self._closed:
            raise CDPError("Not connected", method=method)

        with self._lock:
            self._id += 1
            call_id = self._id

        payload: dict[str, Any] = {"id": call_id, "method": method}
        if params:
            payload["params"] = params

        self._ws.send(json.dumps(payload))

        deadline = time.time() + timeout_s
        with self._response_cv:
            while call_id not in self._responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise CDPError("Timeout waiting for response", method=method)
                self._response_cv.wait(timeout=remaining)

            msg = self._responses.pop(call_id)

        if "error" in msg:
            err = msg["error"]
            raise CDPError(err.get("message", "Unknown CDP error"), method=method)
        return msg.get("result", {})

    def wait_for_event(self, event_method: str, timeout_s: float = 15.0) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CDPError(f"Timeout waiting for event {event_method}")
            try:
                evt = self._events.get(timeout=min(0.5, remaining))
            except Empty:
                continue
            if evt.get("method") == event_method:
                return evt

    def _read_loop(self) -> None:
        assert self._ws is not None
        while not self._closed:
            try:
                raw = self._ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if "id" in msg:
                with self._response_cv:
                    self._responses[int(msg["id"])] = msg
                    self._response_cv.notify_all()
            elif "method" in msg:
                self._events.put(msg)

