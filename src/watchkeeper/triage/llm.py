"""Minimal Claude Messages API client (stdlib only, so the Lambda stays small)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# USD per million tokens (input, output). Used only for cost estimates in reports
# and evaluations; check https://platform.claude.com/docs/en/about-claude/pricing.
PRICING = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class LLM(Protocol):
    model: str

    def create(self, **request: Any) -> dict: ...


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, usage: dict) -> None:
        self.calls += 1
        self.input_tokens += int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0) or 0)
        self.input_tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
        self.output_tokens += int(usage.get("output_tokens", 0))

    def cost_usd(self, model: str) -> Optional[float]:
        if model not in PRICING:
            return None
        price_in, price_out = PRICING[model]
        return round((self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000, 5)


class LLMError(RuntimeError):
    pass


@dataclass
class AnthropicClient:
    api_key: str
    model: str = "claude-sonnet-5-5"
    timeout: int = 60
    max_retries: int = 2
    url: str = API_URL
    sleep: Any = field(default=time.sleep, repr=False)

    def create(self, **request: Any) -> dict:
        body = json.dumps({"model": self.model, **request}).encode("utf-8")
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - fixed API URL
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code in RETRY_STATUSES and attempt < self.max_retries:
                    self.sleep(2 ** attempt * 2)
                    continue
                raise LLMError(f"Claude API returned HTTP {exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    self.sleep(2 ** attempt * 2)
                    continue
                raise LLMError(f"cannot reach Claude API: {exc}") from None
        raise LLMError("unreachable")
