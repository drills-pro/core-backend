-- migrate:up
-- One stable package per name: the one new runs use. Promotion already retires the previous stable bundle; this makes the
-- registry say it too, so no path can leave two. Existing duplicates keep only the newest.
-- migration-lint: allow-destructive (status flip of superseded stable rows, no row is deleted)
UPDATE job_packages p SET status = 'retired', updated_by = 'migration', updated_at = now()
WHERE p.status = 'stable' AND EXISTS (
  SELECT 1 FROM job_packages n WHERE n.name = p.name AND n.status = 'stable' AND (n.updated_at, n.sha256) > (p.updated_at, p.sha256)
);
CREATE UNIQUE INDEX job_packages_one_stable ON job_packages (name) WHERE status = 'stable';

-- migrate:down
DROP INDEX job_packages_one_stable;
