-- migrate:up
-- Shared fixed-window rate-limit counters (app/core/rate_limit.py), so the limit holds across
-- core-api replicas instead of being per-process. UNLOGGED: no WAL on a hot write path, and
-- losing the counters in a crash just resets everyone's window. Not tenant-scoped, no RLS.
CREATE UNLOGGED TABLE IF NOT EXISTS rate_limits (
  key     text   NOT NULL,
  -- unix minute. Not "window": that is a reserved word, which kept this file from ever applying.
  minute  bigint NOT NULL,
  count   integer NOT NULL,
  PRIMARY KEY (key, minute)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limits TO core_app;

-- migrate:down
DROP TABLE IF EXISTS rate_limits;
