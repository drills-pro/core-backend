-- migrate:up
-- When the run's job actually started running (queue time is not billable compute, planning 14 "Billable meters").
-- Stamped by the submitted -> running move. A run still running at migration time gets created_at (the best known
-- lower bound); finished runs were already billed or not, and stay NULL.
ALTER TABLE training_runs ADD COLUMN started_at timestamptz;
UPDATE training_runs SET started_at = created_at WHERE status = 'running';

-- migrate:down
ALTER TABLE training_runs DROP COLUMN started_at;
