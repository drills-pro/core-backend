import { useState } from 'react';
import { Code, Heading, HStack, Stack, Text } from '@chakra-ui/react';
import { useQuery } from '@tanstack/react-query';
import { useParams } from '@tanstack/react-router';
import { client, unwrap } from '@/api/client';
import { ErrorAlert, short } from '@/ui';
import { Datasets } from './pipeline/Datasets';
import { Deployments } from './pipeline/Deployments';
import { Training } from './pipeline/Training';

/** One project's happy path, top to bottom: import a dataset version, train on it, deploy the run, call it. */
export function PipelinePage() {
  const { projectId } = useParams({ strict: false }) as { projectId: string };
  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}', { params: { path: { project_id: projectId } } })),
  });
  const [datasetId, setDatasetId] = useState('');

  return (
    <Stack gap="4">
      <HStack gap="3">
        <Heading>{project.data?.name ?? '…'}</Heading>
        <Code>{short(projectId)}</Code>
        <Text color="fg.muted">{project.data?.task_type}</Text>
      </HStack>
      <ErrorAlert error={project.error} />
      <Datasets projectId={projectId} datasetId={datasetId} onDataset={setDatasetId} />
      <Training projectId={projectId} datasetId={datasetId} />
      <Deployments projectId={projectId} />
    </Stack>
  );
}
