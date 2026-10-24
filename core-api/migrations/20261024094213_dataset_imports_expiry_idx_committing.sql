-- migrate:up
-- The expiry sweeps also look at `committing` (a lapsed commit lease), which the old predicate left out.
DROP INDEX dataset_imports_expiry_idx;
CREATE INDEX dataset_imports_expiry_idx ON dataset_imports(expires_at)
  WHERE status IN ('awaiting_upload', 'awaiting_review', 'committing');

-- migrate:down
DROP INDEX dataset_imports_expiry_idx;
CREATE INDEX dataset_imports_expiry_idx ON dataset_imports(expires_at)
  WHERE status IN ('awaiting_upload', 'awaiting_review');
