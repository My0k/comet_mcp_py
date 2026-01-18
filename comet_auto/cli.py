from __future__ import annotations

import argparse
import os
import sys

from .comet import CometController
from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Comet Auto CLI (prompt -> respuesta)")
    parser.add_argument("prompt", nargs="?", help="Prompt a enviar (si se omite, lee de stdin)")
    parser.add_argument("--new-chat", action="store_true", help="Reinicia conversación (puede navegar a Perplexity home)")
    parser.add_argument("--timeout", type=float, default=120.0, help="Timeout en segundos (default: 120)")
    parser.add_argument("--debug", action="store_true", help="Activa logs de depuración (stderr)")
    args = parser.parse_args(argv)

    if args.debug:
        os.environ["COMET_AUTO_DEBUG"] = "1"

    prompt = args.prompt
    if not prompt:
        prompt = sys.stdin.read().strip()
    if not prompt:
        print("Error: prompt vacío", file=sys.stderr)
        return 2

    cfg = load_config()
    if cfg is None:
        print("Error: falta config.json (ejecuta la GUI una vez para configurar).", file=sys.stderr)
        return 2

    comet = CometController(cfg)
    response = comet.ask(prompt, new_chat=args.new_chat, timeout_s=float(args.timeout))
    print(response.strip(), flush=True)
    print("===COMPLETED===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

