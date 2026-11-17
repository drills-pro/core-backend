import { useState } from 'react';
import { Alert, Button, Code, HStack, Input, Stack, Table, Text, Textarea } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, newKey, unwrap } from '@/api/client';
import { cfg } from '@/config';
import { DEPLOY_DONE, DEPLOY_ENDED, DeployStatus, POLL_MS, PREDICT_SCOPE, RunStatus } from '@/constants';
import { CopyButton, ErrorAlert, Section, Select, short, StatusBadge, when } from '@/ui';
import { runsQuery } from './Training';

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
  const [body, setBody] = useState('{\n  "input": {}\n}');

  const deploy = useMutation({
    mutationFn: () => unwrap(client.POST('/v1/projects/{project_id}/deployments', { params: { path, query: { training_run_id: runId }, header: { 'Idempotency-Key': newKey() } } })),
    onSuccess: (d) => setDeploymentId(d.id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['deployments', projectId] }),
  });
  const onSettled = () => qc.invalidateQueries({ queryKey: ['deployments', projectId] });
  const stop = useMutation({
    mutationFn: (id: string) =>
      unwrap(client.POST('/v1/projects/{project_id}/deployments/{deployment_id}/stop', { params: { path: { ...path, deployment_id: id } } })),
    onSettled,
  });
  const start = useMutation({
    mutationFn: (id: string) =>
      unwrap(client.POST('/v1/projects/{project_id}/deployments/{deployment_id}/start', { params: { path: { ...path, deployment_id: id } } })),
    onSettled,
  });
  const remove = useMutation({
    mutationFn: (id: string) =>
      unwrap(client.DELETE('/v1/projects/{project_id}/deployments/{deployment_id}', { params: { path: { ...path, deployment_id: id } } })),
    onSettled,
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
    <>
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
      <ErrorAlert error={deploy.error ?? deployments.error ?? stop.error ?? start.error ?? remove.error} />
      {deploy.data?.predict_key && (
        <Alert.Root status="info">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Predict key (shown once)</Alert.Title>
            <Alert.Description>
              <HStack>
                <Code>{deploy.data.predict_key}</Code>
                <CopyButton text={deploy.data.predict_key} />
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
                <HStack justify="flex-end">
                  {d.status === DeployStatus.STOPPED && (
                    <Button size="xs" variant="outline" loading={start.isPending && start.variables === d.id} onClick={(e) => { e.stopPropagation(); start.mutate(d.id); }}>
                      Start
                    </Button>
                  )}
                  {!DEPLOY_ENDED.includes(d.status) && (
                    <Button
                      size="xs"
                      variant="outline"
                      loading={stop.isPending && stop.variables === d.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (window.confirm(`Stop deployment ${short(d.id)}? Predict will answer 409 until you start it again.`)) stop.mutate(d.id);
                      }}
                    >
                      Stop
                    </Button>
                  )}
                  {d.status !== DeployStatus.DELETED && (
                    <Button
                      size="xs"
                      variant="outline"
                      colorPalette="red"
                      loading={remove.isPending && remove.variables === d.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (window.confirm(`Delete deployment ${short(d.id)}? Its predict key is cleared.`)) remove.mutate(d.id);
                      }}
                    >
                      Delete
                    </Button>
                  )}
                </HStack>
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
    <ModelsAndKeys projectId={projectId} />
    </>
  );
}

/** Model ids for `POST /v1/predict`, and the project API keys that call it. Key secrets live in state only. */
function ModelsAndKeys({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const path = { project_id: projectId };
  const models = useQuery({
    queryKey: ['models', projectId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/models', { params: { path } })),
  });
  const keys = useQuery({
    queryKey: ['api-keys', projectId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/api-keys', { params: { path } })),
  });
  const [name, setName] = useState('');
  const [secret, setSecret] = useState<string>();
  const [modelId, setModelId] = useState('');
  const onSettled = () => qc.invalidateQueries({ queryKey: ['api-keys', projectId] });
  const create = useMutation({
    mutationFn: () => unwrap(client.POST('/v1/projects/{project_id}/api-keys', { params: { path }, body: { name, scopes: [PREDICT_SCOPE], model_ids: [] } })),
    onSuccess: (k) => {
      setSecret(k.secret);
      setName('');
    },
    onSettled,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => unwrap(client.DELETE('/v1/projects/{project_id}/api-keys/{key_id}', { params: { path: { ...path, key_id: id } } })),
    onSettled,
  });

  const model = models.data?.find((m) => m.id === modelId);
  return (
    <Section title="4. Models and API keys">
      <ErrorAlert error={models.error ?? keys.error ?? create.error ?? revoke.error} />
      <Table.Root size="sm">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Model</Table.ColumnHeader>
            <Table.ColumnHeader>X-Model-Id</Table.ColumnHeader>
            <Table.ColumnHeader>Revision</Table.ColumnHeader>
            <Table.ColumnHeader>Stage</Table.ColumnHeader>
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {models.data?.map((m) => (
            <Table.Row key={m.id} onClick={() => setModelId(m.id)} cursor="pointer" bg={m.id === modelId ? 'orange.subtle' : undefined}>
              <Table.Cell>{m.mlflow_model_name}</Table.Cell>
              <Table.Cell>
                <HStack>
                  <Code>{m.model_id}</Code>
                  <CopyButton text={m.model_id} />
                </HStack>
              </Table.Cell>
              <Table.Cell>
                <HStack>
                  <Code>{m.mlflow_version}</Code>
                  <CopyButton text={m.mlflow_version} />
                </HStack>
              </Table.Cell>
              <Table.Cell>{m.stage}</Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
      <HStack>
        <Input size="sm" placeholder="Key name" value={name} onChange={(e) => setName(e.target.value)} />
        <Button size="sm" colorPalette="orange" disabled={!name.trim()} loading={create.isPending} onClick={() => create.mutate()}>
          Create predict key
        </Button>
      </HStack>
      {secret && (
        <Alert.Root status="info">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>API key (shown once)</Alert.Title>
            <Alert.Description>
              <HStack>
                <Code>{secret}</Code>
                <CopyButton text={secret} />
                <Button size="xs" variant="ghost" onClick={() => setSecret(undefined)}>
                  Dismiss
                </Button>
              </HStack>
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
      )}
      <Table.Root size="sm">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>Key</Table.ColumnHeader>
            <Table.ColumnHeader>Prefix</Table.ColumnHeader>
            <Table.ColumnHeader>Scopes</Table.ColumnHeader>
            <Table.ColumnHeader>Created</Table.ColumnHeader>
            <Table.ColumnHeader />
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {keys.data?.map((k) => (
            <Table.Row key={k.id}>
              <Table.Cell>{k.name}</Table.Cell>
              <Table.Cell>
                <Code>{k.prefix}</Code>
              </Table.Cell>
              <Table.Cell>{k.scopes.join(', ')}</Table.Cell>
              <Table.Cell>{when(k.created_at)}</Table.Cell>
              <Table.Cell textAlign="end">
                {k.revoked_at ? (
                  <StatusBadge status="revoked" />
                ) : (
                  <Button
                    size="xs"
                    variant="outline"
                    loading={revoke.isPending && revoke.variables === k.id}
                    onClick={() => window.confirm(`Revoke key ${k.name}?`) && revoke.mutate(k.id)}
                  >
                    Revoke
                  </Button>
                )}
              </Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
      <Stack>
        <Text fontWeight="semibold">Call POST /v1/predict{model ? ` for ${model.mlflow_model_name}` : ' (click a model row)'}</Text>
        <Code display="block" whiteSpace="pre-wrap" p="2" fontSize="xs">
          {`curl -X POST ${cfg.apiUrl}/v1/predict \\
  -H 'Authorization: Bearer ${secret ?? '<key>'}' \\
  -H 'X-Project-Id: ${projectId}' \\
  -H 'X-Model-Id: ${model?.model_id ?? '<model_id>'}' \\${model ? `\n  -H 'X-Model-Revision: ${model.mlflow_version}' \\` : ''}
  -H 'Content-Type: application/json' \\
  -d '{"input": {}, "metadata": {}}'`}
        </Code>
      </Stack>
    </Section>
  );
}
