"""FinX integration (CHO-21) — read-only account reports for the support agent.

Two capabilities, both read-only, layered on the CHO-20 agent:
  * ``app.finx.auth``    — ``finx-auth``: machine-to-machine exchange of the FinX
                           API key for a cached/refreshed SSO JWT. No OTP, no human.
  * ``app.finx.reports`` — ``account-report-tools``: the ``get_cml_report`` and
                           ``get_contract_note`` tools + their Claude schemas.

Hard boundary (CHO-21 D1): the FinX *trading* API (orders / funds / payments /
EDIS) is never called. The only endpoints touched are the M2M login and the two
read-only ``/mis/v2`` report endpoints.
"""
