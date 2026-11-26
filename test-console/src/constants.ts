import type { Schemas } from '@/api/client';

/** Mirrors core-api PlanTier.INTERNAL: the only tenants this console acts in. */
export const INTERNAL_PLAN_TIER = 'internal';

/** Mirrors core-api TaskType (the schema has it as a type only). The API refuses one its task registry disables. */
export const TASK_TYPES: Schemas['TaskType'][] = [
  'image-classification', 'object-detection', 'image-segmentation', 'tabular-classification', 'tabular-regression',
  'text-classification', 'token-classification', 'question-answering', 'summarization', 'translation', 'text-generation',
  'audio-classification', 'automatic-speech-recognition', 'text-to-speech',
];

/** Mirrors core-api DEFAULT_SEED (integrations/job_spec.py); the generated type makes the defaulted field required. */
export const DEFAULT_SEED = 42;

/** How often a page with something in flight (an import, a run, a deploy) polls. */
export const POLL_MS = 3_000;

export const ImportStatus = {
  AWAITING_UPLOAD: 'awaiting_upload',
  AWAITING_REVIEW: 'awaiting_review',
} as const satisfies Record<string, Schemas['ImportStatus']>;
export const IMPORT_DONE: Schemas['ImportStatus'][] = ['succeeded', 'failed', 'cancelled', 'expired', 'discarded'];

/** Mirrors core-api TrainingStatus and DeploymentStatus (plain strings in the API schema). */
export const RunStatus = { SUCCEEDED: 'succeeded' } as const;
export const RUN_DONE = ['succeeded', 'failed', 'cancelled'];
export const DEPLOY_DONE = ['active', 'failed', 'stopped', 'deleted'];
/** A deployment in one of these has nothing left to stop. */
export const DEPLOY_ENDED = ['failed', 'stopped', 'deleted'];
export const DeployStatus = { DELETED: 'deleted', STOPPED: 'stopped' } as const;
/** The only scope a project API key needs for POST /v1/predict. */
export const PREDICT_SCOPE: Schemas['Scope'] = 'predict';

/** Mirrors core-api PlanTier: the tiers the platform sets limits for, and creates tenants in. */
export const PLAN_TIERS: Schemas['PlanTier'][] = ['free', 'pro', 'enterprise', 'internal'];
/** Mirrors core-api ADMIN_SETTABLE_STATUSES: what the tenant status route sets. pending_delete is a tenant's own delete. */
export const TenantStatus = { ACTIVE: 'active', SUSPENDED: 'suspended' } as const satisfies Record<string, Schemas['TenantStatus']>;
export const IDENTITY_STATUSES: Schemas['IdentityStatus'][] = ['active', 'suspended', 'pending', 'unknown'];
export const MODEL_STATUSES: Schemas['ModelStatus'][] = ['deployed', 'stopped', 'not_deployed'];
export const VISIBILITIES: Schemas['Visibility'][] = ['private', 'internal'];
/** Mirrors core-api AdminAuditEntity: what the admin audit trail records writes to. */
export const AUDIT_ENTITIES: Schemas['AdminAuditEntity'][] = ['tenant', 'task_type', 'task_preset', 'model_catalog', 'plan_limits'];
/** The activity feed's time windows, in days. */
export const ACTIVITY_DAYS = [1, 7, 30];

export const PackageStatus = { CANDIDATE: 'candidate', STABLE: 'stable', RETIRED: 'retired' } as const satisfies Record<
  string,
  Schemas['PackageStatus']
>;
export const PACKAGE_NAMES: Schemas['JobPackageName'][] = ['ray_jobs', 'ray_training', 'ray_serving'];
/** Mirrors core-api SmokeStatus. */
export const SmokeStatus = { PENDING: 'pending', PASSED: 'passed', FAILED: 'failed' } as const satisfies Record<string, Schemas['SmokeStatus']>;
/** The `code` of the 409s from the runtime-package and task-bundle status and smoke routes. */
export const PackageErrorCode = {
  SMOKE_NOT_PASSED: 'smoke_not_passed',
  SMOKE_NOT_RETRYABLE: 'smoke_not_retryable',
  BUNDLE_IS_STABLE: 'bundle_is_stable',
} as const;
export const PACKAGE_ERROR_MESSAGES: Record<string, string> = {
  [PackageErrorCode.SMOKE_NOT_PASSED]: 'Its smoke job has not passed, so it cannot become stable yet.',
  [PackageErrorCode.SMOKE_NOT_RETRYABLE]: 'Only a candidate whose smoke job failed can be smoked again.',
  [PackageErrorCode.BUNDLE_IS_STABLE]: 'This is the stable bundle and cannot be retired. Promote another one to replace it.',
};
/** How much of a sha256 the promote dialog asks you to type back. */
export const SHA_CONFIRM_CHARS = 8;

/** Mirrors core-api CatalogStatus (model_catalog_status_check). */
export const CatalogStatus = { ENABLED: 'enabled', DISABLED: 'disabled' } as const satisfies Record<string, Schemas['CatalogStatus']>;
export const CATALOG_STATUSES: Schemas['CatalogStatus'][] = [CatalogStatus.ENABLED, CatalogStatus.DISABLED];
/** Mirrors core-api CatalogSource; the generated create type makes the defaulted `source` required. */
export const CATALOG_SOURCE: Schemas['CatalogSource'] = 'hf';
/** A full Hugging Face commit sha: the API pins models by it and refuses a branch name. */
export const REVISION_PATTERN = '[0-9a-f]{40}';

/** The `code` of core-api's problem+json errors from /v1/admin/model-catalog, with what each means to an operator. */
export const CatalogErrorCode = {
  TASK_NOT_SUPPORTED: 'task_type_not_supported',
  LICENSE_NOT_ALLOWED: 'license_not_allowed',
  NOT_ADMITTED: 'model_not_admitted',
  HUB_UNAVAILABLE: 'hub_unavailable',
  ALREADY_IN_CATALOG: 'already_in_catalog',
  IN_USE: 'catalog_model_in_use',
} as const;
export const CATALOG_ERROR_MESSAGES: Record<string, string> = {
  [CatalogErrorCode.TASK_NOT_SUPPORTED]: 'The platform does not run this task type yet (its task registry entry is disabled).',
  [CatalogErrorCode.LICENSE_NOT_ALLOWED]: 'This licence is not on the allowed list, so the model cannot be admitted.',
  [CatalogErrorCode.NOT_ADMITTED]: 'The Hugging Face Hub does not admit this model at this revision (wrong repo, revision or task, or a gated model).',
  [CatalogErrorCode.HUB_UNAVAILABLE]: 'The Hugging Face Hub could not be reached to check the model. Try again in a moment.',
  [CatalogErrorCode.ALREADY_IN_CATALOG]: 'This repo at this revision is already in the catalog.',
  [CatalogErrorCode.IN_USE]: 'Projects or training runs still use this model. Disable it instead of deleting it.',
};

/** Badge color per status word, across imports, runs, deployments and packages. */
export function statusColor(status: string): string {
  if (['succeeded', 'active', 'stable', 'passed'].includes(status)) return 'green';
  if (['failed', 'expired', 'retired'].includes(status)) return 'red';
  if (status === DeployStatus.STOPPED) return 'orange';
  if (['cancelled', 'discarded', 'deleted'].includes(status)) return 'gray';
  if ([ImportStatus.AWAITING_UPLOAD, ImportStatus.AWAITING_REVIEW, 'candidate'].includes(status)) return 'yellow';
  return 'blue';
}
