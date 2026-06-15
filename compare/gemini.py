"""Gemini REST client for the document comparator.

Sends PDFs *directly* to Gemini as inline data (no text extraction),
which keeps tables, scans and accents intact.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

# Key lookup: local config.json next to the app, then env var. Never hard-coded.
_LOCAL_CONFIG = Path(__file__).resolve().parent.parent / "config.json"

_FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.0-flash",
]

_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"


class GeminiError(RuntimeError):
    pass


def _load_config() -> dict:
    try:
        if _LOCAL_CONFIG.exists():
            cfg = json.loads(_LOCAL_CONFIG.read_text(encoding="utf-8"))
            if cfg.get("gemini_api_key"):
                return cfg
    except (OSError, ValueError):
        pass
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return {"gemini_api_key": env_key, "gemini_model": "gemini-2.5-flash"}
    raise GeminiError(
        "Nenhuma chave Gemini encontrada. Crie um config.json com "
        "'gemini_api_key', ou defina a variável de ambiente GEMINI_API_KEY."
    )


def default_model() -> str:
    try:
        return _load_config().get("gemini_model", "gemini-2.5-flash")
    except GeminiError:
        return "gemini-2.5-flash"


def _pdf_part(path: str) -> dict:
    data = Path(path).read_bytes()
    return {
        "inline_data": {
            "mime_type": "application/pdf",
            "data": base64.b64encode(data).decode("ascii"),
        }
    }


def generate(parts: list[dict], *, model: str | None = None,
             json_out: bool = False, max_tokens: int = 16384,
             temperature: float = 0.1) -> str:
    """Low-level call. `parts` is the Gemini `contents[0].parts` list (mix of
    {"text": ...} and inline_data PDF parts). Retries 503, falls back models."""
    cfg = _load_config()
    key = cfg["gemini_api_key"]
    first = model or cfg.get("gemini_model", "gemini-2.5-flash")
    candidates = [first] + [m for m in _FALLBACK_MODELS if m != first]

    gen_cfg = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_out:
        gen_cfg["responseMimeType"] = "application/json"

    payload = {"contents": [{"parts": parts}], "generationConfig": gen_cfg}
    body = json.dumps(payload).encode("utf-8")
    last = None

    for m in candidates:
        url = _API.format(model=m, key=key)
        for attempt in range(3):
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=420) as resp:
                    result = json.loads(resp.read())
                cand = (result.get("candidates") or [{}])[0]
                chunks = cand.get("content", {}).get("parts", [])
                text = "".join(c.get("text", "") for c in chunks)
                if not text:
                    last = f"Resposta vazia ({m}); finishReason={cand.get('finishReason')}"
                    break
                return text
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                last = f"HTTP {e.code} ({m}): {detail[:300]}"
                if e.code == 503 and attempt < 2:
                    time.sleep(15 * (attempt + 1))
                    continue
                if e.code in (429, 403, 503):
                    break
                raise GeminiError(last)
            except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
                # network blip or read timeout (big PDFs are slow) — retry, then
                # move to the next model
                last = f"Rede/timeout ({m}): {getattr(e, 'reason', e)}"
                if attempt < 2:
                    time.sleep(8)
                    continue
                break
    raise GeminiError(f"Falha em todos os modelos. Último erro: {last}")


def pdf_parts(paths: list[str]) -> list[dict]:
    """One labelled text part + one inline PDF part per file, in order."""
    parts: list[dict] = []
    for i, p in enumerate(paths, 1):
        parts.append({"text": f"\n=== DOCUMENTO D{i}: {Path(p).name} ===\n"})
        parts.append(_pdf_part(p))
    return parts
