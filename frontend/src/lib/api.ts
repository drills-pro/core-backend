// Typed client for core-api. One function per endpoint the UI actually
// calls, mirroring core-api/app/*.py — note that several endpoints take
// their arguments as *query* params rather than a JSON body (FastAPI
// treats a bare `str`/`uuid.UUID` argument as a query param), so the shape
// of each call below follows the router signature, not a house style.
import { API_BASE, DEV_TENANT_ID } from './config'
import { authHeader, getAuth } from './auth'

// ---------------------------------------------------------------- types

/** app/projects.py VALID_TASK_TYPES */
export const TASK_TYPES = [
  'image_classification',
  'object_detection',
  'tabular_classification',
  'text_classification',
  'legacy_untyped',
] as const

export type TaskType = (typeof TASK_TYPES)[number]

export interface Team {
  id: string
  name: string
  created_at: string
}

export interface ProjectSettings {
  compute_target?: string
  base_model?: string
  deploy_strategy?: string
}

export interface Project {
  id: string
  name: string
  status: string
  team_id: string | null
  task_type: string
  created_at: string
  // Only returned by GET /v1/projects/{id}, not by the list endpoint.
  default_test_dataset_id?: string | null
  settings?: ProjectSettings | null
  visibility?: string | null
  publish_project_metadata?: boolean | null
}

export interface PublicProject {
  id: string
  /** null when the owner has not published project metadata. */
  name: string | null
  task_type: string | null
  created_at: string
}

export interface PublicDataset {
  id: string
  name: string | null
  published: boolean
  published_ref: string | null
}

export interface PublicModel {
  id: string
  mlflow_model_name: string | null
  mlflow_version: string | null
  publish_metadata: boolean
  publish_artifact: boolean
}

export interface Dataset {
  id: string
  name: string
  s3_prefix: string
  created_at: string
}

/** A LakeFS commit on the dataset's branch. */
export interface DatasetVersion {
  id: string
  message: string
  committer?: string
  /** Unix seconds. */
  creation_date: number
  parents?: string[]
}

export interface DatasetVersions {
  versions: DatasetVersion[]
  has_uncommitted_changes: boolean
}

export interface DatasetImport {
  id: string
  status: string
  hf_dataset: string
  kind: string
  error: string | null
  created_at: string
  completed_at: string | null
}

export interface PreprocessingJob {
  id: string
  status: string
  config: Record<string, unknown>
  output_ref?: string | null
  error?: string | null
  created_at: string
  completed_at?: string | null
}

export interface DatasetPreview {
  total_files: number
  total_images: number
  labels: Record<string, number>
  items: { key: string; label: string; view_url: string }[]
}

export interface TabularPreview {
  columns: string[]
  rows: Record<string, string>[]
  total_rows: number
}

export interface EpochPoint {
  epoch: number
  loss?: number
  accuracy?: number
}

/**
 * Whatever app/ray_client.py's log parsers emitted for this run: scalar
 * metrics (accuracy, loss, …) alongside the two structured extras.
 */
export interface TrainingMetrics {
  epoch_history?: EpochPoint[]
  confusion_matrix?: { labels: string[]; matrix: number[][] }
  [metric: string]: unknown
}

export interface TrainingRun {
  id: string
  status: string
  dataset_ref: string
  ray_job_id: string | null
  mlflow_run_id: string | null
  config: Record<string, unknown> | null
  metrics: TrainingMetrics | null
  created_at: string
  completed_at: string | null
  dataset_id?: string | null
  dataset_version?: string | null
  test_dataset_id?: string | null
  test_dataset_version?: string | null
  reproduction_group_id?: string | null
}

export interface TrainingRunLogs {
  status: string
  logs: string
  metrics: Record<string, number>
  extra_metrics: { epoch_history?: EpochPoint[]; [key: string]: unknown }
}

export interface ModelVersion {
  id: string
  mlflow_model_name: string
  mlflow_version: string
  stage: string
  customer_label: string
  created_at: string
}

export interface Deployment {
  id: string
  status: string
  route_prefix: string
  created_at: string
}

export interface CreatedDeployment {
  id: string
  status: string
  route_prefix: string
  rate_limit_per_minute: number
  plan_tier: string | null
  /** Shown once, on first create — a re-deploy of a 'main' route omits it. */
  predict_key?: string
}

export interface TestCall {
  /** bigserial in Postgres — arrives as a JSON number, not a UUID string. */
  id: number
  request: unknown
  response: unknown
  latency_ms: number
  created_at: string
}

export interface Lineage {
  training_run: TrainingRun
  dataset_commit_message: string | null
  mlflow_run_url: string | null
  model_version: { id: string; mlflow_model_name: string; mlflow_version: string; stage: string } | null
  deployment: { id: string; status: string; route_prefix: string } | null
}

export interface Member {
  user_id: string
  role: string
  created_at: string
}

export interface Invitation {
  id: string
  email: string
  role: string
  token: string
  expires_at: string
}

export interface AuditEntry {
  /** bigserial in Postgres — arrives as a JSON number, not a UUID string. */
  id: number
  actor: string
  action: string
  entity_type: string
  entity_id: string
  created_at: string
}

export interface UsageEvent {
  /** bigserial in Postgres — arrives as a JSON number, not a UUID string. */
  id: number
  resource_type: string
  quantity: number
  unit: string
  occurred_at: string
  source_ref: string
  pricing_version: string
}

export interface UsageRollup {
  project_id: string
  resource_type: string
  unit: string
  total_quantity: number
}

// -------------------------------------------------------------- plumbing

type QueryValue = string | number | boolean | undefined | null

function qs(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.append(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  // app/tenancy.py: X-Tenant-Id is only consulted when there is no Bearer
  // token, so this never overrides a real session.
  const merged: Record<string, string> = { ...authHeader() }
  if (!getAuth() && DEV_TENANT_ID) merged['X-Tenant-Id'] = DEV_TENANT_ID
  return { ...merged, ...extra }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: headers(init.headers as Record<string, string> | undefined),
  })
  const text = await resp.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }
  if (!resp.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail
    throw new Error(
      typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `${resp.status} ${resp.statusText}`,
    )
  }
  return data as T
}

function get<T>(path: string): Promise<T> {
  return request<T>(path)
}

function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (body === undefined) return request<T>(path, { method })
  return request<T>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

function uploadMultipart<T>(path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  // No Content-Type header on purpose: the browser has to set the
  // multipart boundary itself.
  return request<T>(path, { method: 'POST', body: form })
}

/** Idempotency-Key for POST /training-runs (app/training.py FR2). */
export function uuid(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}

const p = (projectId: string) => `/v1/projects/${encodeURIComponent(projectId)}`
const ds = (projectId: string, datasetId: string) => `${p(projectId)}/datasets/${encodeURIComponent(datasetId)}`

// ----------------------------------------------------------------- calls

export const api = {
  // teams — app/teams.py
  listTeams: (): Promise<Team[]> => get('/v1/teams'),
  getTeam: (teamId: string): Promise<Team> => get(`/v1/teams/${encodeURIComponent(teamId)}`),
  createTeam: (name: string): Promise<Team> => send('POST', `/v1/teams${qs({ name })}`),

  // projects — app/projects.py
  listProjects: (): Promise<Project[]> => get('/v1/projects'),
  getProject: (projectId: string): Promise<Project> => get(p(projectId)),
  createProject: (name: string, taskType: string, teamId?: string): Promise<Project> =>
    send('POST', `/v1/projects${qs({ name, task_type: taskType, team_id: teamId })}`),
  updateProject: (
    projectId: string,
    patch: { name?: string; status?: string; default_test_dataset_id?: string },
  ): Promise<Project> => send('PATCH', `${p(projectId)}${qs(patch)}`),
  updateProjectSettings: (
    projectId: string,
    settings: ProjectSettings,
  ): Promise<{ id: string; settings: ProjectSettings }> => send('PATCH', `${p(projectId)}/settings`, settings),
  updateProjectVisibility: (
    projectId: string,
    patch: { visibility?: string; publish_project_metadata?: boolean },
  ): Promise<{ id: string; visibility: string; publish_project_metadata: boolean }> =>
    send('PATCH', `${p(projectId)}/visibility${qs(patch)}`),
  deleteProject: (projectId: string): Promise<{ id: string; status: string }> => send('DELETE', p(projectId)),

  // public discovery (no auth) — app/public.py
  listPublicProjects: (): Promise<PublicProject[]> => get('/v1/public/projects'),
  listPublicProjectDatasets: (projectId: string): Promise<PublicDataset[]> =>
    get(`/v1/public/projects/${encodeURIComponent(projectId)}/datasets`),
  listPublicProjectModels: (projectId: string): Promise<PublicModel[]> =>
    get(`/v1/public/projects/${encodeURIComponent(projectId)}/models`),

  // datasets — app/datasets.py
  listDatasets: (projectId: string): Promise<Dataset[]> => get(`${p(projectId)}/datasets`),
  createDataset: (projectId: string, name: string): Promise<Dataset> =>
    send('POST', `${p(projectId)}/datasets${qs({ name })}`),
  listDatasetFiles: (projectId: string, datasetId: string, ref?: string): Promise<{ files: string[] }> =>
    get(`${ds(projectId, datasetId)}/files${qs({ ref })}`),
  previewDataset: (projectId: string, datasetId: string, ref?: string): Promise<DatasetPreview> =>
    get(`${ds(projectId, datasetId)}/preview${qs({ ref })}`),
  getTabular: (projectId: string, datasetId: string, ref?: string): Promise<TabularPreview> =>
    get(`${ds(projectId, datasetId)}/tabular${qs({ ref })}`),
  listDatasetVersions: (projectId: string, datasetId: string): Promise<DatasetVersions> =>
    get(`${ds(projectId, datasetId)}/versions`),
  commitDatasetVersion: (projectId: string, datasetId: string, message: string): Promise<DatasetVersion> =>
    send('POST', `${ds(projectId, datasetId)}/versions`, { message }),
  publishDataset: (
    projectId: string,
    datasetId: string,
    published: boolean,
  ): Promise<{ id: string; published: boolean; published_ref: string | null }> =>
    send('POST', `${ds(projectId, datasetId)}/publish`, { published }),

  // dataset ingestion — app/datasets.py
  getUploadUrl: (
    projectId: string,
    datasetId: string,
    filename: string,
    label: string,
  ): Promise<{ key: string; url: string }> =>
    send('POST', `${ds(projectId, datasetId)}/upload-url${qs({ filename, label })}`),
  /** PUT straight at the presigned URL — this one does not go through core-api. */
  uploadFile: async (url: string, file: File | Blob): Promise<void> => {
    const resp = await fetch(url, { method: 'PUT', body: file })
    if (!resp.ok) throw new Error(`upload failed: ${resp.status} ${resp.statusText}`)
  },
  uploadZip: (
    projectId: string,
    datasetId: string,
    file: File,
  ): Promise<{ uploaded: number; skipped: number; labels: Record<string, number> }> =>
    uploadMultipart(`${ds(projectId, datasetId)}/upload-zip`, file),
  uploadYolo: (
    projectId: string,
    datasetId: string,
    file: File,
  ): Promise<{ images: number; labels: number; skipped: number }> =>
    uploadMultipart(`${ds(projectId, datasetId)}/upload-yolo`, file),
  uploadCoco: (
    projectId: string,
    datasetId: string,
    file: File,
  ): Promise<{ images: number; labels: number; categories: number }> =>
    uploadMultipart(`${ds(projectId, datasetId)}/upload-coco`, file),
  /** Raw CSV body, not multipart — app/datasets.py reads request.body(). */
  uploadTabular: (projectId: string, datasetId: string, csv: string): Promise<{ rows: number; columns: string[] }> =>
    request(`${ds(projectId, datasetId)}/tabular`, {
      method: 'POST',
      headers: { 'Content-Type': 'text/csv' },
      body: csv,
    }),
  importHfDataset: (
    projectId: string,
    datasetId: string,
    body: {
      hf_dataset: string
      kind: 'images' | 'tabular'
      image_column?: string
      label_column?: string
      text_column?: string
      max_rows?: number | null
    },
  ): Promise<DatasetImport> => send('POST', `${ds(projectId, datasetId)}/import-hf`, body),
  listDatasetImports: (projectId: string, datasetId: string): Promise<DatasetImport[]> =>
    get(`${ds(projectId, datasetId)}/imports`),

  // preprocessing — app/preprocessing.py
  preprocessDataset: (
    projectId: string,
    datasetId: string,
    config: {
      resize?: { width: number; height: number }
      dedupe?: boolean
      class_balance?: string | null
    },
  ): Promise<PreprocessingJob> => send('POST', `${ds(projectId, datasetId)}/preprocess`, config),
  listPreprocessingJobs: (projectId: string, datasetId: string): Promise<PreprocessingJob[]> =>
    get(`${ds(projectId, datasetId)}/preprocess`),

  // training — app/training.py
  listTrainingRuns: (projectId: string): Promise<TrainingRun[]> => get(`${p(projectId)}/training-runs`),
  getTrainingRun: (projectId: string, runId: string): Promise<TrainingRun> =>
    get(`${p(projectId)}/training-runs/${encodeURIComponent(runId)}`),
  getTrainingRunLogs: (projectId: string, runId: string): Promise<TrainingRunLogs> =>
    get(`${p(projectId)}/training-runs/${encodeURIComponent(runId)}/logs`),
  getTrainingRunLineage: (projectId: string, runId: string): Promise<Lineage> =>
    get(`${p(projectId)}/training-runs/${encodeURIComponent(runId)}/lineage`),
  createTrainingRun: (
    projectId: string,
    datasetRef: string,
    config: Record<string, unknown>,
    idempotencyKey: string,
    datasetId?: string,
    datasetVersion?: string,
    testDatasetId?: string,
    testDatasetVersion?: string,
  ): Promise<TrainingRun> =>
    request(
      `${p(projectId)}/training-runs${qs({
        dataset_ref: datasetRef,
        dataset_id: datasetId,
        dataset_version: datasetVersion,
        test_dataset_id: testDatasetId,
        test_dataset_version: testDatasetVersion,
      })}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
        body: JSON.stringify(config),
      },
    ),
  cancelTrainingRun: (projectId: string, runId: string): Promise<TrainingRun> =>
    send('POST', `${p(projectId)}/training-runs/${encodeURIComponent(runId)}/cancel`),

  // model registry — app/models.py
  listModels: (projectId: string): Promise<ModelVersion[]> => get(`${p(projectId)}/models`),
  registerModel: (projectId: string, trainingRunId: string, name: string): Promise<ModelVersion> =>
    send('POST', `${p(projectId)}/models/${encodeURIComponent(trainingRunId)}/register${qs({ name })}`),
  promoteModel: (projectId: string, modelVersionId: string, stage: string): Promise<ModelVersion> =>
    send('POST', `${p(projectId)}/models/${encodeURIComponent(modelVersionId)}/promote${qs({ stage })}`),
  publishModel: (
    projectId: string,
    modelVersionId: string,
    patch: { publish_metadata?: boolean; publish_artifact?: boolean },
  ): Promise<{ id: string; publish_metadata: boolean; publish_artifact: boolean }> =>
    send('PATCH', `${p(projectId)}/models/${encodeURIComponent(modelVersionId)}/publish`, patch),

  // deployments — app/deployments.py
  listDeployments: (projectId: string): Promise<Deployment[]> => get(`${p(projectId)}/deployments`),
  createDeployment: (projectId: string, trainingRunId: string): Promise<CreatedDeployment> =>
    send('POST', `${p(projectId)}/deployments${qs({ training_run_id: trainingRunId })}`),
  getDeployment: (projectId: string, deploymentId: string): Promise<Deployment> =>
    get(`${p(projectId)}/deployments/${encodeURIComponent(deploymentId)}`),
  testDeployment: (
    projectId: string,
    deploymentId: string,
    body: unknown,
  ): Promise<{ response: unknown; latency_ms: number }> =>
    send('POST', `${p(projectId)}/deployments/${encodeURIComponent(deploymentId)}/test`, body),
  listTestCalls: (projectId: string, deploymentId: string): Promise<TestCall[]> =>
    get(`${p(projectId)}/deployments/${encodeURIComponent(deploymentId)}/test-calls`),

  // members / invitations / audit — app/project_members.py
  listMembers: (projectId: string): Promise<Member[]> => get(`${p(projectId)}/members`),
  setMemberRole: (projectId: string, userId: string, role: string): Promise<Member> =>
    send('PUT', `${p(projectId)}/members/${encodeURIComponent(userId)}${qs({ role })}`),
  removeMember: (projectId: string, userId: string): Promise<void> =>
    send('DELETE', `${p(projectId)}/members/${encodeURIComponent(userId)}`),
  createInvitation: (projectId: string, email: string, role: string): Promise<Invitation> =>
    send('POST', `${p(projectId)}/invitations${qs({ email, role })}`),
  acceptInvitation: (projectId: string, token: string): Promise<Member> =>
    send('POST', `${p(projectId)}/invitations/${encodeURIComponent(token)}/accept`),
  getAuditLog: (projectId: string): Promise<AuditEntry[]> => get(`${p(projectId)}/audit-log`),

  // usage — app/usage.py
  projectUsage: (projectId: string): Promise<UsageEvent[]> => get(`${p(projectId)}/usage`),
  teamUsage: (teamId: string): Promise<UsageRollup[]> => get(`/v1/teams/${encodeURIComponent(teamId)}/usage`),
  orgUsage: (): Promise<UsageRollup[]> => get('/v1/usage'),

  // linked identity providers — app/modules/user/users
  /** Raw Zitadel payload — shape is Zitadel's, not core-api's. */
  listLinkedIdps: (): Promise<unknown> => get('/v1/users/me/idps'),
  getIdpIntent: (provider: string, returnUrl: string): Promise<{ authUrl?: string; [key: string]: unknown }> =>
    get(`/v1/users/me/idp-intent${qs({ provider, return_url: returnUrl })}`),
  unlinkIdp: (linkId: string): Promise<void> => send('DELETE', `/v1/users/me/idps/${encodeURIComponent(linkId)}`),
}
