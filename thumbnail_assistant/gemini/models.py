"""Small data holder for a Gemini API response, kept separate from
client.py so ui/app_controller.py can import just the type without
pulling in the HTTP dependency."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class GeminiResponse:
    text: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None
