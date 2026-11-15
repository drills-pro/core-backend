"""admin_audit: the enums mirror the migration's CHECKs, and record() writes one row with its own arguments."""
import asyncio
import re
from pathlib import Path

from app.core import admin_audit
from app.core.enums import AdminAuditAction, AdminAuditEntity
from tests.support import FakeConn

MIGRATION = next((Path(__file__).parents[2] / "migrations").glob("*_admin_audit.sql")).read_text().split("-- migrate:down")[0]


def _listed(pattern: str) -> set[str]:
    return set(re.search(pattern, MIGRATION, re.S).group(1).replace("'", "").replace(" ", "").replace("\n", "").split(","))


def test_the_enums_mirror_the_checks():
    assert _listed(r"admin_audit_action_check CHECK \(action IN \((.*?)\)\)") == {a.value for a in AdminAuditAction}
    assert _listed(r"admin_audit_entity_type_check CHECK \(entity_type IN \((.*?)\)\)") == {e.value for e in AdminAuditEntity}


def test_core_app_can_only_add_and_read_rows():
    assert "GRANT SELECT, INSERT ON admin_audit TO core_app;" in MIGRATION


def test_record_appends_one_row():
    conn = FakeConn()
    asyncio.run(admin_audit.record(conn, "ops", AdminAuditAction.PLAN_LIMITS_PUT, AdminAuditEntity.PLAN_LIMITS, "pro", {"k": 1}))
    sql, args = conn.calls[0]
    assert sql.startswith("INSERT INTO admin_audit")
    assert args == ("ops", AdminAuditAction.PLAN_LIMITS_PUT, AdminAuditEntity.PLAN_LIMITS, "pro", {"k": 1})
