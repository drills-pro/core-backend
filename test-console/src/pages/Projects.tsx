import { useState } from 'react';
import { Badge, Breadcrumb, Button, Card, Dialog, Field, HStack, Input, LinkBox, LinkOverlay, Portal, Stack } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link as RouterLink, useParams } from '@tanstack/react-router';
import { client, unwrap, type Schemas } from '@/api/client';
import { tenantQuery } from '@/api/queries';
import { TASK_TYPES } from '@/constants';
import { CardGrid, CardSkeletons, Empty, ErrorAlert, Facts, Id, PageHeader, Select, StatusBadge, when } from '@/ui';

export function ProjectsPage() {
  const { tenantId } = useParams({ strict: false }) as { tenantId: string };
  const tenant = useQuery(tenantQuery(tenantId)).data;
  const projects = useQuery({ queryKey: ['projects', tenantId], queryFn: () => unwrap(client.GET('/v1/projects')) });
  const [creating, setCreating] = useState(false);

  return (
    <Stack gap="4">
      <PageHeader
        back={
          <Breadcrumb.Root size="sm">
            <Breadcrumb.List>
              <Breadcrumb.Item>
                <Breadcrumb.Link asChild>
                  <RouterLink to="/">Tenants</RouterLink>
                </Breadcrumb.Link>
              </Breadcrumb.Item>
              <Breadcrumb.Separator />
              <Breadcrumb.Item>
                <Breadcrumb.CurrentLink>{tenant?.name ?? '…'}</Breadcrumb.CurrentLink>
              </Breadcrumb.Item>
            </Breadcrumb.List>
          </Breadcrumb.Root>
        }
        title="Projects"
        description="Each project holds datasets, training runs and deployments for one task."
        actions={
          <Button colorPalette="orange" onClick={() => setCreating(true)}>
            New project
          </Button>
        }
      />
      <ErrorAlert error={projects.error} />
      {projects.isPending ? (
        <CardSkeletons />
      ) : projects.data?.items.length ? (
        <CardGrid>
          {projects.data.items.map((p) => (
            <LinkBox key={p.id} asChild>
              <Card.Root variant="outline" _hover={{ borderColor: 'orange.emphasized', shadow: 'sm' }}>
                <Card.Header>
                  <HStack justify="space-between">
                    <Card.Title>
                      <LinkOverlay asChild>
                        <RouterLink to="/t/$tenantId/p/$projectId" params={{ tenantId, projectId: p.id }}>
                          {p.name}
                        </RouterLink>
                      </LinkOverlay>
                    </Card.Title>
                    <StatusBadge status={p.status} />
                  </HStack>
                </Card.Header>
                <Card.Body gap="2">
                  <Badge alignSelf="start" variant="surface">
                    {p.task_type}
                  </Badge>
                  <Facts
                    items={[
                      ['Id', <Id key="id" value={p.id} />],
                      ['Created', when(p.created_at)],
                    ]}
                  />
                </Card.Body>
              </Card.Root>
            </LinkBox>
          ))}
        </CardGrid>
      ) : (
        <Empty title="No projects yet">Create one to start importing a dataset.</Empty>
      )}
      {creating && <NewProject tenantId={tenantId} onClose={() => setCreating(false)} />}
    </Stack>
  );
}

function NewProject({ tenantId, onClose }: { tenantId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const [task, setTask] = useState<Schemas['TaskType']>(TASK_TYPES[0]);
  const create = useMutation({
    mutationFn: () => unwrap(client.POST('/v1/projects', { params: { query: { name: name.trim(), task_type: task } } })),
    onSuccess: () => {
      onClose();
      return qc.invalidateQueries({ queryKey: ['projects', tenantId] });
    },
  });
  return (
    <Dialog.Root open onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                create.mutate();
              }}
            >
              <Dialog.Header>
                <Dialog.Title>New project</Dialog.Title>
              </Dialog.Header>
              <Dialog.Body>
                <Stack gap="3">
                  <Field.Root required>
                    <Field.Label>Name</Field.Label>
                    <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} />
                  </Field.Root>
                  <Field.Root>
                    <Field.Label>Task</Field.Label>
                    <Select value={task} onChange={(v) => setTask(v as Schemas['TaskType'])} options={TASK_TYPES.map((t) => ({ value: t, label: t }))} />
                  </Field.Root>
                  <ErrorAlert error={create.error} />
                </Stack>
              </Dialog.Body>
              <Dialog.Footer>
                <Button variant="outline" onClick={onClose}>
                  Cancel
                </Button>
                <Button type="submit" colorPalette="orange" disabled={!name.trim()} loading={create.isPending}>
                  Create
                </Button>
              </Dialog.Footer>
            </form>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
