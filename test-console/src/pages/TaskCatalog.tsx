import { useState } from 'react';
import { Badge, Button, Card, Checkbox, Code, Dialog, Field, Fieldset, HStack, Input, Portal, Separator, Stack, Switch, Text, Textarea } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';
import { packageKeys, type TaskSlug } from '@/api/packages';
import { CardGrid, CardSkeletons, ConfirmDialog, ErrorAlert, Facts, PageHeader, StatusBadge, when } from '@/ui';

const fetchCatalog = () => unwrap(client.GET('/v1/admin/task-bundles/catalog'));
/** From the call, not the component schemas: the generated component type and the response type differ in tuple-ness. */
type Entry = Awaited<ReturnType<typeof fetchCatalog>>[number];
type Task = Entry['task'];
type Preset = Entry['presets'][number];

const SHA_SHOWN = 12;
const REVISION_SHOWN = 8;
/** The execution providers a deployment may ask onnxruntime for (core-api ExecutionProvider). */
const PROVIDERS = ['CPUExecutionProvider', 'CUDAExecutionProvider'] as const;
type Provider = (typeof PROVIDERS)[number];
/** core-api ServingParams bounds: score_threshold in [0, 1], max_results in [1, MAX_RESULTS_LIMIT]. */
const MAX_RESULTS_LIMIT = 1000;
/** The fields a preset's PUT does not take: the name is the path, the rest the server stamps. */
const NOT_SENT = ['name', 'task', 'enabled', 'updated_at', 'updated_by'];
const presetJson = (p: Preset) => JSON.stringify(Object.fromEntries(Object.entries(p).filter(([k]) => !NOT_SENT.includes(k))), null, 2);

/** What tenants can train, as cards you can edit: a task's switch and its serving settings, whether a passing bundle is
 * promoted on its own, and each preset (switch, edit, delete, new). Edits to a preset are overwritten when its bundle is
 * promoted next, because the manifest is the source of those rows. */
export function TaskCatalogPage() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: packageKeys.catalog, queryFn: fetchCatalog });
  const refresh = () => qc.invalidateQueries({ queryKey: packageKeys.catalog });
  const [serving, setServing] = useState<Task | null>(null);
  const [preset, setPreset] = useState<{ task: TaskSlug; name?: string; text: string } | null>(null);
  const [deleting, setDeleting] = useState<{ task: TaskSlug; name: string } | null>(null);

  const toggleTask = useMutation({
    mutationFn: ({ slug, enabled }: { slug: TaskSlug; enabled: boolean }) =>
      unwrap(client.PUT('/v1/admin/task-bundles/catalog/{slug}/enabled', { params: { path: { slug } }, body: { enabled } })),
    onSuccess: refresh,
  });
  const togglePreset = useMutation({
    mutationFn: ({ slug, name, enabled }: { slug: TaskSlug; name: string; enabled: boolean }) =>
      unwrap(client.PUT('/v1/admin/task-bundles/catalog/{slug}/presets/{name}/enabled', { params: { path: { slug, name } }, body: { enabled } })),
    onSuccess: refresh,
  });
  const setAutoPromote = useMutation({
    mutationFn: ({ slug, auto_promote }: { slug: TaskSlug; auto_promote: boolean }) =>
      unwrap(client.PUT('/v1/admin/task-bundles/{slug}/settings', { params: { path: { slug } }, body: { auto_promote } })),
    onSuccess: refresh,
  });
  const deletePreset = useMutation({
    mutationFn: ({ task, name }: { task: TaskSlug; name: string }) =>
      unwrap(client.DELETE('/v1/admin/training-presets/{task}/{name}', { params: { path: { task, name } } })),
    onSuccess: () => {
      setDeleting(null);
      return refresh();
    },
  });
  const busy = toggleTask.isPending || togglePreset.isPending || setAutoPromote.isPending;

  return (
    <Stack gap="4">
      <PageHeader
        title="Task catalog"
        description="What tenants can train. Switches and settings here take effect at once; a promotion of a task's bundle leaves them as they are, but rewrites its presets from the manifest."
      />
      <ErrorAlert error={list.error ?? toggleTask.error ?? togglePreset.error ?? setAutoPromote.error ?? deletePreset.error} />
      {list.isPending && <CardSkeletons />}
      <CardGrid min="26rem">
        {list.data?.map((e) => (
          <TaskCard
            key={e.task.slug}
            entry={e}
            busy={busy}
            onTask={(enabled) => toggleTask.mutate({ slug: e.task.slug, enabled })}
            onAutoPromote={(auto_promote) => setAutoPromote.mutate({ slug: e.task.slug, auto_promote })}
            onPresetEnabled={(name, enabled) => togglePreset.mutate({ slug: e.task.slug, name, enabled })}
            onServing={() => setServing(e.task)}
            onNewPreset={() => setPreset({ task: e.task.slug, text: e.presets[0] ? presetJson(e.presets[0]) : '{}' })}
            onEditPreset={(p) => setPreset({ task: e.task.slug, name: p.name, text: presetJson(p) })}
            onDeletePreset={(name) => setDeleting({ task: e.task.slug, name })}
          />
        ))}
      </CardGrid>
      {serving && <ServingDialog task={serving} onClose={() => setServing(null)} />}
      {preset && <PresetDialog task={preset.task} name={preset.name} initialText={preset.text} onClose={() => setPreset(null)} />}
      {deleting && (
        <ConfirmDialog
          title={`Delete ${deleting.name}`}
          danger
          confirmLabel="Delete"
          loading={deletePreset.isPending}
          error={deletePreset.error}
          onConfirm={() => deletePreset.mutate(deleting)}
          onClose={() => setDeleting(null)}
        >
          <Text>
            Removes <b>{deleting.name}</b> from {deleting.task} for every tenant. A promotion of the task's bundle brings it back if its manifest lists it.
          </Text>
        </ConfirmDialog>
      )}
    </Stack>
  );
}

function TaskCard({
  entry,
  busy,
  onTask,
  onAutoPromote,
  onPresetEnabled,
  onServing,
  onNewPreset,
  onEditPreset,
  onDeletePreset,
}: {
  entry: Entry;
  busy: boolean;
  onTask: (enabled: boolean) => void;
  onAutoPromote: (auto_promote: boolean) => void;
  onPresetEnabled: (name: string, enabled: boolean) => void;
  onServing: () => void;
  onNewPreset: () => void;
  onEditPreset: (p: Preset) => void;
  onDeletePreset: (name: string) => void;
}) {
  const { task, presets, bundle, auto_promote } = entry;
  return (
    <Card.Root variant="outline" opacity={task.enabled ? 1 : 0.85}>
      <Card.Header>
        <HStack justify="space-between">
          <Card.Title>{task.slug}</Card.Title>
          <Switch.Root size="sm" colorPalette="green" checked={task.enabled} disabled={busy} onCheckedChange={(e) => onTask(e.checked)}>
            <Switch.HiddenInput aria-label={`${task.slug} enabled`} />
            <Switch.Control />
            <Switch.Label>Enabled</Switch.Label>
          </Switch.Root>
        </HStack>
        <HStack>
          <Badge colorPalette={task.enabled ? 'green' : 'gray'}>{task.enabled ? 'enabled' : 'disabled'}</Badge>
          <Badge variant="surface">{task.servable ? 'servable' : 'not servable'}</Badge>
        </HStack>
      </Card.Header>
      <Card.Body gap="3">
        <Facts
          items={[
            ['Data kind', <Code key="k">{task.data_kind}</Code>],
            ['Trainer', <Code key="t">{task.trainer ?? '-'}</Code>],
            [
              'Bundle',
              bundle ? (
                <HStack key="b">
                  <Code>{bundle.sha256.slice(0, SHA_SHOWN)}</Code>
                  <StatusBadge status={bundle.smoke_status} />
                </HStack>
              ) : (
                <Text key="b" color="fg.muted">
                  none (a task from before bundles)
                </Text>
              ),
            ],
            ['Updated', `${when(task.updated_at)} by ${task.updated_by}`],
          ]}
        />
        <Switch.Root size="sm" colorPalette="orange" checked={auto_promote} disabled={busy} onCheckedChange={(e) => onAutoPromote(e.checked)}>
          <Switch.HiddenInput />
          <Switch.Control />
          <Switch.Label>Promote a bundle on its own when its smoke passes</Switch.Label>
        </Switch.Root>
        <HStack justify="space-between" wrap="wrap" gap="2">
          <Text fontSize="sm">
            Serving: score at least {task.serving.score_threshold}, up to {task.serving.max_results} results, {task.serving.providers.join(', ')}
          </Text>
          <Button size="xs" variant="outline" onClick={onServing}>
            Edit serving
          </Button>
        </HStack>
        <Separator />
        <Text fontSize="sm" fontWeight="semibold">
          Presets ({presets.length})
        </Text>
        <Stack gap="2">
          {presets.map((p) => (
            <HStack key={p.name} justify="space-between" p="2" rounded="md" bg="bg.subtle" opacity={p.enabled ? 1 : 0.7} gap="2" wrap="wrap">
              <Stack gap="0" minW="0">
                <Text fontWeight="medium" fontSize="sm">
                  {p.name}
                </Text>
                <Text fontSize="xs" color="fg.muted">
                  {p.base_model ? `${p.base_model.repo_id}@${p.base_model.revision.slice(0, REVISION_SHOWN)}` : 'no base model'} · {p.gpu ? `${p.gpu} GPU` : 'CPU'} · lr{' '}
                  {p.learning_rate}
                </Text>
              </Stack>
              <HStack>
                <Switch.Root size="sm" colorPalette="green" checked={p.enabled} disabled={busy} onCheckedChange={(e) => onPresetEnabled(p.name, e.checked)}>
                  <Switch.HiddenInput aria-label={`${p.name} enabled`} />
                  <Switch.Control />
                </Switch.Root>
                <Button size="xs" variant="outline" onClick={() => onEditPreset(p)}>
                  Edit
                </Button>
                <Button size="xs" variant="ghost" colorPalette="red" onClick={() => onDeletePreset(p.name)}>
                  Delete
                </Button>
              </HStack>
            </HStack>
          ))}
        </Stack>
        <Text fontSize="xs" color="fg.muted">
          A promotion of this task's bundle rewrites its presets from the manifest and removes the ones the manifest does not list.
        </Text>
      </Card.Body>
      <Card.Footer justifyContent="flex-end">
        <Button size="sm" variant="outline" onClick={onNewPreset}>
          New preset
        </Button>
      </Card.Footer>
    </Card.Root>
  );
}

/** The serving knobs of a task, saved as its task row: the other fields go back as they are. */
function ServingDialog({ task, onClose }: { task: Task; onClose: () => void }) {
  const qc = useQueryClient();
  const [threshold, setThreshold] = useState(String(task.serving.score_threshold));
  const [maxResults, setMaxResults] = useState(String(task.serving.max_results));
  const [providers, setProviders] = useState<Provider[]>(task.serving.providers as Provider[]);
  const thresholdValid = threshold.trim() !== '' && Number(threshold) >= 0 && Number(threshold) <= 1;
  const maxValid = /^\d+$/.test(maxResults) && Number(maxResults) >= 1 && Number(maxResults) <= MAX_RESULTS_LIMIT;
  const valid = thresholdValid && maxValid && providers.length > 0;
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.PUT('/v1/admin/task-types/{task}', {
          params: { path: { task: task.slug } },
          body: {
            data_kind: task.data_kind,
            trainer: task.trainer,
            servable: task.servable,
            encoder: task.encoder,
            postprocess: task.postprocess,
            enabled: task.enabled,
            serving: { score_threshold: Number(threshold), max_results: Number(maxResults), providers },
          },
        }),
      ),
    onSuccess: () => {
      onClose();
      return qc.invalidateQueries({ queryKey: packageKeys.catalog });
    },
  });

  return (
    <Dialog.Root open onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>Serving of {task.slug}</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                <Field.Root invalid={!thresholdValid}>
                  <Field.Label>Score threshold (0 to 1)</Field.Label>
                  <Input size="sm" type="number" step="0.01" min={0} max={1} value={threshold} onChange={(e) => setThreshold(e.target.value)} />
                  <Field.HelperText>A result scoring below this is not reported.</Field.HelperText>
                </Field.Root>
                <Field.Root invalid={!maxValid}>
                  <Field.Label>Max results (1 to {MAX_RESULTS_LIMIT})</Field.Label>
                  <Input size="sm" type="number" min={1} max={MAX_RESULTS_LIMIT} value={maxResults} onChange={(e) => setMaxResults(e.target.value)} />
                </Field.Root>
                {/* Fieldset, not Field: Field gives every checkbox the same id, so a click on one label toggles the first. */}
                <Fieldset.Root invalid={providers.length === 0}>
                  <Fieldset.Legend>Execution providers</Fieldset.Legend>
                  <Stack gap="2">
                    {PROVIDERS.map((p) => (
                      <Checkbox.Root
                        key={p}
                        checked={providers.includes(p)}
                        onCheckedChange={(e) => setProviders(PROVIDERS.filter((x) => (x === p ? e.checked === true : providers.includes(x))))}
                      >
                        <Checkbox.HiddenInput />
                        <Checkbox.Control />
                        <Checkbox.Label>{p}</Checkbox.Label>
                      </Checkbox.Root>
                    ))}
                  </Stack>
                </Fieldset.Root>
                <ErrorAlert error={save.error} />
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button disabled={!valid} loading={save.isPending} onClick={() => save.mutate()}>
                Save
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

/** Creates a preset (name and JSON body) or edits one (its JSON body). The API validates the body and says what is wrong. */
function PresetDialog({ task, name, initialText, onClose }: { task: TaskSlug; name?: string; initialText: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [newName, setNewName] = useState('');
  const [text, setText] = useState(initialText);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const target = name ?? newName.trim();
  const save = useMutation({
    mutationFn: (body: Schemas['PresetBody']) =>
      unwrap(client.PUT('/v1/admin/training-presets/{task}/{name}', { params: { path: { task, name: target } }, body })),
    onSuccess: () => {
      onClose();
      return qc.invalidateQueries({ queryKey: packageKeys.catalog });
    },
  });
  const submit = () => {
    let body: Schemas['PresetBody'];
    try {
      body = JSON.parse(text) as Schemas['PresetBody'];
    } catch (e) {
      setJsonError(`Not valid JSON: ${(e as Error).message}`);
      return;
    }
    setJsonError(null);
    save.mutate(body);
  };

  return (
    <Dialog.Root open size="xl" scrollBehavior="inside" onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>{name ? `Edit ${name}` : `New preset for ${task}`}</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                {!name && (
                  <Field.Root required>
                    <Field.Label>Name</Field.Label>
                    <Input size="sm" value={newName} onChange={(e) => setNewName(e.target.value)} />
                  </Field.Root>
                )}
                <Field.Root>
                  <Field.Label>Body (JSON)</Field.Label>
                  <Textarea size="sm" rows={18} fontFamily="mono" value={text} onChange={(e) => setText(e.target.value)} />
                </Field.Root>
                <ErrorAlert error={jsonError ? new Error(jsonError) : save.error} />
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button disabled={!target} loading={save.isPending} onClick={submit}>
                Save
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
