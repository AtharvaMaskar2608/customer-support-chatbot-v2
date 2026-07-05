"""Read-only FinX MIS report tools (CHO-21 ``account-report-tools``).

Two tools the CHO-20 agent can call:
  * ``get_cml_report(mobile)`` → ``POST /mis/v2/reports/v2/generate``
    ``{reportType:"cml", searchBy:"mobile-number", searchValue:<mobile>}``
  * ``get_contract_note(mobile, contract_date)`` →
    ``POST /mis/v2/contract-note/generate`` ``{mobileNo:<mobile>, contractDate:<dd-mm-yyyy>}``

Both are read-only ("generate a report") and never modify FinX state.

Structured-input boundary (CHO-21 D3, Model 2): ``contract_date`` is **absent
from the LLM tool schema** — the model can only *trigger* the tool; the harness
collects the date via a ``date_picker`` widget and injects it. ``mobile`` is a
plain tool argument for the POC (tester-supplied); production MUST bind it to an
authenticated identity — recorded as a hard follow-up (CHO-21 D7).

The report *response shape* (PDF bytes / download URL / JSON; sync vs poll) is a
task-1 unknown, so :func:`parse_report_response` is deliberately shape-agnostic
and feeds a shape-agnostic ``ReportEvent`` (CHO-21 D4).
"""
from __future__ import annotations

import base64
import re
from typing import Any

from .. import config
from ..events import ReportEvent
from .auth import FinxAuth

_MOBILE_RE = re.compile(r"^\d{10}$")
_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")  # dd-mm-yyyy


class ReportToolError(ValueError):
    """Bad input or an unusable report response. Message is user-safe."""


def _clean_mobile(mobile: str) -> str:
    digits = re.sub(r"\D", "", mobile or "")
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if not _MOBILE_RE.match(digits):
        raise ReportToolError("A valid 10-digit mobile number is required.")
    return digits


def parse_report_response(report_type: str, resp: Any) -> ReportEvent:
    """Turn an MIS response into a shape-agnostic ``ReportEvent`` (CHO-21 D4).

    Handles the three plausible shapes task 1 will resolve to:
      * binary PDF (``content-type: application/pdf``) → base64 content;
      * JSON carrying a download URL under a common field → ``url``;
      * JSON payload → ``payload`` (also scanned for base64 pdf content).
    """
    label = "CML report" if report_type == "cml" else "contract note"
    ctype = ""
    headers = getattr(resp, "headers", {}) or {}
    try:
        ctype = (headers.get("content-type") or headers.get("Content-Type") or "").lower()
    except Exception:  # noqa: BLE001
        ctype = ""

    # 1) Raw PDF bytes.
    if "application/pdf" in ctype:
        content = getattr(resp, "content", b"") or b""
        return ReportEvent(
            report_type=report_type,
            summary=f"Your {label} is ready.",
            filename=f"{report_type}.pdf",
            mime="application/pdf",
            content_b64=base64.b64encode(content).decode("ascii"),
        )

    # 2) JSON — either a URL, or a payload (possibly with embedded base64).
    body: Any = None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON, non-PDF; fall through
        body = None

    if isinstance(body, dict):
        # FinX wraps payloads in {statusCode, message, devMessage, body} (confirmed
        # against a live 401). Success data rides under `body`; tolerate data/result too.
        inner = body
        for env_key in ("body", "data", "result"):
            nested = body.get(env_key)
            if isinstance(nested, dict) and nested:
                inner = nested
                break
        url = _find_first(inner, ("url", "reportUrl", "downloadUrl", "fileUrl",
                                  "link", "pdfUrl"))
        if isinstance(url, str) and url:
            return ReportEvent(report_type=report_type,
                               summary=f"Your {label} is ready.", url=url)
        b64 = _find_first(inner, ("base64", "fileBase64", "pdfBase64", "content"))
        if isinstance(b64, str) and len(b64) > 100:  # heuristic: an embedded file
            return ReportEvent(
                report_type=report_type, summary=f"Your {label} is ready.",
                filename=f"{report_type}.pdf", mime="application/pdf", content_b64=b64)
        return ReportEvent(report_type=report_type,
                           summary=f"Your {label} is ready.", payload=inner)

    raise ReportToolError(
        f"The {label} could not be generated right now. Please try again later.")


def _find_first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return None


class FinxReports:
    """The two report tools over a shared :class:`FinxAuth`."""

    def __init__(self, auth: FinxAuth) -> None:
        self._auth = auth

    async def get_cml_report(self, mobile: str) -> ReportEvent:
        mobile = _clean_mobile(mobile)
        url = config.FINX_MIS_BASE + config.FINX_CML_PATH
        resp = await self._auth.mis_post(
            url, {"reportType": "cml", "searchBy": "mobile-number",
                  "searchValue": mobile})
        _raise_for_status(resp, "CML report")
        return parse_report_response("cml", resp)

    async def get_contract_note(self, mobile: str, contract_date: str) -> ReportEvent:
        mobile = _clean_mobile(mobile)
        if not _DATE_RE.match(contract_date or ""):
            # The date arrives from the date_picker widget, never LLM free text;
            # this guards a malformed harness value, not model output.
            raise ReportToolError("A contract date in dd-mm-yyyy format is required.")
        url = config.FINX_MIS_BASE + config.FINX_CONTRACT_NOTE_PATH
        resp = await self._auth.mis_post(
            url, {"mobileNo": mobile, "contractDate": contract_date})
        _raise_for_status(resp, "contract note")
        return parse_report_response("contract_note", resp)


def _raise_for_status(resp: Any, label: str) -> None:
    status = getattr(resp, "status_code", 200)
    if status >= 400:
        raise ReportToolError(
            f"The {label} could not be generated (HTTP {status}). "
            "Please check the details and try again.")


# --------------------------------------------------------------------------- #
# Claude tool schemas.
# NOTE (CHO-21 D3): get_contract_note exposes ONLY `mobile` — `contract_date` is
# intentionally NOT in the schema. The harness collects it via a date_picker.
# --------------------------------------------------------------------------- #
CML_TOOL = {
    "name": "get_cml_report",
    "description": (
        "Generate and fetch the customer's CML (Client Master List) report from "
        "Choice FinX. Use when the user asks for their CML report / CMR / demat "
        "master details. Returns the report as a downloadable document."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mobile": {
                "type": "string",
                "description": "The customer's registered 10-digit mobile number.",
            },
        },
        "required": ["mobile"],
    },
}

CONTRACT_NOTE_TOOL = {
    "name": "get_contract_note",
    "description": (
        "Generate and fetch the customer's contract note for a trading day from "
        "Choice FinX. Use when the user asks for a contract note / trade "
        "confirmation for a date. The date is collected from the user via a "
        "calendar widget — do NOT ask for or supply the date yourself; just call "
        "this with the mobile number and the calendar will appear."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mobile": {
                "type": "string",
                "description": "The customer's registered 10-digit mobile number.",
            },
        },
        "required": ["mobile"],
    },
}
