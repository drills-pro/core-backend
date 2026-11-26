import { useState } from 'react';
import { Button, Dialog, HStack, Input, Portal, SimpleGrid, Stack } from '@chakra-ui/react';
import { useQuery } from '@tanstack/react-query';
import { adminKeys } from '@/api/admin';
import { client, unwrap, type Schemas } from '@/api/client';
import { usePagedList } from '@/api/paged';
import { MODEL_STATUSES, TASK_TYPES, VISIBILITIES } from '@/constants';
import { Empty, ErrorAlert, LoadMore, PageHeader, Rows, Select, Stat, StatusBadge, when } from '@/ui';

type Model = Schemas['AdminModel'];

/** Every registered model on the platform, one row per model; its versions open beside it. */
export function ModelsPage() {
  const [q, setQ] = useState('');
  const [taskType, setTaskType] = useState('');
  const [visibility, setVisibility] = useState('');
  const [status, setStatus] = useState('');
  const [versionsOf, setVersionsOf] = useState<Model | null>(null);

  const summary = useQuery({ queryKey: adminKeys.modelsSummary, queryFn: () => unwrap(client.GET('/v1/admin/models/summary')) });
  const filters = {
    q: q.trim() || undefined,
    task_type: (taskType || undefined) as Schemas['TaskType'] | undefined,
    visibility: visibility || undefined,
    status: status || undefined,
  };
  const list = usePagedList([...adminKeys.models, filters], (page_token) => unwrap(client.GET('/v1/admin/models', { params: { query: { ...filters, page_token } } })));

  const s = summary.data;
  return (
    <Stack gap="4">
      <PageHeader title="Models" description="Every model on the platform across tenants. A model is one project's named model; its versions are listed on open." />
      <ErrorAlert error={summary.error} />
      {s && (
        <SimpleGrid minChildWidth="9rem" gap="3">
          <Stat label="Models" value={s.model_count} hint={`${s.version_count} versions`} />
          <Stat label="Projects" value={s.project_count} />
          <Stat label="Deployed" value={s.deployed_count} hint={`${s.deploying_count} deploying`} />
          <Stat label="Stopped" value={s.stopped_count} />
          <Stat label="Not deployed" value={s.not_deployed_count} />
        </SimpleGrid>
      )}
      <HStack wrap="wrap" gap="2">
        <Input size="sm" maxW="xs" placeholder="Search model, project or organisation" value={q} onChange={(e) => setQ(e.target.value)} />
        <Select value={taskType} onChange={setTaskType} options={[{ value: '', label: 'Any task' }, ...TASK_TYPES.map((t) => ({ value: t, label: t }))]} />
        <Select value={visibility} onChange={setVisibility} options={[{ value: '', label: 'Any visibility' }, ...VISIBILITIES.map((v) => ({ value: v, label: v }))]} />
        <Select value={status} onChange={setStatus} options={[{ value: '', label: 'Any status' }, ...MODEL_STATUSES.map((v) => ({ value: v, label: v }))]} />
      </HStack>
      <ErrorAlert error={list.error} />
      {!list.isPending && list.items.length === 0 && <Empty title="No models match" />}
      {list.items.length > 0 && (
        <Rows
          headers={['Model', 'Version', 'Status', 'Organisation', 'Project', 'Visibility', 'Versions', 'Created', 'History']}
          items={list.items}
          rowKey={(m) => m.id}
          cells={(m) => [
            m.model_name,
            m.version,
            <StatusBadge key="s" status={m.status} />,
            m.tenant_name,
            m.project_name,
            m.visibility,
            m.version_count,
            when(m.created_at),
            <Button key="h" size="xs" variant="outline" onClick={() => setVersionsOf(m)}>
              Versions
            </Button>,
          ]}
        />
      )}
      <LoadMore hasMore={list.hasMore} loading={list.loadingMore} onClick={list.loadMore} />
      {versionsOf && <VersionsDialog model={versionsOf} onClose={() => setVersionsOf(null)} />}
    </Stack>
  );
}

function VersionsDialog({ model, onClose }: { model: Model; onClose: () => void }) {
  const versions = usePagedList(adminKeys.modelVersions(model.id), (page_token) =>
    unwrap(client.GET('/v1/admin/models/{version_id}/versions', { params: { path: { version_id: model.id }, query: { page_token } } })),
  );
  return (
    <Dialog.Root open size="lg" scrollBehavior="inside" onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>{model.model_name} versions</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                <ErrorAlert error={versions.error} />
                {!versions.isPending && versions.items.length === 0 && <Empty title="No versions" />}
                {versions.items.length > 0 && (
                  <Rows
                    headers={['Version', 'Stage', 'Status', 'Created']}
                    items={versions.items}
                    rowKey={(v) => v.id}
                    cells={(v) => [v.version, v.stage, <StatusBadge key="s" status={v.status} />, when(v.created_at)]}
                  />
                )}
                <LoadMore hasMore={versions.hasMore} loading={versions.loadingMore} onClick={versions.loadMore} />
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Close
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
