import { useState } from 'react';
import { Button, Code, HStack, Input, Link, Stack, Table } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link as RouterLink, useParams } from '@tanstack/react-router';
import { client, unwrap, type Schemas } from '@/api/client';
import { tenantQuery } from '@/api/queries';
import { TASK_TYPES } from '@/constants';
import { ErrorAlert, Section, Select, short, StatusBadge, when } from '@/ui';

export function ProjectsPage() {
  const { tenantId } = useParams({ strict: false }) as { tenantId: string };
  const qc = useQueryClient();
  const tenant = useQuery(tenantQuery(tenantId)).data;
  const projects = useQuery({ queryKey: ['projects', tenantId], queryFn: () => unwrap(client.GET('/v1/projects')) });
  const [name, setName] = useState('');
  const [task, setTask] = useState<Schemas['TaskType']>('image-classification');
  const create = useMutation({
    mutationFn: () => unwrap(client.POST('/v1/projects', { params: { query: { name, task_type: task } } })),
    onSuccess: () => {
      setName('');
      return qc.invalidateQueries({ queryKey: ['projects', tenantId] });
    },
  });

  return (
    <Section title={`Projects in ${tenant?.name ?? '…'}`}>
      <HStack>
        <Input size="sm" placeholder="Project name" value={name} onChange={(e) => setName(e.target.value)} />
        <Select value={task} onChange={(v) => setTask(v as Schemas['TaskType'])} options={TASK_TYPES.map((t) => ({ value: t, label: t }))} />
        <Button size="sm" colorPalette="orange" disabled={!name.trim()} loading={create.isPending} onClick={() => create.mutate()}>
          Create
        </Button>
      </HStack>
      <ErrorAlert error={create.error ?? projects.error} />
      <Stack>
        <Table.Root size="sm">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>Name</Table.ColumnHeader>
              <Table.ColumnHeader>Id</Table.ColumnHeader>
              <Table.ColumnHeader>Task</Table.ColumnHeader>
              <Table.ColumnHeader>Status</Table.ColumnHeader>
              <Table.ColumnHeader>Created</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {projects.data?.items.map((p) => (
              <Table.Row key={p.id}>
                <Table.Cell>
                  <Link asChild colorPalette="orange">
                    <RouterLink to="/t/$tenantId/p/$projectId" params={{ tenantId, projectId: p.id }}>
                      {p.name}
                    </RouterLink>
                  </Link>
                </Table.Cell>
                <Table.Cell>
                  <Code>{short(p.id)}</Code>
                </Table.Cell>
                <Table.Cell>{p.task_type}</Table.Cell>
                <Table.Cell>
                  <StatusBadge status={p.status} />
                </Table.Cell>
                <Table.Cell>{when(p.created_at)}</Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      </Stack>
    </Section>
  );
}
