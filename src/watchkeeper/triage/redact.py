"""Mask secrets and personal data before anything is sent to the model."""

from __future__ import annotations

import re

PATTERNS = [
    # postgres://user:password@host -> keep user and host, drop the password
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@", re.I), r"\1[REDACTED]@"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk-ant-|sk-|xox[abprs]-)[A-Za-z0-9_-]{8,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"), "https://hooks.slack.com/services/[REDACTED]"),
    (re.compile(r"((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;&\"']+", re.I), r"\1[REDACTED]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"), "[PHONE]"),
]


def redact(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text
