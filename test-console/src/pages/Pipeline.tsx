import { useState } from 'react';
import { Badge, Breadcrumb, HStack, Stack, Tabs } from '@chakra-ui/react';
import { useQuery } from '@tanstack/react-query';
import { Link as RouterLink, useParams } from '@tanstack/react-router';
import { client, unwrap } from '@/api/client';
import { tenantQuery } from '@/api/queries';
import { ErrorAlert, Id, PageHeader } from '@/ui';
import { Datasets } from './pipeline/Datasets';
import { Deployments, ModelsAndKeys } from './pipeline/Deployments';
import { Training } from './pipeline/Training';

/** One project's happy path as tabs, in order: import a dataset version, train on it, deploy the run, call it. */
export function PipelinePage() {
  const { tenantId, projectId } = useParams({ strict: false }) as { tenantId: string; projectId: string };
  const tenant = useQuery(tenantQuery(tenantId)).data;
  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}', { params: { path: { project_id: projectId } } })),
  });
  const [datasetId, setDatasetId] = useState('');

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
                <Breadcrumb.Link asChild>
                  <RouterLink to="/t/$tenantId" params={{ tenantId }}>
                    {tenant?.name ?? '…'}
                  </RouterLink>
                </Breadcrumb.Link>
              </Breadcrumb.Item>
              <Breadcrumb.Separator />
              <Breadcrumb.Item>
                <Breadcrumb.CurrentLink>{project.data?.name ?? '…'}</Breadcrumb.CurrentLink>
              </Breadcrumb.Item>
            </Breadcrumb.List>
          </Breadcrumb.Root>
        }
        title={project.data?.name ?? '…'}
        description={
          <HStack>
            <Id value={projectId} />
            {project.data && <Badge variant="surface">{project.data.task_type}</Badge>}
          </HStack>
        }
      />
      <ErrorAlert error={project.error} />
      <Tabs.Root defaultValue="dataset" colorPalette="orange" lazyMount unmountOnExit>
        <Tabs.List>
          <Tabs.Trigger value="dataset">1 · Dataset</Tabs.Trigger>
          <Tabs.Trigger value="training">2 · Training</Tabs.Trigger>
          <Tabs.Trigger value="deploy">3 · Deploy and predict</Tabs.Trigger>
          <Tabs.Trigger value="keys">4 · Models and API keys</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content colorPalette="gray" value="dataset">
          <Datasets projectId={projectId} datasetId={datasetId} onDataset={setDatasetId} />
        </Tabs.Content>
        <Tabs.Content colorPalette="gray" value="training">
          <Training projectId={projectId} datasetId={datasetId} />
        </Tabs.Content>
        <Tabs.Content colorPalette="gray" value="deploy">
          <Deployments projectId={projectId} />
        </Tabs.Content>
        <Tabs.Content colorPalette="gray" value="keys">
          <ModelsAndKeys projectId={projectId} />
        </Tabs.Content>
      </Tabs.Root>
    </Stack>
  );
}
