"""admin_audit: the enums mirror the migration's CHECKs, and record() writes one row with its own arguments."""
import asyncio
import uuid
from pathlib import Path

from app.core import admin_audit
from app.core.enums import AdminAuditAction, AdminAuditEntity
from tests.support import FakeConn
from tests.support.check_mirror import assert_mirrors

MIGRATION = next((Path(__file__).parents[2] / "migrations").glob("*_admin_audit.sql")).read_text().split("-- migrate:down")[0]


def test_the_enums_mirror_the_checks():
    assert_mirrors("admin_audit", "action", AdminAuditAction)
    assert_mirrors("admin_audit", "entity_type", AdminAuditEntity)


def test_core_app_can_only_add_and_read_rows():
    assert "GRANT SELECT, INSERT ON admin_audit TO core_app;" in MIGRATION


def test_record_appends_one_row():
    conn = FakeConn()
    asyncio.run(admin_audit.record(conn, "ops", AdminAuditAction.PLAN_LIMITS_PUT, AdminAuditEntity.PLAN_LIMITS, "pro", {"k": 1}))
    sql, args = conn.calls[0]
    assert sql.startswith("INSERT INTO admin_audit")
    assert args == ("ops", AdminAuditAction.PLAN_LIMITS_PUT, AdminAuditEntity.PLAN_LIMITS, "pro", {"k": 1})


def test_record_stringifies_the_entity_id_and_defaults_the_detail_to_an_empty_object():
    conn = FakeConn()
    model_id = uuid.uuid4()
    asyncio.run(admin_audit.record(conn, "ops", AdminAuditAction.MODEL_CATALOG_DELETE, AdminAuditEntity.MODEL_CATALOG, model_id))
    assert conn.calls[0][1] == ("ops", AdminAuditAction.MODEL_CATALOG_DELETE, AdminAuditEntity.MODEL_CATALOG, str(model_id), {})
