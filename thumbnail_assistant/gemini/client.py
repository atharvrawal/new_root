"""Thin, stateless wrapper around the Gemini API.

Deliberately minimal, same philosophy as capture.py: this module's only
job is "take queued images + prompt text, return one response". No
conversation history is kept or resent - every send() call is fully
independent, per the product requirement (each queued batch is one-shot).

Uses Google's ``google-genai`` SDK. Blocking call - the caller (
ui/app_controller.py) is expected to run this off the Qt main thread
(e.g. via QThread or a worker thread) since there's no streaming and a
response may take a few seconds.
"""
from __future__ import annotations

import base64
import logging
from typing import List, Optional

from .models import GeminiResponse

logger = logging.getLogger(__name__)


def send(
    images_base64: List[str],
    prompt_text: str,
    api_key: Optional[str],
    model: str = "gemini-3.6-flash",
) -> GeminiResponse:
    """Bundle every queued image plus the queued prompt text into one
    Gemini API request and return the full response text. Never raises -
    failures come back as a GeminiResponse with .error set, so the UI
    layer can render an error bubble instead of crashing."""
    if not api_key:
        return GeminiResponse(error="No Gemini API key configured. Add one in Settings.")

    if not images_base64 and not prompt_text.strip():
        return GeminiResponse(error="Nothing queued to send.")

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.exception("google-genai is not installed.")
        return GeminiResponse(error="google-genai package is not installed.")

    try:
        client = genai.Client(api_key=api_key)

        parts: list = []
        for img_b64 in images_base64:
            try:
                raw = base64.b64decode(img_b64)
            except Exception:
                logger.exception("Failed to decode a queued image; skipping it.")
                continue
            parts.append(types.Part.from_bytes(data=raw, mime_type="image/png"))

        if prompt_text.strip():
            parts.append(types.Part.from_text(text=prompt_text.strip()))

        if not parts:
            return GeminiResponse(error="Nothing queued to send.")

        response = client.models.generate_content(
            model=model,
            contents=parts,
        )

        text = getattr(response, "text", None)
        if not text:
            logger.warning("Gemini response had no text content.")
            return GeminiResponse(error="Gemini returned an empty response.")

        return GeminiResponse(text=text)

    except Exception as exc:
        logger.exception("Gemini API call failed.")
        return GeminiResponse(error=f"Gemini API call failed: {exc}")
