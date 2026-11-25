import pytest

from tests.integration import db


@pytest.fixture
def alerts_clean(env, strict_scratch_db):
    """Remove the alert rows a test created (system_alerts rows carry no synthetic marker, so: every row that was not
    there before the test). Scratch database only: never on hyperchat_dev."""
    with db() as conn:
        before = [r[0] for r in conn.execute("SELECT id FROM public.system_alerts")]
    yield
    with db() as conn:
        conn.execute("DELETE FROM public.system_alerts WHERE NOT (id = ANY(%s::uuid[]))", (before,))
