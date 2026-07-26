"""Best-effort webhook notifications with secret redaction."""

from __future__ import annotations

import os


_SECRET_NAME_FRAGMENTS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "WEBHOOK",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "API_KEY",
)


def _redact_secret_values(value: object) -> str:
    """Remove values of secret-like environment variables from arbitrary text."""

    text = str(value)
    secrets = {
        env_value
        for env_name, env_value in os.environ.items()
        if env_value
        and (
            any(
                fragment in env_name.upper()
                for fragment in _SECRET_NAME_FRAGMENTS
            )
            or env_name.upper().endswith("_PAT")
        )
    }
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def notify(text: str, level: str = "info") -> None:
    """POST a notification if configured, swallowing every ordinary failure."""

    del level  # Reserved for notifier-specific routing without changing the payload.
    webhook = os.environ.get("NOTIFY_WEBHOOK")
    if not webhook:
        return

    try:
        import requests

        requests.post(
            webhook,
            json={"text": _redact_secret_values(text)},
            timeout=10,
        )
    except Exception:
        # Notifications are advisory. They must never fail archival work or copy
        # an exception containing credentials into logs.
        return
