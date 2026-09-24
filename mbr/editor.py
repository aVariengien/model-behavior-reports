"""Tiny keyword-editing API behind /status/ (Caddy adds the password).

GET  /status/api/models  -> the tracked models and their keywords, as matched right now
POST /status/api/models  -> {"slug", "aliases", "generic", "released"}: save a hand edit and
                            start a background re-key (rematch, rescan, grade, rebuild)
"""

import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

REKEY_UNIT = "modelbehavior-rekey"


def _rekey_running() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", REKEY_UNIT]).returncode == 0


def _start_rekey() -> str:
    if _rekey_running():
        return "already running (your edit will be picked up by the next hourly run)"
    subprocess.run(["systemctl", "reset-failed", REKEY_UNIT], capture_output=True)
    subprocess.run([
        "systemd-run", f"--unit={REKEY_UNIT}", "--collect",
        "--property=EnvironmentFile=/opt/modelbehavior/secrets.env",
        f"--setenv=MBR_ROOT={config.ROOT}", f"--setenv=MBR_OUT={config.OUT_DIR}",
        f"--working-directory={config.ROOT}", str(config.ROOT / ".venv/bin/mbr"), "rekey",
    ], check=True, capture_output=True)
    return "started"


def _clean(words) -> list[str]:
    return sorted({w.strip().lower() for w in words if isinstance(w, str) and len(w.strip()) >= 3})


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") != "/status/api/models":
            return self._json(404, {"error": "not found"})
        config.load_models()
        self._json(200, {"rekey_running": _rekey_running(), "models": [
            {k: m.get(k) for k in ("slug", "name", "released", "aliases", "generic")} for m in config.MODELS]})

    def do_POST(self):
        # JSON only: a cross-site form can't send it without a CORS preflight we never answer.
        if self.path.rstrip("/") != "/status/api/models" or \
                not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json(400, {"error": "bad request"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            slug = data["slug"]
            config.load_models()
            if slug not in config.MODEL_BY_SLUG:
                raise ValueError("unknown model")
            aliases, generic = _clean(data.get("aliases", [])), _clean(data.get("generic", []))
            released = data.get("released") or config.MODEL_BY_SLUG[slug]["released"]
            if not aliases and not generic:
                raise ValueError("a model needs at least one keyword")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", released):
                raise ValueError("released must be YYYY-MM-DD")
        except (ValueError, KeyError, TypeError) as e:
            return self._json(400, {"error": str(e)})
        try:
            extra = json.loads(config.EXTRA_MODELS_PATH.read_text())
        except (OSError, ValueError):
            extra = []
        extra = [e for e in extra if not (e["slug"] == slug and e.get("replace"))]
        extra.append({"slug": slug, "replace": True, "aliases": aliases, "generic": generic, "released": released})
        config.EXTRA_MODELS_PATH.write_text(json.dumps(extra, indent=2))
        config.load_models()
        self._json(200, {"ok": True, "rekey": _start_rekey(),
                         "model": {k: config.MODEL_BY_SLUG[slug].get(k) for k in ("slug", "aliases", "generic", "released")}})

    def log_message(self, *args):
        pass


def serve(port: int = 3004):
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
