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
export const DEPLOY_DONE = ['active', 'failed', 'deleted'];
/** A deployment in one of these has nothing left to stop. */
export const DEPLOY_ENDED = ['failed', 'deleted'];
export const DeployStatus = { DELETED: 'deleted' } as const;

export const PackageStatus = { CANDIDATE: 'candidate', STABLE: 'stable', RETIRED: 'retired' } as const satisfies Record<
  string,
  Schemas['PackageStatus']
>;
export const PACKAGE_NAMES: Schemas['JobPackageName'][] = ['ray_jobs', 'ray_training', 'ray_serving'];
/** How much of a sha256 the promote dialog asks you to type back. */
export const SHA_CONFIRM_CHARS = 8;

/** Badge color per status word, across imports, runs, deployments and packages. */
export function statusColor(status: string): string {
  if (['succeeded', 'active', 'stable', 'passed'].includes(status)) return 'green';
  if (['failed', 'expired', 'retired'].includes(status)) return 'red';
  if (['cancelled', 'discarded', 'deleted'].includes(status)) return 'gray';
  if ([ImportStatus.AWAITING_UPLOAD, ImportStatus.AWAITING_REVIEW, 'candidate'].includes(status)) return 'yellow';
  return 'blue';
}
