"""
Hugging Face **Gradio SDK / ZeroGPU** entry point (free tier, no card).
=====================================================================
Since mid-2026 Hugging Face requires billing for Docker Spaces and a PRO plan
for Gradio Spaces on `cpu-basic`.  The only free option left is a **Gradio
Space on ZeroGPU** hardware (free personal accounts: up to 2 Spaces, verified
e-mail, account older than 30 days).

This file makes the worker run there:

  * `app.py` (FastAPI) is imported unchanged — `/health`, `/translate` keep
    working exactly like on every other platform, so the bot needs nothing new.
  * A tiny Gradio UI is mounted at `/` so the Space has a landing page and HF's
    "Running" checks are happy.
  * ZeroGPU refuses to start unless at least one `@spaces.GPU` function exists
    *and* the startup report is sent.  We register a never-called probe and send
    the report manually (we serve with uvicorn instead of `demo.launch()`).
    No GPU quota is ever consumed — translation is plain HTTP to Google.

README.md front-matter for the Space:

    sdk: gradio
    app_file: hf_app.py
    python_version: "3.12"

Locally:  python hf_app.py   (→ http://127.0.0.1:7860 ; /health ; POST /translate)
"""

from __future__ import annotations

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ── ZeroGPU startup probe ─────────────────────────────────────────────────────
# `spaces` is pre-installed on ZeroGPU hosts only; guard it everywhere else.
try:
    import spaces  # type: ignore
except ImportError:  # local / Render / Vercel / PythonAnywhere …
    spaces = None  # type: ignore[assignment]

if spaces is not None:

    @spaces.GPU(duration=1)
    def _zerogpu_probe(*_a, **_k):  # pragma: no cover
        """Never called. Exists only so ZeroGPU's startup scan finds a GPU function."""
        return "ok"


def _zerogpu_startup_report() -> None:
    """Tell the ZeroGPU supervisor we are up (normally done inside demo.launch())."""
    if spaces is None:
        return
    try:
        from spaces.zero import startup as zero_startup  # type: ignore
    except ImportError:
        return  # spaces installed but not a ZeroGPU host
    try:
        zero_startup()
        print("✅ ZeroGPU startup report sent")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ ZeroGPU startup report failed: {exc!r}")


# ── FastAPI worker (unchanged) + Gradio UI ────────────────────────────────────
import gradio as gr  # noqa: E402
import uvicorn  # noqa: E402

from app import PLATFORM, WORKER_SECRET, _google, _health_payload, app as api  # noqa: E402

LANGS = {
    "Hindi": "hi", "English": "en", "Bengali": "bn", "Tamil": "ta", "Telugu": "te",
    "Marathi": "mr", "Gujarati": "gu", "Kannada": "kn", "Malayalam": "ml", "Punjabi": "pa",
    "Urdu": "ur", "Spanish": "es", "French": "fr", "German": "de", "Arabic": "ar",
    "Chinese": "zh-CN", "Japanese": "ja", "Russian": "ru", "Portuguese": "pt", "Indonesian": "id",
}


async def _demo_translate(text: str, lang: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    try:
        out = await _google([text], LANGS.get(lang, "hi"), "auto")
        return out[0]
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def _status() -> str:
    return json.dumps(_health_payload(), indent=2)


with gr.Blocks(title="EPUB Translator Worker") as demo:
    gr.Markdown(
        "# 📚 EPUB Translator — Worker node\n"
        "Stateless translate worker for the Telegram bot. "
        "Endpoints: `GET /health` · `POST /translate` "
        f"{'(protected by `X-Worker-Key`)' if WORKER_SECRET else '(no secret set)'}\n\n"
        "Add this Space URL in the bot with `/addworker https://<user>-<space>.hf.space`"
    )
    with gr.Row():
        inp = gr.Textbox(label="Text", lines=4, placeholder="Type something to test the worker…")
        out = gr.Textbox(label="Translation", lines=4)
    with gr.Row():
        lang = gr.Dropdown(list(LANGS), value="Hindi", label="Target language")
        btn = gr.Button("Translate", variant="primary")
    btn.click(_demo_translate, inputs=[inp, lang], outputs=out, api_name="demo_translate")
    with gr.Accordion("Worker status (GET /health)", open=False):
        st = gr.Code(language="json", value=_status)
        gr.Button("Refresh").click(_status, outputs=st)

# API routes are already registered on `api` (imported above) so they take
# precedence; the Gradio UI only receives paths the API does not handle.
# ssr_mode=False: Gradio's SSR proxy breaks for apps mounted at "/" on Spaces.
app = gr.mount_gradio_app(api, demo, path="/", ssr_mode=False)


def _port() -> int:
    # HF routes to 7860 only; elsewhere honour $PORT.
    if os.getenv("SPACE_ID"):
        return 7860
    return int(os.getenv("PORT") or "7860")


if __name__ == "__main__":
    print(f"hf_app: platform={PLATFORM} zerogpu={'yes' if spaces else 'no'} port={_port()}")
    _zerogpu_startup_report()
    uvicorn.run(app, host="0.0.0.0", port=_port(), proxy_headers=True)
