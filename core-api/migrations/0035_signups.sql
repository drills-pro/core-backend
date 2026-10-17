-- migrate:up
-- Self-serve signup saga state (docs/signup-saga.md). One row per signup attempt, keyed by the
-- client's Idempotency-Key, so a retry resumes the saga instead of creating a second Zitadel org,
-- and so the reaper can find orgs whose tenant row never landed. Not tenant-scoped: it exists
-- before the tenant does, so no RLS.
CREATE TABLE IF NOT EXISTS signups (
  key         text PRIMARY KEY,
  email       text NOT NULL,
  org_name    text NOT NULL,
  state       text NOT NULL DEFAULT 'started' CHECK (state IN ('started', 'org', 'granted', 'done')),
  org_id      text,
  user_id     text,
  tenant_id   uuid,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS signups_stale_idx ON signups(created_at) WHERE state <> 'done';

GRANT SELECT, INSERT, UPDATE, DELETE ON signups TO core_app;
-- The saga inserts the tenant row and its Default team in one tenant-scoped transaction
-- (0031 already granted this for the back-office; repeated so this file stands alone).
GRANT INSERT ON tenants TO core_app;

-- migrate:down
DROP TABLE IF EXISTS signups;
-- INSERT on tenants stays: 0031 granted it first and is still applied.
