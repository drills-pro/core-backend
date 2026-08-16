-- Org/team/project model (improvement/phase2/02-org-team-project-model.md).
-- Additive per 11-migration-phasing.md step 1: team_id and task_type start
-- nullable, backfilled below, NOT NULL tightening on team_id is a later
-- follow-up migration once zero NULL rows are confirmed in prod.

CREATE TABLE IF NOT EXISTS teams (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id),
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS teams_tenant_id_idx ON teams(tenant_id);

ALTER TABLE teams ENABLE ROW LEVEL SECURITY;
ALTER TABLE teams FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON teams;
CREATE POLICY tenant_isolation ON teams
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON teams TO core_app;

-- FR1: every existing tenant gets a "Default" team, so FR2 (project belongs
-- to exactly one team) can backfill projects.team_id below without an
-- orphaned project. New tenants get theirs from zitadel-bootstrap.sh.
INSERT INTO teams (tenant_id, name)
SELECT t.id, 'Default' FROM tenants t
WHERE NOT EXISTS (SELECT 1 FROM teams WHERE teams.tenant_id = t.id);

ALTER TABLE projects ADD COLUMN IF NOT EXISTS team_id uuid REFERENCES teams(id);
CREATE INDEX IF NOT EXISTS projects_team_id_idx ON projects(team_id);

UPDATE projects p SET team_id = (
  SELECT id FROM teams WHERE teams.tenant_id = p.tenant_id ORDER BY created_at LIMIT 1
) WHERE p.team_id IS NULL;

-- FR4/FR6: task type set at project creation, immutable after first
-- dataset attach (enforced in app code, not here). legacy_untyped is the
-- permanent escape hatch for pre-existing Phase-1 projects.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS task_type text;
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_constraint WHERE conname = 'projects_task_type_check') THEN
    ALTER TABLE projects ADD CONSTRAINT projects_task_type_check
      CHECK (task_type IN ('image_classification', 'object_detection',
                            'tabular_classification', 'text_classification',
                            'legacy_untyped'));
  END IF;
END
$$;
UPDATE projects SET task_type = 'legacy_untyped' WHERE task_type IS NULL;

-- FR3: usage_events gains team_id so 09's two-tier rollup has a join path.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS team_id uuid REFERENCES teams(id);
CREATE INDEX IF NOT EXISTS usage_events_team_id_idx ON usage_events(team_id, occurred_at DESC);
UPDATE usage_events ue SET team_id = (
  SELECT p.team_id FROM projects p WHERE p.id = ue.project_id
) WHERE ue.team_id IS NULL;
