"""Short-lived HMAC capability used only between backend and Revit Bridge."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from backend.domain.errors import PolicyFailure


def issue_approval_token(
    secret: str,
    *,
    build_id: str,
    action: str,
    ttl_seconds: int = 300,
    wall_model_sha256: str | None = None,
    script_sha256: str | None = None,
    target_model_path: str | None = None,
    source_model_sha256: str | None = None,
    approval_digest: str | None = None,
) -> str:
    if len(secret) < 32:
        raise PolicyFailure("Revit approval secret must contain at least 32 characters")
    claims: dict[str, Any] = {
        "build_id": build_id,
        "action": action,
        "exp": int(time.time()) + max(30, min(ttl_seconds, 900)),
    }
    if wall_model_sha256 is not None:
        claims["wall_model_sha256"] = wall_model_sha256
    if script_sha256 is not None:
        claims["script_sha256"] = script_sha256
    if target_model_path is not None:
        claims["target_model_path"] = normalize_target_path(target_model_path)
    if source_model_sha256 is not None:
        claims["source_model_sha256"] = source_model_sha256
    if approval_digest is not None:
        claims["approval_digest"] = approval_digest
    payload = json.dumps(
        claims, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).digest()
    return encoded.decode("ascii") + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def verify_approval_token(
    token: str,
    secret: str,
    *,
    build_id: str,
    action: str,
    wall_model_sha256: str | None = None,
    script_sha256: str | None = None,
    target_model_path: str | None = None,
    source_model_sha256: str | None = None,
    approval_digest: str | None = None,
) -> None:
    try:
        encoded_text, signature_text = token.split(".", 1)
        encoded = encoded_text.encode("ascii")
        supplied = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("signature mismatch")
        payload = json.loads(base64.urlsafe_b64decode(
            encoded_text + "=" * (-len(encoded_text) % 4)
        ))
        if not isinstance(payload, dict):
            raise ValueError("approval payload must be an object")
        if payload.get("build_id") != build_id or payload.get("action") != action:
            raise ValueError("approval scope mismatch")
        expected_claims = {
            "wall_model_sha256": wall_model_sha256,
            "script_sha256": script_sha256,
            "target_model_path": (
                normalize_target_path(target_model_path)
                if target_model_path is not None else None
            ),
            "source_model_sha256": source_model_sha256,
            "approval_digest": approval_digest,
        }
        for name, expected_value in expected_claims.items():
            if expected_value is None:
                continue
            supplied_value = payload.get(name)
            if not isinstance(supplied_value, str) or not hmac.compare_digest(
                supplied_value, str(expected_value)
            ):
                raise ValueError("approval binding mismatch")
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("approval expired")
    except (
        AttributeError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        UnicodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise PolicyFailure("invalid or expired Revit approval token") from exc


def normalize_target_path(path: str) -> str:
    """Return the platform-normalized path used by approval bindings."""

    if not isinstance(path, str) or not path.strip():
        raise ValueError("target model path is required")
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))


def approval_digest(approval: Any) -> str:
    """Hash the complete human approval record for capability binding."""

    if hasattr(approval, "model_dump"):
        value = approval.model_dump(mode="json")
    elif isinstance(approval, dict):
        value = dict(approval)
    else:
        raise ValueError("approval record must be a mapping or Pydantic model")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
