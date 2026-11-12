-- migrate:up
-- In-app notifications are the project's events (the outbox) seen from one member: all that is stored is how far
-- that member has read, so "unread" is every event after the cursor.
CREATE TABLE notification_cursors (
  tenant_id  uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL REFERENCES projects(id),
  user_sub   text NOT NULL,
  last_seq   bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (project_id, user_sub)
);
ALTER TABLE notification_cursors ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON notification_cursors USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE ON notification_cursors TO core_app;

-- migrate:down
DROP TABLE notification_cursors;
