"""FinX bearer-token auth for the read-only MIS report calls (CHO-21 ``finx-auth``).

Two modes (``FINX_AUTH_MODE``), default **direct**:

* **direct** (default) — the configured credential *is* the bearer JWT; it is sent
  verbatim as ``Authorization: <jwt>`` with ``authType: jwt``, ``source: FINX_WEB``.
  This matches confirmed reality: the FINX-issued key is itself a JWT, and the
  public FinX OpenAPI exposes no machine-to-machine exchange endpoint (only a human
  TOTP login, which CHO-21 excludes). It also covers the dev workflow of pasting an
  SSO JWT copied from the logged-in browser (``CHOICE_FINX_JWT``). We validate the
  token's ``exp`` locally; a ``401`` (expired, or the request's source IP not
  matching the token's ``CliIPAddress``) is surfaced as a clear, actionable error —
  there is nothing to silently re-mint.

* **exchange** — POST the key to ``FINX_LOGIN_URL`` to mint + cache an SSO JWT,
  refreshing on expiry/``401``. Retained for a future confirmed M2M endpoint; not
  the default.

Credential safety (CHO-21 "Credential safety"): the key/JWT are never logged or
committed; they are read from ``.env`` (git-ignored).
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

from .. import config


def _decode_jwt_exp(token: str) -> float | None:
    """Return the ``exp`` (epoch seconds) from a JWT payload, or ``None``.

    Pure local base64 decode of the middle segment — no signature check (we are
    the bearer, not the verifier); used only to schedule refresh.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # pad to a multiple of 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — any malformed token → unknown expiry
        return None


class FinxAuthError(RuntimeError):
    """Login failed or no key configured. Message is client-safe (no secrets)."""


class FinxAuth:
    """Single cached JWT with refresh-on-expiry / refresh-on-401.

    ``httpx`` is imported lazily so importing this module (and unit-testing the
    token/header logic) needs no network stack. An injected ``client`` (any object
    with an async ``post``) is used verbatim, which is what the tests exercise.
    """

    def __init__(self, api_key: str | None = None, client: Any = None,
                 mode: str | None = None) -> None:
        self._api_key = api_key or config.finx_bearer()
        if not self._api_key:
            raise FinxAuthError("CHOICE_FINX_JWT / CHOICE_FINX_API_KEY not set (repo .env).")
        self._mode = mode or config.FINX_AUTH_MODE   # "direct" | "exchange"
        self._client = client            # injectable for tests; else lazy httpx
        self._owns_client = client is None
        self._jwt: str | None = None
        self._exp: float = 0.0           # epoch seconds; 0 = no token cached

    # -- client ------------------------------------------------------------- #
    async def _get_client(self) -> Any:
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=config.FINX_HTTP_TIMEOUT_S)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- token lifecycle ---------------------------------------------------- #
    def _valid(self) -> bool:
        return bool(self._jwt) and time.time() < (self._exp - config.FINX_JWT_SKEW_S)

    async def get_jwt(self, *, force: bool = False) -> str:
        """Return a usable bearer JWT.

        In ``direct`` mode the configured credential IS the bearer — used verbatim
        (a FINX key, or a JWT pasted from the browser). We only check it hasn't
        expired; there is nothing to "log in" to, so ``force`` on an expired token
        raises a clear, user-actionable error. In ``exchange`` mode we POST the key
        to the login URL and cache the minted token.
        """
        if self._mode == "direct":
            exp = _decode_jwt_exp(self._api_key)
            if exp is not None and time.time() >= (exp - config.FINX_JWT_SKEW_S):
                raise FinxAuthError(
                    "The FinX JWT has expired. Paste a fresh token "
                    "(CHOICE_FINX_JWT) — e.g. copied from the logged-in browser.")
            return self._api_key
        if force or not self._valid():
            await self._login()
        assert self._jwt is not None
        return self._jwt

    async def _login(self) -> None:
        client = await self._get_client()
        # Key sent as bearer header AND body field — a harmless superset until
        # task 1 confirms which the endpoint wants. Neither is ever logged.
        resp = await client.post(
            config.FINX_LOGIN_URL,
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json"},
            json={"apiKey": self._api_key, "grantType": "client_credentials",
                  "source": config.FINX_SOURCE},
        )
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            raise FinxAuthError(f"FinX login failed (HTTP {status}).")
        token = self._extract_token(resp.json())
        if not token:
            raise FinxAuthError("FinX login returned no token.")
        self._jwt = token
        exp = _decode_jwt_exp(token)
        # Prefer the JWT's own exp; else fall back to a conservative 8h window.
        self._exp = exp if exp is not None else (time.time() + 8 * 3600)

    @staticmethod
    def _extract_token(body: dict[str, Any]) -> str | None:
        """Pull the JWT from a login response under any common field name.

        Confirmed shape (task 1) → narrow this to the real field.
        """
        if not isinstance(body, dict):
            return None
        # FinX wraps payloads under "body" (confirmed live); tolerate data/result.
        for envelope in (body, body.get("body"), body.get("data"), body.get("result")):
            if not isinstance(envelope, dict):
                continue
            for key in ("jwt", "token", "access_token", "accessToken",
                        "id_token", "idToken", "authToken"):
                val = envelope.get(key)
                if isinstance(val, str) and val:
                    return val
        return None

    # -- MIS request helpers ------------------------------------------------ #
    def mis_headers(self, jwt: str) -> dict[str, str]:
        """Authorization headers every read-only MIS call must carry."""
        return {
            "Authorization": jwt,
            "authType": config.FINX_AUTH_TYPE,   # "jwt"
            "source": config.FINX_SOURCE,        # "FINX_WEB"
            "Content-Type": "application/json",
        }

    async def mis_post(self, url: str, payload: dict[str, Any]) -> Any:
        """POST to a read-only MIS endpoint with auth; refresh once on 401.

        Returns the raw response object (caller parses per task 1.2 shape). The
        one automatic retry covers a JWT that expired mid-conversation — the
        agent never sees the auth churn (CHO-21 D2).
        """
        client = await self._get_client()
        jwt = await self.get_jwt()
        resp = await client.post(url, headers=self.mis_headers(jwt), json=payload)
        if getattr(resp, "status_code", 200) == 401:
            if self._mode == "direct":
                # Nothing to re-mint from a directly-supplied token. A 401 here is
                # an expired/invalid token OR the source IP not matching the token's
                # CliIPAddress — surface it clearly rather than silently retrying.
                raise FinxAuthError(
                    "FinX rejected the token (401). It has likely expired, or this "
                    "host's IP doesn't match the token's whitelisted CliIPAddress. "
                    "Paste a fresh browser JWT and run from the whitelisted IP.")
            jwt = await self.get_jwt(force=True)  # exchange mode: re-login once
            resp = await client.post(url, headers=self.mis_headers(jwt), json=payload)
        return resp
