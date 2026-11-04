import { useState } from 'react';
import { Button, Code, Field, HStack, Input, SimpleGrid, Table, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, client, newKey, unwrap, type Schemas } from '@/api/client';
import { IMPORT_DONE, ImportStatus, POLL_MS } from '@/constants';
import { ErrorAlert, Section, Select, short, StatusBadge, when } from '@/ui';

type Source = Schemas['ImportSource'];
type Params = Schemas['ImportParams'];
const SPLITS: Schemas['Split'][] = ['train', 'validation', 'test'];

export const versionsQuery = (projectId: string, datasetId: string) => ({
  queryKey: ['versions', datasetId],
  queryFn: () =>
    unwrap(
      client.GET('/v1/projects/{project_id}/datasets/{dataset_id}/versions', {
        params: { path: { project_id: projectId, dataset_id: datasetId } },
      }),
    ),
  enabled: !!datasetId,
});

export function Datasets({ projectId, datasetId, onDataset }: { projectId: string; datasetId: string; onDataset: (id: string) => void }) {
  const qc = useQueryClient();
  const path = { project_id: projectId, dataset_id: datasetId };
  const datasets = useQuery({
    queryKey: ['datasets', projectId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/datasets', { params: { path: { project_id: projectId } } })),
  });
  const versions = useQuery(versionsQuery(projectId, datasetId));
  const imports = useQuery({
    queryKey: ['imports', datasetId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/datasets/{dataset_id}/imports', { params: { path } })),
    enabled: !!datasetId,
    refetchInterval: (q) => (q.state.data?.items.some((i) => !IMPORT_DONE.includes(i.status)) ? POLL_MS : false),
  });
  const refresh = () => Promise.all([qc.invalidateQueries({ queryKey: ['imports', datasetId] }), qc.invalidateQueries({ queryKey: ['versions', datasetId] })]);

  const [name, setName] = useState('');
  const createDataset = useMutation({
    mutationFn: () => unwrap(client.POST('/v1/projects/{project_id}/datasets', { params: { path: { project_id: projectId } }, body: { name } })),
    onSuccess: (d) => {
      setName('');
      onDataset(d.id);
      return qc.invalidateQueries({ queryKey: ['datasets', projectId] });
    },
  });

  const [source, setSource] = useState<Source>('huggingface');
  const [params, setParams] = useState<Params>({});
  const [file, setFile] = useState<File | null>(null);
  const setParam = (k: keyof Params) => (v: string) => setParams((p) => ({ ...p, [k]: v || null }));

  // Create, upload when the source is a file, then start: the three calls the customer app makes, in one click.
  const runImport = useMutation({
    mutationFn: async () => {
      const latest = versions.data?.items[0]?.id ?? null; // a dataset that has versions must name the one it builds on
      const imp = await unwrap(
        client.POST('/v1/projects/{project_id}/datasets/{dataset_id}/imports', {
          params: { path, header: { 'Idempotency-Key': newKey() } },
          body: { source, params, base_version_id: latest },
        }),
      );
      if (source === 'upload') {
        if (!imp.upload_url || !file) throw new Error('no upload_url or no file');
        const put = await fetch(imp.upload_url, { method: 'PUT', body: file });
        if (!put.ok) throw new ApiError(put.status, undefined, 'upload to the presigned URL failed');
      }
      return unwrap(
        client.POST('/v1/projects/{project_id}/datasets/{dataset_id}/imports/{import_id}/start', {
          params: { path: { ...path, import_id: imp.id } },
        }),
      );
    },
    onSettled: refresh,
  });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'commit' | 'discard' | 'cancel' }): Promise<unknown> => {
      const p = { params: { path: { ...path, import_id: id } } };
      if (action === 'commit') return unwrap(client.POST('/v1/projects/{project_id}/datasets/{dataset_id}/imports/{import_id}/commit', p));
      if (action === 'discard') return unwrap(client.POST('/v1/projects/{project_id}/datasets/{dataset_id}/imports/{import_id}/discard', p));
      return unwrap(client.POST('/v1/projects/{project_id}/datasets/{dataset_id}/imports/{import_id}/cancel', p));
    },
    onSettled: refresh,
  });

  return (
    <Section title="1. Dataset">
      <HStack>
        <Select
          value={datasetId}
          onChange={onDataset}
          placeholder="Pick a dataset"
          options={(datasets.data?.items ?? []).map((d) => ({ value: d.id, label: `${d.name} (${short(d.id)})` }))}
        />
        <Input size="sm" placeholder="New dataset name" value={name} onChange={(e) => setName(e.target.value)} />
        <Button size="sm" disabled={!name.trim()} loading={createDataset.isPending} onClick={() => createDataset.mutate()}>
          Create
        </Button>
      </HStack>
      <ErrorAlert error={datasets.error ?? createDataset.error} />

      {datasetId && (
        <>
          <Text fontWeight="semibold">New import</Text>
          <SimpleGrid columns={{ base: 1, md: 3 }} gap="2">
            <Field.Root>
              <Field.Label>Source</Field.Label>
              <Select value={source} onChange={(v) => setSource(v as Source)} options={[{ value: 'huggingface', label: 'Hugging Face' }, { value: 'upload', label: 'Upload a file' }]} />
            </Field.Root>
            {source === 'huggingface' ? (
              <>
                <Field.Root required>
                  <Field.Label>Repo</Field.Label>
                  <Input size="sm" placeholder="owner/name" value={params.repo ?? ''} onChange={(e) => setParam('repo')(e.target.value)} />
                </Field.Root>
                <Field.Root>
                  <Field.Label>Revision</Field.Label>
                  <Input size="sm" placeholder="main" value={params.revision ?? ''} onChange={(e) => setParam('revision')(e.target.value)} />
                </Field.Root>
                <Field.Root>
                  <Field.Label>Split</Field.Label>
                  <Select value={params.split ?? ''} onChange={setParam('split')} placeholder="(all)" options={SPLITS.map((s) => ({ value: s, label: s }))} />
                </Field.Root>
                <Field.Root>
                  <Field.Label>Column</Field.Label>
                  <Input size="sm" value={params.column ?? ''} onChange={(e) => setParam('column')(e.target.value)} />
                </Field.Root>
              </>
            ) : (
              <Field.Root required>
                <Field.Label>File</Field.Label>
                <Input size="sm" type="file" p="1" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
              </Field.Root>
            )}
            <Field.Root>
              <Field.Label>Target</Field.Label>
              <Input size="sm" placeholder="label column (tabular)" value={params.target ?? ''} onChange={(e) => setParam('target')(e.target.value)} />
            </Field.Root>
          </SimpleGrid>
          <HStack>
            <Button
              size="sm"
              colorPalette="orange"
              loading={runImport.isPending}
              disabled={source === 'upload' ? !file : !params.repo}
              onClick={() => runImport.mutate()}
            >
              Create and start import
            </Button>
          </HStack>
          <ErrorAlert error={runImport.error ?? act.error ?? imports.error} />

          <Table.Root size="sm">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>Import</Table.ColumnHeader>
                <Table.ColumnHeader>Source</Table.ColumnHeader>
                <Table.ColumnHeader>Status</Table.ColumnHeader>
                <Table.ColumnHeader>Created</Table.ColumnHeader>
                <Table.ColumnHeader />
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {imports.data?.items.map((i) => (
                <Table.Row key={i.id}>
                  <Table.Cell>
                    <Code>{short(i.id)}</Code>
                  </Table.Cell>
                  <Table.Cell>{i.source === 'huggingface' ? i.params.repo : 'upload'}</Table.Cell>
                  <Table.Cell>
                    <StatusBadge status={i.status} />
                  </Table.Cell>
                  <Table.Cell>{when(i.created_at)}</Table.Cell>
                  <Table.Cell>
                    <HStack justify="end">
                      {i.status === ImportStatus.AWAITING_REVIEW && (
                        <>
                          <Button size="xs" colorPalette="green" onClick={() => act.mutate({ id: i.id, action: 'commit' })}>
                            Commit
                          </Button>
                          <Button size="xs" variant="outline" onClick={() => act.mutate({ id: i.id, action: 'discard' })}>
                            Discard
                          </Button>
                        </>
                      )}
                      {!IMPORT_DONE.includes(i.status) && i.status !== ImportStatus.AWAITING_REVIEW && (
                        <Button size="xs" variant="outline" onClick={() => act.mutate({ id: i.id, action: 'cancel' })}>
                          Cancel
                        </Button>
                      )}
                    </HStack>
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table.Root>

          <Text fontWeight="semibold">Versions</Text>
          <Table.Root size="sm">
            <Table.Body>
              {versions.data?.items.map((v) => (
                <Table.Row key={v.id}>
                  <Table.Cell>v{v.number}</Table.Cell>
                  <Table.Cell>
                    <Code>{short(v.id)}</Code>
                  </Table.Cell>
                  <Table.Cell>
                    <Code fontSize="xs">{JSON.stringify(v.stats)}</Code>
                  </Table.Cell>
                  <Table.Cell>{when(v.created_at)}</Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table.Root>
        </>
      )}
    </Section>
  );
}
