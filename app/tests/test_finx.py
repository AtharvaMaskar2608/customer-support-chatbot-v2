"""FinX auth + report tests (CHO-21) — JWT caching/refresh + response parsing.

No network: a fake async HTTP client records posts and returns scripted responses.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from app.events import ReportEvent
from app.finx.auth import FinxAuth, FinxAuthError, _decode_jwt_exp
from app.finx.reports import (
    FinxReports,
    ReportToolError,
    _clean_mobile,
    parse_report_response,
)


# --- fakes ---------------------------------------------------------------- #
class FakeResp:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code = status
        self._body = body
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeClient:
    """Records posts; returns queued responses (or a default) in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.posts = []

    async def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "headers": headers, "json": json})
        return self._responses.pop(0)


def make_jwt(exp_epoch: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp_epoch), "iss": "sso.choiceindia.com"}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


# --- auth: direct mode (default) ------------------------------------------ #
def test_decode_jwt_exp_reads_claim():
    exp = time.time() + 3600
    assert abs(_decode_jwt_exp(make_jwt(exp)) - int(exp)) < 1
    assert _decode_jwt_exp("not-a-jwt") is None


async def test_direct_mode_uses_token_verbatim():
    jwt = make_jwt(time.time() + 8 * 3600)
    client = FakeClient([FakeResp(status=200, body={"body": {"url": "u"}})])
    auth = FinxAuth(api_key=jwt, client=client)          # default mode="direct"
    resp = await auth.mis_post("https://x/mis", {"a": 1})
    assert resp.status_code == 200
    # No login call — exactly one POST (the MIS call), with the token sent verbatim.
    assert len(client.posts) == 1
    assert client.posts[0]["headers"]["Authorization"] == jwt
    assert client.posts[0]["headers"]["authType"] == "jwt"


async def test_direct_mode_expired_token_raises():
    auth = FinxAuth(api_key=make_jwt(time.time() - 10), client=FakeClient([]))
    with pytest.raises(FinxAuthError):
        await auth.get_jwt()


async def test_direct_mode_401_raises_actionable_error():
    jwt = make_jwt(time.time() + 3600)
    client = FakeClient([FakeResp(status=401, body={"message": "Invalid Request."})])
    auth = FinxAuth(api_key=jwt, client=client)
    with pytest.raises(FinxAuthError):
        await auth.mis_post("https://x/mis", {"a": 1})
    assert len(client.posts) == 1  # no silent retry in direct mode


# --- auth: exchange mode (opt-in) ----------------------------------------- #
async def test_exchange_mode_caches_and_reuses_jwt():
    jwt = make_jwt(time.time() + 8 * 3600)
    client = FakeClient([FakeResp(body={"token": jwt})])
    auth = FinxAuth(api_key="secret", client=client, mode="exchange")
    t1 = await auth.get_jwt()
    t2 = await auth.get_jwt()          # cached — no second login
    assert t1 == t2 == jwt
    assert len(client.posts) == 1
    assert client.posts[0]["json"]["apiKey"] == "secret"


async def test_exchange_mode_refresh_on_401_then_retry():
    good = make_jwt(time.time() + 8 * 3600)
    client = FakeClient([
        FakeResp(body={"token": good}),          # initial login
        FakeResp(status=401),                     # MIS call → 401
        FakeResp(body={"token": good}),           # forced re-login
        FakeResp(status=200, body={"ok": True}),  # MIS retry succeeds
    ])
    auth = FinxAuth(api_key="secret", client=client, mode="exchange")
    resp = await auth.mis_post("https://x/mis", {"a": 1})
    assert resp.status_code == 200
    assert len(client.posts) == 4


# --- report parsing ------------------------------------------------------- #
def test_parse_pdf_bytes():
    resp = FakeResp(content=b"%PDF-1.7 ...", headers={"content-type": "application/pdf"})
    ev = parse_report_response("cml", resp)
    assert isinstance(ev, ReportEvent) and ev.mime == "application/pdf"
    assert base64.b64decode(ev.content_b64) == b"%PDF-1.7 ..."


def test_parse_json_url():
    resp = FakeResp(body={"data": {"downloadUrl": "https://x/cml.pdf"}},
                    headers={"content-type": "application/json"})
    ev = parse_report_response("cml", resp)
    assert ev.url == "https://x/cml.pdf"


def test_parse_json_body_envelope():
    # FinX wraps success payloads under `body` (confirmed against a live 401 shape).
    resp = FakeResp(body={"statusCode": 200, "message": "OK", "devMessage": None,
                          "body": {"reportUrl": "https://x/cml.pdf"}},
                    headers={"content-type": "application/json"})
    ev = parse_report_response("cml", resp)
    assert ev.url == "https://x/cml.pdf"


def test_parse_json_payload_fallback():
    resp = FakeResp(body={"holdings": [1, 2, 3]},
                    headers={"content-type": "application/json"})
    ev = parse_report_response("contract_note", resp)
    assert ev.payload == {"holdings": [1, 2, 3]}


# --- mobile normalisation + tool wiring ----------------------------------- #
def test_clean_mobile():
    assert _clean_mobile("8779552825") == "8779552825"
    assert _clean_mobile("+91 87795 52825") == "8779552825"
    with pytest.raises(ReportToolError):
        _clean_mobile("12345")


async def test_cml_tool_posts_correct_payload():
    jwt = make_jwt(time.time() + 3600)                     # direct mode: token = jwt
    client = FakeClient([
        FakeResp(body={"body": {"url": "https://x/cml.pdf"}},   # MIS report (no login)
                 headers={"content-type": "application/json"}),
    ])
    reports = FinxReports(FinxAuth(api_key=jwt, client=client))
    ev = await reports.get_cml_report("8779552825")
    mis_post = client.posts[-1]
    assert mis_post["json"] == {"reportType": "cml", "searchBy": "mobile-number",
                                "searchValue": "8779552825"}
    assert mis_post["headers"]["Authorization"] == jwt
    assert mis_post["headers"]["authType"] == "jwt"
    assert mis_post["headers"]["source"] == "FINX_WEB"
    assert ev.url == "https://x/cml.pdf"


async def test_contract_note_requires_valid_date():
    reports = FinxReports(FinxAuth(api_key=make_jwt(time.time() + 3600),
                                   client=FakeClient([])))
    with pytest.raises(ReportToolError):
        await reports.get_contract_note("8779552825", "2026-07-05")  # wrong format
