"""20261118013637 (project sharing) on a POPULATED database: each old flag combination lands on its level, the per-model flags
become "exclude" flags, and the down half restores the old flags. Built like test_models_backfill_db: a scratch schema from the
migrations before it, seeded, then migrated."""
import asyncio
import os
import uuid

from app.modules.user.projects.constants import Sharing
from tests.db.conftest import DSN_ENV
from tests.db.schema import SEEDS, TENANT_SEED
from tests.db.test_models_backfill_db import MIGRATIONS, apply_up, parts

import asyncpg

TARGET = next(p for p in sorted(MIGRATIONS.glob("*_project_sharing.sql")))
BEFORE = [f for f in sorted(MIGRATIONS.glob("*.sql")) if f.name < TARGET.name]
NEEDED = {"datasets", "dataset_imports", "dataset_versions", "training_runs", "models", "model_versions"}
TENANT = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
# name: (gallery_published, publish_project_metadata) before the migration
CASES = {
    "not_in_gallery": (False, True),
    "gallery_without_metadata": (True, False),
    "gallery": (True, True),
    "gallery_with_artifact": (True, True),
    "gallery_with_dataset": (True, True),
}


async def seed(conn):
    await conn.execute(TENANT_SEED, TENANT)
    for name, (gallery, metadata) in CASES.items():
        await conn.execute(
            "INSERT INTO projects (tenant_id, name, visibility, gallery_published, publish_project_metadata) "
            "VALUES ($1, $2, 'internal', $3, $4)", TENANT, name, gallery, metadata)
    for table, sql in SEEDS:
        if table in NEEDED:
            await conn.execute(sql, TENANT)
    # a model artifact published in one project, even one that never reached the gallery; a pinned dataset version in another
    await conn.execute("UPDATE model_versions SET publish_artifact = true WHERE project_id IN "
                       "(SELECT id FROM projects WHERE name IN ('gallery_with_artifact', 'not_in_gallery'))")
    await conn.execute("UPDATE datasets d SET published_version_id = v.id FROM dataset_versions v, projects p "
                       "WHERE v.dataset_id = d.id AND p.id = d.project_id AND p.name = 'gallery_with_dataset'")


def test_every_flag_combination_maps_to_a_level_and_down_restores_the_flags():
    async def run():
        schema = "mig_" + uuid.uuid4().hex[:12]
        conn = await asyncpg.connect(os.environ[DSN_ENV])
        try:
            await conn.execute(f"CREATE SCHEMA {schema}")
            await conn.execute(f"SET search_path TO {schema}; SET check_function_bodies = off")
            await apply_up(conn, BEFORE)
            await seed(conn)
            await conn.execute(parts(TARGET)[0])
            levels = {r["name"]: r["sharing"] for r in await conn.fetch("SELECT name, sharing FROM projects")}
            assert levels == {
                "not_in_gallery": Sharing.PRIVATE,
                "gallery_without_metadata": Sharing.PRIVATE,  # its name was never shown, so it does not start now
                "gallery": Sharing.GALLERY,
                "gallery_with_artifact": Sharing.GALLERY_DOWNLOADS,
                "gallery_with_dataset": Sharing.GALLERY_DOWNLOADS,
            }
            derived = {r["name"]: (r["gallery_published"], r["publish_project_metadata"]) for r in await conn.fetch("SELECT * FROM projects")}
            assert derived["gallery"] == (True, True) and derived["not_in_gallery"] == (False, False)
            flags = {r["name"]: (r["publish_metadata"], r["publish_artifact"]) for r in await conn.fetch(
                "SELECT p.name, m.publish_metadata, m.publish_artifact FROM model_versions m JOIN projects p ON p.id = m.project_id")}
            assert flags["not_in_gallery"] == (True, True)  # nothing was ever shown: nothing starts excluded
            # a shared project keeps what it chose: metadata of a version it never published stays excluded; artifact flags
            # below the downloads level are reset (nothing was shown), and the downloads level keeps its explicit choice
            assert flags["gallery"] == (False, True) and flags["gallery_with_artifact"] == (False, True)
            await conn.execute(parts(TARGET)[1])
            old = {r["name"]: (r["gallery_published"], r["publish_project_metadata"]) for r in await conn.fetch("SELECT * FROM projects")}
            assert old["gallery"] == (True, True) and old["not_in_gallery"] == (False, False)
            assert await conn.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_schema = $1 "
                                       "AND table_name = 'projects' AND column_name = 'sharing'", schema) == 0
        finally:
            await conn.execute("RESET search_path")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await conn.close()

    asyncio.run(run())
