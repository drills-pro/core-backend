-- Shared fixed-window rate-limit counters (app/core/rate_limit.py), so the limit holds across
-- core-api replicas instead of being per-process. UNLOGGED: no WAL on a hot write path, and
-- losing the counters in a crash just resets everyone's window. Not tenant-scoped, no RLS.
CREATE UNLOGGED TABLE IF NOT EXISTS rate_limits (
  key     text   NOT NULL,
  window  bigint NOT NULL,  -- unix minute
  count   integer NOT NULL,
  PRIMARY KEY (key, window)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limits TO core_app;
