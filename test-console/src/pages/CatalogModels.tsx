import { useState } from 'react';
import { Button, Code, HStack, Input, Stack, Table, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, unwrap, type Schemas } from '@/api/client';
import { CATALOG_ERROR_MESSAGES, CATALOG_SOURCE, CATALOG_STATUSES, CatalogStatus, REVISION_PATTERN, TASK_TYPES } from '@/constants';
import { CodedError, ErrorAlert, Section, Select, StatusBadge, when } from '@/ui';

type Entry = Schemas['AdminCatalogModel'];
type Form = { repo_id: string; revision: string; display_name: string; task: string; license: string; params: string; size_bytes: string };

const EMPTY: Form = { repo_id: '', revision: '', display_name: '', task: TASK_TYPES[0], license: '', params: '', size_bytes: '' };
const KEY = ['model-catalog'];
const taskOptions = TASK_TYPES.map((t) => ({ value: t, label: t }));
const num = (v: string) => (v.trim() === '' ? null : Number(v));

const CatalogError = ({ error }: { error: unknown }) => <CodedError error={error} messages={CATALOG_ERROR_MESSAGES} />;

export function CatalogModelsPage() {
  const qc = useQueryClient();
  const [task, setTask] = useState('');
  const [status, setStatus] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const list = useQuery({
    queryKey: [...KEY, task, status],
    queryFn: () =>
      unwrap(
        client.GET('/v1/admin/model-catalog', {
          params: { query: { task: task || undefined, status: (status || undefined) as Schemas['CatalogStatus'] | undefined } },
        }),
      ),
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
      setDeleting(null);
      return done();
    },
  });

  return (
    <Stack gap="4">
      <Text color="fg.muted">
        Base models tenants may fine-tune, pinned by Hugging Face commit. Only enabled entries reach tenants; the API checks the Hub, the task registry and the licence
        allow-list when you add one.
      </Text>
      <AddForm onDone={done} />
      <Section
        title="Models"
        actions={
          <HStack>
            <Select value={task} onChange={setTask} options={taskOptions} placeholder="Any task" />
            <Select value={status} onChange={setStatus} options={CATALOG_STATUSES.map((s) => ({ value: s, label: s }))} placeholder="Any status" />
          </HStack>
        }
      >
        <ErrorAlert error={list.error} />
        <CatalogError error={toggle.error ?? remove.error} />
        <Table.Root size="sm">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>Model</Table.ColumnHeader>
              <Table.ColumnHeader>Task</Table.ColumnHeader>
              <Table.ColumnHeader>Licence</Table.ColumnHeader>
              <Table.ColumnHeader>Params</Table.ColumnHeader>
              <Table.ColumnHeader>Size (bytes)</Table.ColumnHeader>
              <Table.ColumnHeader>Status</Table.ColumnHeader>
              <Table.ColumnHeader>Updated</Table.ColumnHeader>
              <Table.ColumnHeader />
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {list.data?.map((m) =>
              editing === m.id ? (
                <EditRow key={m.id} m={m} onClose={() => setEditing(null)} onDone={done} />
              ) : (
                <Table.Row key={m.id}>
                  <Table.Cell>
                    {m.display_name}
                    <br />
                    <Code fontSize="xs">
                      {m.repo_id}@{m.revision.slice(0, 8)}
                    </Code>
                  </Table.Cell>
                  <Table.Cell>{m.task}</Table.Cell>
                  <Table.Cell>{m.license}</Table.Cell>
                  <Table.Cell>{m.params ?? '-'}</Table.Cell>
                  <Table.Cell>{m.size_bytes ?? '-'}</Table.Cell>
                  <Table.Cell>
                    <StatusBadge status={m.status} />
                  </Table.Cell>
                  <Table.Cell>
                    {when(m.updated_at)} by {m.updated_by}
                  </Table.Cell>
                  <Table.Cell>
                    {deleting === m.id ? (
                      <HStack justify="end">
                        <Text fontSize="xs">Delete {m.display_name}?</Text>
                        <Button size="xs" colorPalette="red" loading={remove.isPending} onClick={() => remove.mutate(m.id)}>
                          Delete
                        </Button>
                        <Button size="xs" variant="outline" onClick={() => setDeleting(null)}>
                          Cancel
                        </Button>
                      </HStack>
                    ) : (
                      <HStack justify="end">
                        <Button size="xs" variant="outline" onClick={() => setEditing(m.id)}>
                          Edit
                        </Button>
                        <Button size="xs" variant="outline" loading={toggle.isPending && toggle.variables?.id === m.id} onClick={() => toggle.mutate(m)}>
                          {m.status === CatalogStatus.ENABLED ? 'Disable' : 'Enable'}
                        </Button>
                        <Button size="xs" variant="outline" colorPalette="red" onClick={() => setDeleting(m.id)}>
                          Delete
                        </Button>
                      </HStack>
                    )}
                  </Table.Cell>
                </Table.Row>
              ),
            )}
          </Table.Body>
        </Table.Root>
        {list.data?.length === 0 && <Text color="fg.muted">No models match.</Text>}
      </Section>
    </Stack>
  );
}

function AddForm({ onDone }: { onDone: () => void }) {
  const [f, setF] = useState(EMPTY);
  const set = (k: keyof Form) => (e: { currentTarget: { value: string } }) => setF({ ...f, [k]: e.currentTarget.value });
  const add = useMutation({
    mutationFn: () =>
      unwrap(
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
      setF(EMPTY);
      return onDone();
    },
  });
  const valid = f.repo_id.trim() && new RegExp(`^${REVISION_PATTERN}$`).test(f.revision.trim()) && f.display_name.trim() && f.license.trim();
  return (
    <Section title="Add a model">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        <Stack gap="2">
          <HStack wrap="wrap">
            <Input w="xs" placeholder="Hub repo id, e.g. org/name" value={f.repo_id} onChange={set('repo_id')} />
            <Input w="md" placeholder="Commit sha (40 hex)" value={f.revision} onChange={set('revision')} />
            <Input w="xs" placeholder="Display name" value={f.display_name} onChange={set('display_name')} />
          </HStack>
          <HStack wrap="wrap">
            <Select value={f.task} onChange={(task) => setF({ ...f, task })} options={taskOptions} />
            <Input w="2xs" placeholder="Licence" value={f.license} onChange={set('license')} />
            <Input w="2xs" type="number" min="1" placeholder="Params (optional)" value={f.params} onChange={set('params')} />
            <Input w="2xs" type="number" min="0" placeholder="Size bytes (optional)" value={f.size_bytes} onChange={set('size_bytes')} />
            <Button type="submit" colorPalette="orange" disabled={!valid} loading={add.isPending}>
              Add
            </Button>
          </HStack>
          <CatalogError error={add.error} />
        </Stack>
      </form>
    </Section>
  );
}

/** Repo and revision are fixed: a different one is a different model, so it is a new entry. */
function EditRow({ m, onClose, onDone }: { m: Entry; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ display_name: m.display_name, task: m.task, license: m.license, params: String(m.params ?? ''), size_bytes: String(m.size_bytes ?? '') });
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        client.PATCH('/v1/admin/model-catalog/{model_id}', {
          params: { path: { model_id: m.id } },
          body: { display_name: f.display_name.trim(), task: f.task, license: f.license.trim(), params: num(f.params), size_bytes: num(f.size_bytes) },
        }),
      ),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  return (
    <Table.Row>
      <Table.Cell>
        <Input size="sm" value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.currentTarget.value })} />
        <Code fontSize="xs">
          {m.repo_id}@{m.revision.slice(0, 8)}
        </Code>
      </Table.Cell>
      <Table.Cell>
        <Select value={f.task} onChange={(task) => setF({ ...f, task })} options={taskOptions} />
      </Table.Cell>
      <Table.Cell>
        <Input size="sm" value={f.license} onChange={(e) => setF({ ...f, license: e.currentTarget.value })} />
      </Table.Cell>
      <Table.Cell>
        <Input size="sm" type="number" min="1" value={f.params} onChange={(e) => setF({ ...f, params: e.currentTarget.value })} />
      </Table.Cell>
      <Table.Cell>
        <Input size="sm" type="number" min="0" value={f.size_bytes} onChange={(e) => setF({ ...f, size_bytes: e.currentTarget.value })} />
      </Table.Cell>
      <Table.Cell colSpan={2}>
        <CatalogError error={save.error} />
      </Table.Cell>
      <Table.Cell>
        <HStack justify="end">
          <Button size="xs" colorPalette="green" disabled={!f.display_name.trim() || !f.license.trim()} loading={save.isPending} onClick={() => save.mutate()}>
            Save
          </Button>
          <Button size="xs" variant="outline" onClick={onClose}>
            Cancel
          </Button>
        </HStack>
      </Table.Cell>
    </Table.Row>
  );
}
