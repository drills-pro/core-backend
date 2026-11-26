import { useState } from 'react';
import { Button, Dialog, Field, HStack, Input, Portal, SimpleGrid, Stack, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link as RouterLink, useNavigate } from '@tanstack/react-router';
import { adminKeys } from '@/api/admin';
import { client, unwrap, type Schemas } from '@/api/client';
import { usePagedList } from '@/api/paged';
import { IDENTITY_STATUSES, PLAN_TIERS } from '@/constants';
import { Empty, ErrorAlert, LoadMore, PageHeader, Rows, Select, Stat, StatusBadge, when } from '@/ui';

/** Every tenant on the platform: the operator's view across organisations. Internal tenants are left out unless the
 * filter asks for them (core-api). Open one for its members, projects and datasets, and to suspend it or change its settings. */
export function AllTenantsPage() {
  const [q, setQ] = useState('');
  const [planTier, setPlanTier] = useState('');
  const [status, setStatus] = useState('');
  const [creating, setCreating] = useState(false);

  const summary = useQuery({ queryKey: adminKeys.tenantsSummary, queryFn: () => unwrap(client.GET('/v1/admin/tenants/summary')) });
  const filters = { q: q.trim() || undefined, plan_tier: planTier || undefined, status: status || undefined };
  const list = usePagedList([...adminKeys.tenants, filters], (page_token) =>
    unwrap(client.GET('/v1/admin/tenants', { params: { query: { ...filters, page_token } } })),
  );

  const s = summary.data;
  return (
    <Stack gap="4">
      <PageHeader
        title="All tenants"
        description="Every tenant on the platform. Open one to see its members, projects and datasets, to suspend it or to change its settings."
        actions={
          <Button size="sm" colorPalette="orange" onClick={() => setCreating(true)}>
            New tenant
          </Button>
        }
      />
      <ErrorAlert error={summary.error} />
      {s && (
        <SimpleGrid minChildWidth="10rem" gap="3">
          <Stat label="Organisations" value={s.organizations} hint={`${s.organizations_new_30d} new in 30 days`} />
          <Stat label="Projects" value={s.projects} hint={`${s.projects_new_30d} new in 30 days`} />
          <Stat label="Members" value={s.members} />
        </SimpleGrid>
      )}
      <HStack wrap="wrap" gap="2">
        <Input size="sm" maxW="xs" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} />
        <Select
          value={planTier}
          onChange={setPlanTier}
          options={[{ value: '', label: 'Every plan but internal' }, ...PLAN_TIERS.map((t) => ({ value: t, label: t }))]}
        />
        <Select value={status} onChange={setStatus} options={[{ value: '', label: 'Any identity status' }, ...IDENTITY_STATUSES.map((v) => ({ value: v, label: v }))]} />
      </HStack>
      <ErrorAlert error={list.error} />
      {!list.isPending && list.items.length === 0 && <Empty title="No tenants match" />}
      {list.items.length > 0 && (
        <Rows
          headers={['Name', 'Plan', 'Identity', 'Owner', 'Projects', 'Members', 'Created']}
          items={list.items}
          rowKey={(t) => t.id}
          cells={(t) => [
            <RouterLink key="n" to="/all-tenants/$tenantId" params={{ tenantId: t.id }}>
              {t.name}
            </RouterLink>,
            t.plan_tier,
            <StatusBadge key="s" status={t.status} />,
            t.owner ? (t.owner.display_name ?? t.owner.login_name ?? t.owner.user_id) : '-',
            t.project_count,
            t.member_count,
            when(t.created_at),
          ]}
        />
      )}
      <LoadMore hasMore={list.hasMore} loading={list.loadingMore} onClick={list.loadMore} />
      {creating && <NewTenantDialog onClose={() => setCreating(false)} />}
    </Stack>
  );
}

/** Creates a tenant as its platform admin (who becomes its owner), then opens it. An org id adopts an existing Zitadel org. */
function NewTenantDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [planTier, setPlanTier] = useState<Schemas['PlanTier']>('free');
  const [orgId, setOrgId] = useState('');
  const create = useMutation({
    mutationFn: () =>
      unwrap(client.POST('/v1/admin/tenants', { body: { name: name.trim(), plan_tier: planTier, zitadel_org_id: orgId.trim() || null } })),
    onSuccess: (t) => {
      void qc.invalidateQueries({ queryKey: adminKeys.tenants });
      void qc.invalidateQueries({ queryKey: adminKeys.tenantsSummary });
      void navigate({ to: '/all-tenants/$tenantId', params: { tenantId: t.id } });
    },
  });

  return (
    <Dialog.Root open onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>New tenant</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                <Field.Root required>
                  <Field.Label>Name</Field.Label>
                  <Input size="sm" value={name} onChange={(e) => setName(e.target.value)} />
                </Field.Root>
                <Field.Root>
                  <Field.Label>Plan</Field.Label>
                  <Select value={planTier} onChange={(v) => setPlanTier(v as Schemas['PlanTier'])} options={PLAN_TIERS.map((t) => ({ value: t, label: t }))} />
                </Field.Root>
                <Field.Root>
                  <Field.Label>Zitadel organisation id (optional)</Field.Label>
                  <Input size="sm" placeholder="Leave empty to create a new organisation" value={orgId} onChange={(e) => setOrgId(e.target.value)} />
                </Field.Root>
                <Text color="fg.muted" fontSize="sm">
                  An internal tenant is the one the test console acts in.
                </Text>
                <ErrorAlert error={create.error} />
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button disabled={!name.trim()} loading={create.isPending} onClick={() => create.mutate()}>
                Create
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
