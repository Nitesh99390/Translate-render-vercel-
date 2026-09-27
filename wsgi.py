"""
PythonAnywhere entry point (WSGI).
==================================
PythonAnywhere only speaks WSGI, so the ASGI FastAPI app is wrapped with a2wsgi.

Setup on PythonAnywhere (free account is enough):
  1. Bash console:
        git clone https://github.com/Nitesh99390/Translate-render-vercel-.git worker
        cd worker && pip3 install --user -r requirements.txt
  2. Web tab → Add a new web app → Manual configuration → Python 3.10+
  3. Edit the WSGI configuration file and replace its content with:

        import sys, os
        sys.path.insert(0, "/home/<username>/worker")
        # os.environ["WORKER_SECRET"] = "same-as-bot"     # optional
        from wsgi import application

  4. Reload the web app. Worker URL: https://<username>.pythonanywhere.com

Free accounts reach the internet through proxy.server:3128 — PythonAnywhere
already exports HTTP(S)_PROXY, and app.py honours them (trust_env=True).
*.googleapis.com is on the PythonAnywhere allowlist, so translate.googleapis.com works.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Optional .env next to this file (PythonAnywhere has no env-var UI for web apps)
_env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_file):
    with open(_env_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split("#", 1)[0].strip().strip('"').strip("'"))

from a2wsgi import ASGIMiddleware  # noqa: E402

from app import app as _asgi_app  # noqa: E402

# a2wsgi runs the ASGI app in its own event loop thread; wait_time bounds a
# single request (the master's REQUEST_TIMEOUT is well below this).
application = ASGIMiddleware(_asgi_app, wait_time=120.0)
