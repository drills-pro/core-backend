import { useState } from 'react';
import { Badge, Button, Card, Code, Dialog, Field, HStack, Input, Portal, SimpleGrid, Stack, Switch, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';
import { CATALOG_ERROR_MESSAGES, CATALOG_SOURCE, CATALOG_STATUSES, CatalogStatus, REVISION_PATTERN, TASK_TYPES } from '@/constants';
import { CardGrid, CardSkeletons, CodedError, ConfirmDialog, Empty, Facts, PageHeader, Select, when } from '@/ui';

type Entry = Schemas['AdminCatalogModel'];
type Form = { repo_id: string; revision: string; display_name: string; task: string; license: string; params: string; size_bytes: string };

const EMPTY: Form = { repo_id: '', revision: '', display_name: '', task: TASK_TYPES[0], license: '', params: '', size_bytes: '' };
const KEY = ['model-catalog'];
const REVISION_SHOWN = 8;
const taskOptions = TASK_TYPES.map((t) => ({ value: t, label: t }));
const num = (v: string) => (v.trim() === '' ? null : Number(v));

const CatalogError = ({ error }: { error: unknown }) => <CodedError error={error} messages={CATALOG_ERROR_MESSAGES} />;

export function CatalogModelsPage() {
  const qc = useQueryClient();
  const [task, setTask] = useState('');
  const [status, setStatus] = useState('');
  const [dialog, setDialog] = useState<{ kind: 'add' } | { kind: 'edit' | 'delete'; model: Entry } | null>(null);
  const list = useQuery({
    queryKey: [...KEY, task, status],
    queryFn: () =>
      unwrap(client.GET('/v1/admin/model-catalog', { params: { query: { task: task || undefined, status: (status || undefined) as Schemas['CatalogStatus'] | undefined } } })),
  });
  const done = () => qc.invalidateQueries({ queryKey: KEY });
  const toggle = useMutation({
    mutationFn: (m: Entry) =>
      unwrap(
        client.POST(m.status === CatalogStatus.ENABLED ? '/v1/admin/model-catalog/{model_id}/disable' : '/v1/admin/model-catalog/{model_id}/enable', {
          params: { path: { model_id: m.id } },
        }),
      ),
    onSuccess: done,
  });
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(client.DELETE('/v1/admin/model-catalog/{model_id}', { params: { path: { model_id: id } } })),
    onSuccess: () => {
      setDialog(null);
      return done();
    },
  });
  const close = () => {
    remove.reset();
    setDialog(null);
  };

  return (
    <Stack gap="4">
      <PageHeader
        title="Model catalog"
        description="Base models tenants may fine-tune, pinned by Hugging Face commit. Only enabled entries reach tenants; the API checks the Hub, the task registry and the licence allow-list when you add one."
        actions={
          <Button colorPalette="orange" onClick={() => setDialog({ kind: 'add' })}>
            Add a model
          </Button>
        }
      />
      <HStack wrap="wrap">
        <Select value={task} onChange={setTask} options={taskOptions} placeholder="Any task" />
        <Select value={status} onChange={setStatus} options={CATALOG_STATUSES.map((s) => ({ value: s, label: s }))} placeholder="Any status" />
      </HStack>
      <CatalogError error={list.error ?? toggle.error} />
      {list.isPending && <CardSkeletons />}
      {list.data?.length === 0 && <Empty title="No models match" />}
      <CardGrid min="22rem">
        {list.data?.map((m) => (
          <Card.Root key={m.id} variant="outline" opacity={m.status === CatalogStatus.ENABLED ? 1 : 0.8}>
            <Card.Header>
              <HStack justify="space-between" align="start">
                <Stack gap="0.5">
                  <Card.Title>{m.display_name}</Card.Title>
                  <Code fontSize="xs">
                    {m.repo_id}@{m.revision.slice(0, REVISION_SHOWN)}
                  </Code>
                </Stack>
                <Switch.Root
                  size="sm"
                  colorPalette="green"
                  checked={m.status === CatalogStatus.ENABLED}
                  disabled={toggle.isPending && toggle.variables?.id === m.id}
                  onCheckedChange={() => toggle.mutate(m)}
                >
                  <Switch.HiddenInput aria-label={`${m.display_name} enabled`} />
                  <Switch.Control />
                </Switch.Root>
              </HStack>
              <HStack>
                <Badge colorPalette={m.status === CatalogStatus.ENABLED ? 'green' : 'gray'}>{m.status}</Badge>
                <Badge variant="surface">{m.task}</Badge>
              </HStack>
            </Card.Header>
            <Card.Body>
              <Facts
                items={[
                  ['Licence', m.license],
                  ['Params', m.params ?? '-'],
                  ['Size (bytes)', m.size_bytes ?? '-'],
                  ['Updated', `${when(m.updated_at)} by ${m.updated_by}`],
                ]}
              />
            </Card.Body>
            <Card.Footer justifyContent="flex-end">
              <Button size="xs" variant="outline" onClick={() => setDialog({ kind: 'edit', model: m })}>
                Edit
              </Button>
              <Button size="xs" variant="outline" colorPalette="red" onClick={() => setDialog({ kind: 'delete', model: m })}>
                Delete
              </Button>
            </Card.Footer>
          </Card.Root>
        ))}
      </CardGrid>
      {dialog?.kind === 'add' && <ModelDialog onClose={close} onDone={done} />}
      {dialog?.kind === 'edit' && <ModelDialog model={dialog.model} onClose={close} onDone={done} />}
      {dialog?.kind === 'delete' && (
        <ConfirmDialog title={`Delete ${dialog.model.display_name}?`} confirmLabel="Delete" danger loading={remove.isPending} error={remove.error} onClose={close} onConfirm={() => remove.mutate(dialog.model.id)}>
          <Text>A model that projects or training runs still use cannot be deleted: disable it instead.</Text>
        </ConfirmDialog>
      )}
    </Stack>
  );
}

/** Add (no `model`) or edit one. Repo and revision are fixed once added: a different one is a different model, so a new entry. */
function ModelDialog({ model, onClose, onDone }: { model?: Entry; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState<Form>(
    model ? { ...EMPTY, repo_id: model.repo_id, revision: model.revision, display_name: model.display_name, task: model.task, license: model.license, params: String(model.params ?? ''), size_bytes: String(model.size_bytes ?? '') } : EMPTY,
  );
  const set = (k: keyof Form) => (e: { currentTarget: { value: string } }) => setF({ ...f, [k]: e.currentTarget.value });
  const save = useMutation({
    mutationFn: () =>
      model
        ? unwrap(
            client.PATCH('/v1/admin/model-catalog/{model_id}', {
              params: { path: { model_id: model.id } },
              body: { display_name: f.display_name.trim(), task: f.task, license: f.license.trim(), params: num(f.params), size_bytes: num(f.size_bytes) },
            }),
          )
        : unwrap(
            client.POST('/v1/admin/model-catalog', {
              body: {
                repo_id: f.repo_id.trim(),
                revision: f.revision.trim(),
                display_name: f.display_name.trim(),
                source: CATALOG_SOURCE,
                status: CatalogStatus.ENABLED,
                task: f.task,
                license: f.license.trim(),
                params: num(f.params),
                size_bytes: num(f.size_bytes),
              },
            }),
          ),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  const valid = f.display_name.trim() && f.license.trim() && (model || (f.repo_id.trim() && new RegExp(`^${REVISION_PATTERN}$`).test(f.revision.trim())));
  return (
    <Dialog.Root open size="lg" onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                save.mutate();
              }}
            >
              <Dialog.Header>
                <Dialog.Title>{model ? `Edit ${model.display_name}` : 'Add a model'}</Dialog.Title>
              </Dialog.Header>
              <Dialog.Body>
                <Stack gap="3">
                  <SimpleGrid columns={{ base: 1, md: 2 }} gap="3">
                    <Field.Root required disabled={!!model}>
                      <Field.Label>Hub repo id</Field.Label>
                      <Input placeholder="org/name" value={f.repo_id} onChange={set('repo_id')} />
                    </Field.Root>
                    <Field.Root required disabled={!!model}>
                      <Field.Label>Commit sha</Field.Label>
                      <Input placeholder="40 hex characters" value={f.revision} onChange={set('revision')} />
                    </Field.Root>
                    <Field.Root required>
                      <Field.Label>Display name</Field.Label>
                      <Input value={f.display_name} onChange={set('display_name')} />
                    </Field.Root>
                    <Field.Root required>
                      <Field.Label>Licence</Field.Label>
                      <Input value={f.license} onChange={set('license')} />
                    </Field.Root>
                    <Field.Root>
                      <Field.Label>Task</Field.Label>
                      <Select value={f.task} onChange={(task) => setF({ ...f, task })} options={taskOptions} />
                    </Field.Root>
                    <Field.Root>
                      <Field.Label>Params (optional)</Field.Label>
                      <Input type="number" min="1" value={f.params} onChange={set('params')} />
                    </Field.Root>
                    <Field.Root>
                      <Field.Label>Size in bytes (optional)</Field.Label>
                      <Input type="number" min="0" value={f.size_bytes} onChange={set('size_bytes')} />
                    </Field.Root>
                  </SimpleGrid>
                  <CatalogError error={save.error} />
                </Stack>
              </Dialog.Body>
              <Dialog.Footer>
                <Button variant="outline" onClick={onClose}>
                  Cancel
                </Button>
                <Button type="submit" colorPalette="orange" disabled={!valid} loading={save.isPending}>
                  {model ? 'Save' : 'Add'}
                </Button>
              </Dialog.Footer>
            </form>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
