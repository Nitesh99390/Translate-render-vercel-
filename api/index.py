"""Vercel serverless entry point — re-exports the FastAPI app from app.py.

Vercel auto-detects `api/*.py` files exposing an ASGI `app` object.
`vercel.json` rewrites every path to this function.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401
