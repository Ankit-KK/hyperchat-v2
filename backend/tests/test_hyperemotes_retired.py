"""HyperEmotes are permanently discontinued (owner decision 2026-11-13): no V2 code path may create, configure, price,
auto-approve or expose them. The database keeps donations.is_hyperemote as history (ETL reconciliation) and, until a
later forward-only migration, the two unused streamers.hyperemotes_* columns. No database, no network."""

import inspect
import pathlib
import re

import pydantic
import pytest

from app.dashboard import models as dashboard_models
from app.dashboard import validation
from app.payments import rules
from app.payments.models import CheckoutRequest
from app.public import models as public_models
from app.realtime import payloads
from app.soundboard import models as soundboard_models

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
WORD = re.compile(r"hyper.?emote", re.IGNORECASE)
# The only mentions left in application code: comments recording the decision (validation.py), never a field name.
ALLOWED = {("dashboard/validation.py", "# ── settings (legacy update-streamer-settings allowlist, minus the read-only "
                                       "amount and HyperEmotes) ──"),
           ("dashboard/validation.py", "# HyperEmotes are discontinued (owner decision 2026-11-13): their streamer "
                                       "columns stay in the schema until a later")}


def test_application_code_has_no_hyperemote_reference_except_the_decision_comments():
    found = set()
    for path in APP.rglob("*"):
        if path.suffix in {".py", ".js", ".html", ".css"} and "__pycache__" not in path.parts:
            for line in path.read_text(encoding="utf-8").splitlines():
                if WORD.search(line):
                    found.add((path.relative_to(APP).as_posix(), line.strip()))
    assert found == ALLOWED


def _models(*modules):
    for module in modules:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, pydantic.BaseModel) and obj.__module__ == module.__name__:
                yield obj


def test_no_request_or_response_model_has_a_hyperemote_field():
    names = [(m.__name__, f) for m in _models(dashboard_models, public_models, soundboard_models)
             for f in m.model_fields if WORD.search(f)]
    assert names == []
    assert not any(WORD.search(f) for f in CheckoutRequest.model_fields)
    assert not any(WORD.search(f) for f in payloads._DONATION_FIELDS)


def test_settings_cannot_enable_configure_or_price_hyperemotes():
    assert not [s for s in (*validation.WRITABLE_SETTINGS, *validation.READ_ONLY_SETTINGS) if WORD.search(s)]
    for body in ({"hyperemotes_enabled": True}, {"hyperemotes_enabled": False}, {"hyperemotes_min_amount": 1}):
        with pytest.raises(pydantic.ValidationError):
            dashboard_models.SettingsPatch.model_validate(body)


def test_checkout_refuses_a_hyperemote_flag():
    with pytest.raises(pydantic.ValidationError):
        CheckoutRequest.model_validate({"name": "Ravi", "amount": "100", "is_hyperemote": True})


def test_auto_approve_has_no_hyperemote_input():
    assert not any(WORD.search(p) for p in inspect.signature(rules.auto_approves).parameters)
