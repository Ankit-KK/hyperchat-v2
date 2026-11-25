import pytest

from tests.integration import db


@pytest.fixture
def alerts_clean(env, strict_scratch_db):
    """An empty system_alerts table before and after the test (alert rows carry no synthetic marker and the delivery
    loop takes the oldest undelivered row, so a test starts from none). Scratch database only, never hyperchat_dev."""
    with db() as conn:
        conn.execute("DELETE FROM public.system_alerts")
    yield
    with db() as conn:
        conn.execute("DELETE FROM public.system_alerts")
