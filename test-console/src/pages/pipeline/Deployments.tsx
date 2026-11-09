import { useState } from 'react';
import { Alert, Button, Code, HStack, Stack, Table, Text, Textarea } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, newKey, unwrap } from '@/api/client';
import { cfg } from '@/config';
import { DEPLOY_DONE, DEPLOY_ENDED, POLL_MS, RunStatus } from '@/constants';
import { ErrorAlert, Section, Select, short, StatusBadge, when } from '@/ui';
import { runsQuery } from './Training';

const COPIED_MS = 1_500;

export function Deployments({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const path = { project_id: projectId };
  const runs = useQuery(runsQuery(projectId));
  const deployments = useQuery({
    queryKey: ['deployments', projectId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/deployments', { params: { path } })),
    refetchInterval: (q) => (q.state.data?.some((d) => !DEPLOY_DONE.includes(d.status)) ? POLL_MS : false),
  });
  const [runId, setRunId] = useState('');
  const [deploymentId, setDeploymentId] = useState('');
  const [body, setBody] = useState('{\n  "inputs": []\n}');

  const deploy = useMutation({
    mutationFn: () => unwrap(client.POST('/v1/projects/{project_id}/deployments', { params: { path, query: { training_run_id: runId }, header: { 'Idempotency-Key': newKey() } } })),
    onSuccess: (d) => setDeploymentId(d.id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['deployments', projectId] }),
  });
  const stop = useMutation({
    mutationFn: (id: string) =>
      unwrap(client.DELETE('/v1/projects/{project_id}/deployments/{deployment_id}', { params: { path: { ...path, deployment_id: id } } })),
    onSettled: () => qc.invalidateQueries({ queryKey: ['deployments', projectId] }),
  });
  const [copied, setCopied] = useState(false);
  const copyKey = (key: string) =>
    navigator.clipboard.writeText(key).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), COPIED_MS);
    });
  // Through the session route, not /v1/predict: same Serve path, no predict key needed, and the call is logged.
  const test = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST('/v1/projects/{project_id}/deployments/{deployment_id}/test', {
          params: { path: { ...path, deployment_id: deploymentId } },
          body: JSON.parse(body),
        }),
      ),
  });

  const succeeded = (runs.data ?? []).filter((r) => r.status === RunStatus.SUCCEEDED);
  return (
    <Section title="3. Deploy and predict">
      <HStack>
        <Select
          value={runId}
          onChange={setRunId}
          placeholder="A succeeded run"
          options={succeeded.map((r) => ({ value: r.id, label: `${short(r.id)} ${r.preset ?? ''}` }))}
        />
        <Button size="sm" colorPalette="orange" disabled={!runId} loading={deploy.isPending} onClick={() => deploy.mutate()}>
          Deploy
        </Button>
      </HStack>
      <ErrorAlert error={deploy.error ?? deployments.error ?? stop.error} />
      {deploy.data?.predict_key && (
        <Alert.Root status="info">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Predict key (shown once)</Alert.Title>
            <Alert.Description>
              <HStack>
                <Code>{deploy.data.predict_key}</Code>
                <Button size="xs" variant="outline" onClick={() => copyKey(deploy.data.predict_key!)}>
                  {copied ? 'Copied' : 'Copy'}
                </Button>
              </HStack>
              <Code display="block" whiteSpace="pre-wrap" mt="2" p="2" fontSize="xs">
                {`curl -X POST ${cfg.apiUrl}/v1/predict/${deploy.data.id} \\
  -H 'X-Predict-Key: ${deploy.data.predict_key}' \\
  -H 'Content-Type: application/json' \\
  -d '{"inputs": []}'`}
              </Code>
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
      )}
      <Table.Root size="sm">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Deployment</Table.ColumnHeader>
            <Table.ColumnHeader>Route</Table.ColumnHeader>
            <Table.ColumnHeader>Status</Table.ColumnHeader>
            <Table.ColumnHeader>Created</Table.ColumnHeader>
            <Table.ColumnHeader />
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {deployments.data?.map((d) => (
            <Table.Row key={d.id} onClick={() => setDeploymentId(d.id)} cursor="pointer" bg={d.id === deploymentId ? 'orange.subtle' : undefined}>
              <Table.Cell>
                <Code>{short(d.id)}</Code>
              </Table.Cell>
              <Table.Cell>{d.route_prefix}</Table.Cell>
              <Table.Cell>
                <StatusBadge status={d.status} />
              </Table.Cell>
              <Table.Cell>{when(d.created_at)}</Table.Cell>
              <Table.Cell textAlign="end">
                {!DEPLOY_ENDED.includes(d.status) && (
                  <Button
                    size="xs"
                    variant="outline"
                    loading={stop.isPending && stop.variables === d.id}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (window.confirm(`Stop deployment ${short(d.id)}? Predict will answer 409.`)) stop.mutate(d.id);
                    }}
                  >
                    Stop
                  </Button>
                )}
              </Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
      {deploymentId && (
        <Stack>
          <Text fontWeight="semibold">Test call to {short(deploymentId)}</Text>
          <Textarea fontFamily="mono" fontSize="sm" rows={6} value={body} onChange={(e) => setBody(e.target.value)} />
          <HStack>
            <Button size="sm" colorPalette="orange" loading={test.isPending} onClick={() => test.mutate()}>
              Send
            </Button>
            {test.data && <Text color="fg.muted">{test.data.latency_ms.toFixed(0)} ms</Text>}
          </HStack>
          <ErrorAlert error={test.error} />
          {test.data && (
            <Code display="block" whiteSpace="pre-wrap" p="2" fontSize="xs">
              {JSON.stringify(test.data.response, null, 2)}
            </Code>
          )}
        </Stack>
      )}
    </Section>
  );
}
