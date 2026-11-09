-- migrate:up
-- Learning-rate schedule, augmentation recipe and early stopping become per-preset data (audit C-25), copied into the
-- job spec like the other preset fields. Existing rows keep today's behaviour: constant LR, no augmentation, no early stop.
ALTER TABLE task_presets
  ADD COLUMN lr_schedule text NOT NULL DEFAULT 'constant',
  ADD COLUMN warmup_fraction double precision NOT NULL DEFAULT 0 CHECK (warmup_fraction >= 0 AND warmup_fraction < 1),
  ADD COLUMN augmentation text NOT NULL DEFAULT 'none',
  ADD COLUMN early_stopping_patience integer NOT NULL DEFAULT 0 CHECK (early_stopping_patience >= 0);  -- 0 = off

ALTER TABLE task_presets ADD CONSTRAINT task_presets_lr_schedule_check CHECK (lr_schedule IN ('constant', 'cosine'));
ALTER TABLE task_presets ADD CONSTRAINT task_presets_augmentation_check CHECK (augmentation IN ('none', 'flip_crop', 'flip'));

-- migrate:down
ALTER TABLE task_presets
  DROP COLUMN lr_schedule, DROP COLUMN warmup_fraction, DROP COLUMN augmentation, DROP COLUMN early_stopping_patience;
