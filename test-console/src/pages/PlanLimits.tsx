import { useState } from 'react';
import { Badge, Button, Card, Dialog, Field, Input, Portal, Stack, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { adminKeys } from '@/api/admin';
import { client, unwrap, type Schemas } from '@/api/client';
import { CardGrid, CardSkeletons, ErrorAlert, Facts, PageHeader, when } from '@/ui';

type Limits = Schemas['PlanLimits'];
type Body = Schemas['PlanLimitsBody'];

/** Every number a plan tier sets, as [group, field, label]. The API takes the whole body, so each one is always sent. */
const FIELDS: [keyof Body, string, string][] = [
  ['archives', 'upload_max_bytes', 'Archive upload (bytes)'],
  ['archives', 'extracted_max_bytes', 'Archive extracted (bytes)'],
  ['archives', 'max_files', 'Archive files'],
  ['concurrency', 'max_active_imports_per_tenant', 'Active imports per tenant'],
  ['concurrency', 'max_concurrent_training_runs', 'Training runs at once'],
  ['deployments', 'max_active_deployments', 'Active deployments'],
  ['huggingface', 'hf_max_repo_bytes', 'Hugging Face repo (bytes)'],
  ['images', 'image_max_bytes', 'Image (bytes)'],
  ['images', 'image_max_dimension', 'Image dimension (px)'],
  ['predict', 'max_body_bytes', 'Predict body (bytes)'],
  ['predict', 'max_inflight_per_deployment', 'Predict in flight per deployment'],
  ['tables', 'csv_max_rows', 'CSV rows'],
  ['tables', 'csv_max_columns', 'CSV columns'],
];

const valueOf = (limits: Limits, group: keyof Body, field: string) => (limits[group] as Record<string, number>)[field];
const keyOf = (group: keyof Body, field: string) => `${group}.${field}`;
const isPositiveInt = (v: string) => /^\d+$/.test(v) && Number(v) > 0;

/** The plan limits of every tier. A tier's limits apply to every tenant on it; the internal row is the test tenant's. */
export function PlanLimitsPage() {
  const limits = useQuery({ queryKey: adminKeys.planLimits, queryFn: () => unwrap(client.GET('/v1/admin/plan-limits')) });
  const [editing, setEditing] = useState<Limits | null>(null);

  return (
    <Stack gap="4">
      <PageHeader
        title="Plan limits"
        description="Quotas per plan tier: upload sizes, row and image limits, concurrency and predict throughput. Changes apply to the tenants on that tier at once."
      />
      <ErrorAlert error={limits.error} />
      {limits.isPending && <CardSkeletons count={2} />}
      <CardGrid min="26rem">
        {limits.data?.map((l) => (
          <Card.Root key={l.plan_tier} variant="outline">
            <Card.Header>
              <Card.Title>
                <Badge colorPalette={l.plan_tier === 'internal' ? 'orange' : 'gray'} variant="surface">
                  {l.plan_tier}
                </Badge>
              </Card.Title>
            </Card.Header>
            <Card.Body gap="3">
              <Facts items={FIELDS.map(([g, f, label]) => [label, valueOf(l, g, f).toLocaleString()])} />
              <Text fontSize="xs" color="fg.muted">
                Set by {l.updated_by}, {when(l.updated_at)}
              </Text>
              <Button size="sm" variant="outline" alignSelf="start" onClick={() => setEditing(l)}>
                Edit
              </Button>
            </Card.Body>
          </Card.Root>
        ))}
      </CardGrid>
      {editing && <EditLimits limits={editing} onClose={() => setEditing(null)} />}
    </Stack>
  );
}

function EditLimits({ limits, onClose }: { limits: Limits; onClose: () => void }) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map(([g, f]) => [keyOf(g, f), String(valueOf(limits, g, f))])),
  );
  const valid = FIELDS.every(([g, f]) => isPositiveInt(values[keyOf(g, f)]));
  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, Record<string, number>> = {};
      for (const [g, f] of FIELDS) (body[g] ??= {})[f] = Number(values[keyOf(g, f)]);
      return unwrap(client.PUT('/v1/admin/plan-limits/{tier}', { params: { path: { tier: limits.plan_tier } }, body: body as Body }));
    },
    onSuccess: () => {
      onClose();
      return qc.invalidateQueries({ queryKey: adminKeys.planLimits });
    },
  });

  return (
    <Dialog.Root open size="lg" scrollBehavior="inside" onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>Limits of the {limits.plan_tier} tier</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                {FIELDS.map(([g, f, label]) => (
                  <Field.Root key={keyOf(g, f)} invalid={!isPositiveInt(values[keyOf(g, f)])}>
                    <Field.Label>{label}</Field.Label>
                    <Input
                      size="sm"
                      type="number"
                      min={1}
                      value={values[keyOf(g, f)]}
                      onChange={(e) => setValues((v) => ({ ...v, [keyOf(g, f)]: e.target.value }))}
                    />
                  </Field.Root>
                ))}
                <ErrorAlert error={save.error} />
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button disabled={!valid} loading={save.isPending} onClick={() => save.mutate()}>
                Save
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
