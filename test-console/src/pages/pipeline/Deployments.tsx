import { useState } from 'react';
import { Alert, Badge, Button, Card, Code, HStack, Input, Stack, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { client, newKey, unwrap } from '@/api/client';
import { cfg } from '@/config';
import { DEPLOY_DONE, DEPLOY_ENDED, DeployStatus, POLL_MS, PREDICT_SCOPE, RunStatus } from '@/constants';
import { CardGrid, ConfirmDialog, CopyButton, Empty, ErrorAlert, Facts, Id, Section, Select, short, StatusBadge, when } from '@/ui';
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
  // The try-it log: console predictions made through the session route, newest first.
  const test = useQuery({
    queryKey: ['test-calls', projectId, deploymentId],
    queryFn: () => unwrap(client.GET('/v1/projects/{project_id}/deployments/{deployment_id}/test-calls', { params: { path: { ...path, deployment_id: deploymentId } } })),
    enabled: !!deploymentId,
  });

  const [confirm, setConfirm] = useState<{ kind: 'stop' | 'delete'; id: string } | null>(null);
  const succeeded = (runs.data?.items ?? []).filter((r) => r.status === RunStatus.SUCCEEDED);
  const selected = deployments.data?.find((d) => d.id === deploymentId);
  const closeConfirm = () => {
    stop.reset();
    remove.reset();
    setConfirm(null);
  };
  return (
    <Stack gap="4">
      <Section title="Deploy a run" description="Serve a succeeded training run behind a predict route.">
        <HStack wrap="wrap">
          <Select
            value={runId}
            onChange={setRunId}
            placeholder="A succeeded run"
            options={succeeded.map((r) => ({ value: r.id, label: `${short(r.id)} ${r.preset ?? ''}` }))}
          />
          <Button colorPalette="orange" disabled={!runId} loading={deploy.isPending} onClick={() => deploy.mutate()}>
            Deploy
          </Button>
        </HStack>
        <ErrorAlert error={deploy.error ?? deployments.error ?? start.error} />
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
      </Section>

      <Section title="Deployments" description="Pick one to send it a test call.">
        {deployments.data?.length === 0 && <Empty title="No deployments yet" />}
        <CardGrid>
          {deployments.data?.map((d) => (
            <Card.Root
              key={d.id}
              size="sm"
              variant="outline"
              cursor="pointer"
              borderColor={d.id === deploymentId ? 'orange.emphasized' : undefined}
              bg={d.id === deploymentId ? 'orange.subtle' : undefined}
              onClick={() => setDeploymentId(d.id)}
            >
              <Card.Header>
                <HStack justify="space-between">
                  <Id value={d.id} />
                  <StatusBadge status={d.status} />
                </HStack>
              </Card.Header>
              <Card.Body>
                <Facts
                  items={[
                    ['Route', <Code key="r" fontSize="xs">{d.route_prefix}</Code>],
                    ['Created', when(d.created_at)],
                  ]}
                />
              </Card.Body>
              <Card.Footer justifyContent="flex-end">
                {d.status === DeployStatus.STOPPED && (
                  <Button size="xs" variant="outline" loading={start.isPending && start.variables === d.id} onClick={(e) => { e.stopPropagation(); start.mutate(d.id); }}>
                    Start
                  </Button>
                )}
                {!DEPLOY_ENDED.includes(d.status) && (
                  <Button size="xs" variant="outline" onClick={(e) => { e.stopPropagation(); setConfirm({ kind: 'stop', id: d.id }); }}>
                    Stop
                  </Button>
                )}
                {d.status !== DeployStatus.DELETED && (
                  <Button size="xs" variant="outline" colorPalette="red" onClick={(e) => { e.stopPropagation(); setConfirm({ kind: 'delete', id: d.id }); }}>
                    Delete
                  </Button>
                )}
              </Card.Footer>
            </Card.Root>
          ))}
        </CardGrid>
      </Section>

      {deploymentId && (
        <Section title={`Test calls of ${short(deploymentId)}`} description={selected ? `Status: ${selected.status}` : undefined}>
          <ErrorAlert error={test.error} />
          {test.data && (
            <Code display="block" whiteSpace="pre-wrap" p="2" fontSize="xs">
              {JSON.stringify(test.data, null, 2)}
            </Code>
          )}
        </Section>
      )}

      {confirm?.kind === 'stop' && (
        <ConfirmDialog title={`Stop deployment ${short(confirm.id)}?`} confirmLabel="Stop" loading={stop.isPending} error={stop.error} onClose={closeConfirm} onConfirm={() => stop.mutate(confirm.id, { onSuccess: closeConfirm })}>
          <Text>Predict answers 409 until you start it again.</Text>
        </ConfirmDialog>
      )}
      {confirm?.kind === 'delete' && (
        <ConfirmDialog title={`Delete deployment ${short(confirm.id)}?`} confirmLabel="Delete" danger loading={remove.isPending} error={remove.error} onClose={closeConfirm} onConfirm={() => remove.mutate(confirm.id, { onSuccess: closeConfirm })}>
          <Text>Its predict key is cleared.</Text>
        </ConfirmDialog>
      )}
    </Stack>
  );
}

/** Model ids for `POST /v1/predict`, and the project API keys that call it. Key secrets live in state only. */
export function ModelsAndKeys({ projectId }: { projectId: string }) {
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

  const [revoking, setRevoking] = useState<(typeof keys.data extends (infer K)[] | undefined ? K : never) | null>(null);
  const model = models.data?.find((m) => m.id === modelId);
  return (
    <Stack gap="4">
      <ErrorAlert error={models.error ?? keys.error ?? create.error} />
      <Section title="Models" description="The ids POST /v1/predict needs. Pick one to fill the example call below.">
        {models.data?.length === 0 && <Empty title="No models yet">A succeeded training run registers one.</Empty>}
        <CardGrid>
          {models.data?.map((m) => (
            <Card.Root
              key={m.id}
              size="sm"
              variant="outline"
              cursor="pointer"
              borderColor={m.id === modelId ? 'orange.emphasized' : undefined}
              bg={m.id === modelId ? 'orange.subtle' : undefined}
              onClick={() => setModelId(m.id)}
            >
              <Card.Header>
                <HStack justify="space-between">
                  <Card.Title>{m.mlflow_model_name}</Card.Title>
                  <Badge variant="surface">{m.stage}</Badge>
                </HStack>
              </Card.Header>
              <Card.Body>
                <Facts
                  items={[
                    [
                      'X-Model-Id',
                      <HStack key="i">
                        <Code fontSize="xs">{m.model_id}</Code>
                        <CopyButton text={m.model_id} />
                      </HStack>,
                    ],
                    [
                      'Revision',
                      <HStack key="r">
                        <Code fontSize="xs">{m.mlflow_version}</Code>
                        <CopyButton text={m.mlflow_version} />
                      </HStack>,
                    ],
                  ]}
                />
              </Card.Body>
            </Card.Root>
          ))}
        </CardGrid>
      </Section>

      <Section title="API keys" description="Project keys that may call predict. A secret is shown once, when you create it.">
        <HStack>
          <Input size="sm" maxW="xs" placeholder="Key name" value={name} onChange={(e) => setName(e.target.value)} />
          <Button colorPalette="orange" disabled={!name.trim()} loading={create.isPending} onClick={() => create.mutate()}>
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
        {keys.data?.length === 0 && <Empty title="No keys yet" />}
        <CardGrid min="18rem">
          {keys.data?.map((k) => (
            <Card.Root key={k.id} size="sm" variant="subtle" opacity={k.revoked_at ? 0.6 : 1}>
              <Card.Header>
                <HStack justify="space-between">
                  <Card.Title>{k.name}</Card.Title>
                  {k.revoked_at ? <StatusBadge status="revoked" /> : <Code>{k.prefix}</Code>}
                </HStack>
              </Card.Header>
              <Card.Body>
                <Facts
                  items={[
                    ['Scopes', k.scopes.join(', ')],
                    ['Created', when(k.created_at)],
                  ]}
                />
              </Card.Body>
              {!k.revoked_at && (
                <Card.Footer justifyContent="flex-end">
                  <Button size="xs" variant="outline" colorPalette="red" onClick={() => setRevoking(k)}>
                    Revoke
                  </Button>
                </Card.Footer>
              )}
            </Card.Root>
          ))}
        </CardGrid>
      </Section>

      <Section title={`Call POST /v1/predict${model ? ` for ${model.mlflow_model_name}` : ''}`} description={model ? undefined : 'Pick a model above to fill in its ids.'}>
        <Code display="block" whiteSpace="pre-wrap" p="2" fontSize="xs">
          {`curl -X POST ${cfg.apiUrl}/v1/predict \\
  -H 'Authorization: Bearer ${secret ?? '<key>'}' \\
  -H 'X-Project-Id: ${projectId}' \\
  -H 'X-Model-Id: ${model?.model_id ?? '<model_id>'}' \\${model ? `\n  -H 'X-Model-Revision: ${model.mlflow_version}' \\` : ''}
  -H 'Content-Type: application/json' \\
  -d '{"input": {}, "metadata": {}}'`}
        </Code>
      </Section>

      {revoking && (
        <ConfirmDialog
          title={`Revoke key ${revoking.name}?`}
          confirmLabel="Revoke"
          danger
          loading={revoke.isPending}
          error={revoke.error}
          onClose={() => {
            revoke.reset();
            setRevoking(null);
          }}
          onConfirm={() => revoke.mutate(revoking.id, { onSuccess: () => setRevoking(null) })}
        >
          <Text>Calls that use it stop working. This cannot be undone.</Text>
        </ConfirmDialog>
      )}
    </Stack>
  );
}
