import { useState } from 'react';
import { Badge, Button, Code, HStack, Stack, Switch, Tabs, Text } from '@chakra-ui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link as RouterLink, useParams } from '@tanstack/react-router';
import { adminKeys } from '@/api/admin';
import { client, unwrap, type Schemas } from '@/api/client';
import { TenantStatus } from '@/constants';
import { ConfirmDialog, Empty, ErrorAlert, Facts, Id, PageHeader, Rows, Section, StatusBadge, when } from '@/ui';

const fetchTenant = (tenantId: string) => unwrap(client.GET('/v1/admin/tenants/{tenant_id}', { params: { path: { tenant_id: tenantId } } }));
type Settings = Schemas['TenantSettings'];

const SETTINGS: [keyof Settings, string][] = [
  ['allow_public_models', 'Allow public models'],
  ['members_can_create_projects', 'Members can create projects'],
  ['require_review_before_training', 'Require review before training'],
];
/** The whole settings object: the PATCH takes every switch, so a change is sent with the others as they are. */
const allSettings = (s: Settings): Required<Settings> => ({
  allow_public_models: s.allow_public_models ?? false,
  members_can_create_projects: s.members_can_create_projects ?? false,
  require_review_before_training: s.require_review_before_training ?? false,
});

export function AllTenantDetailPage() {
  const { tenantId } = useParams({ strict: false }) as { tenantId: string };
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: adminKeys.tenant(tenantId), queryFn: () => fetchTenant(tenantId) });
  const [confirm, setConfirm] = useState<typeof TenantStatus.ACTIVE | typeof TenantStatus.SUSPENDED | null>(null);
  const refresh = () => Promise.all([adminKeys.tenant(tenantId), adminKeys.tenants, adminKeys.tenantsSummary].map((queryKey) => qc.invalidateQueries({ queryKey })));

  const setStatus = useMutation({
    mutationFn: (status: string) => unwrap(client.POST('/v1/admin/tenants/{tenant_id}/status', { params: { path: { tenant_id: tenantId } }, body: { status } })),
    onSuccess: () => {
      setConfirm(null);
      return refresh();
    },
  });
  const saveSettings = useMutation({
    mutationFn: (changes: Settings) => unwrap(client.PATCH('/v1/admin/tenants/{tenant_id}/settings', { params: { path: { tenant_id: tenantId } }, body: changes })),
    onSuccess: () => refresh(),
  });

  const t = detail.data;
  return (
    <Stack gap="4">
      <Button asChild variant="ghost" size="sm" alignSelf="start">
        <RouterLink to="/all-tenants">← All tenants</RouterLink>
      </Button>
      <ErrorAlert error={detail.error} />
      {t && (
        <>
          <PageHeader
            title={t.name}
            description={
              <HStack>
                <Badge variant="surface">{t.plan_tier}</Badge>
                <StatusBadge status={t.status} />
              </HStack>
            }
            actions={
              <HStack>
                <Button size="sm" variant="outline" colorPalette="green" onClick={() => setConfirm(TenantStatus.ACTIVE)}>
                  Reactivate
                </Button>
                <Button size="sm" variant="outline" colorPalette="red" onClick={() => setConfirm(TenantStatus.SUSPENDED)}>
                  Suspend
                </Button>
              </HStack>
            }
          />
          <Section title="Tenant">
            <Facts
              items={[
                ['Id', <Id key="id" value={t.id} />],
                ['Organisation', t.zitadel_org_id ? <Code key="o">{t.zitadel_org_id}</Code> : <Text key="o" color="fg.muted">none</Text>],
                ['Owner', t.owner ? (t.owner.display_name ?? t.owner.login_name ?? t.owner.user_id) : '-'],
                ['Projects', t.project_count],
                ['Members', t.member_count],
                ['Datasets', t.dataset_count],
                ['Models', t.model_count],
                ['Created', when(t.created_at)],
              ]}
            />
          </Section>
          <Section title="Settings">
            <Stack gap="3">
              {SETTINGS.map(([key, label]) => (
                <Switch.Root
                  key={key}
                  checked={allSettings(t.settings)[key]}
                  disabled={saveSettings.isPending}
                  onCheckedChange={(e) => saveSettings.mutate({ ...allSettings(t.settings), [key]: e.checked })}
                >
                  <Switch.HiddenInput />
                  <Switch.Control />
                  <Switch.Label>{label}</Switch.Label>
                </Switch.Root>
              ))}
            </Stack>
            <ErrorAlert error={saveSettings.error} />
          </Section>
          <Tabs.Root defaultValue="members" variant="line" colorPalette="orange" lazyMount>
            <Tabs.List>
              <Tabs.Trigger value="members">Members</Tabs.Trigger>
              <Tabs.Trigger value="projects">Projects</Tabs.Trigger>
              <Tabs.Trigger value="datasets">Datasets</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Content colorPalette="gray" value="members">
              <MembersTab tenantId={tenantId} />
            </Tabs.Content>
            <Tabs.Content colorPalette="gray" value="projects">
              <ProjectsTab tenantId={tenantId} />
            </Tabs.Content>
            <Tabs.Content colorPalette="gray" value="datasets">
              <DatasetsTab tenantId={tenantId} />
            </Tabs.Content>
          </Tabs.Root>
        </>
      )}
      {t && confirm && (
        <ConfirmDialog
          title={confirm === TenantStatus.SUSPENDED ? `Suspend ${t.name}` : `Reactivate ${t.name}`}
          danger={confirm === TenantStatus.SUSPENDED}
          confirmLabel={confirm === TenantStatus.SUSPENDED ? 'Suspend' : 'Reactivate'}
          loading={setStatus.isPending}
          error={setStatus.error}
          onConfirm={() => setStatus.mutate(confirm)}
          onClose={() => setConfirm(null)}
        >
          <Text>
            {confirm === TenantStatus.SUSPENDED
              ? 'The tenant is suspended, and its Zitadel organisation is deactivated too.'
              : 'The tenant is active again, and its Zitadel organisation is reactivated too.'}
          </Text>
        </ConfirmDialog>
      )}
    </Stack>
  );
}

function MembersTab({ tenantId }: { tenantId: string }) {
  const members = useQuery({
    queryKey: [...adminKeys.tenant(tenantId), 'members'],
    queryFn: () => unwrap(client.GET('/v1/admin/tenants/{tenant_id}/members', { params: { path: { tenant_id: tenantId } } })),
  });
  if (members.error) return <ErrorAlert error={members.error} />;
  if (members.data?.length === 0) return <Empty title="No members" />;
  return (
    <Rows
      headers={['User', 'Email', 'Roles', 'Projects', 'Identity', 'Joined']}
      items={members.data ?? []}
      rowKey={(m) => m.user_id}
      cells={(m) => [
        m.display_name ?? m.login_name ?? m.user_id,
        m.email ?? '-',
        m.roles.join(', ') || '-',
        m.project_count,
        <StatusBadge key="s" status={m.status} />,
        when(m.joined_at),
      ]}
    />
  );
}

function ProjectsTab({ tenantId }: { tenantId: string }) {
  const projects = useQuery({
    queryKey: [...adminKeys.tenant(tenantId), 'projects'],
    queryFn: () => unwrap(client.GET('/v1/admin/tenants/{tenant_id}/projects', { params: { path: { tenant_id: tenantId } } })),
  });
  if (projects.error) return <ErrorAlert error={projects.error} />;
  if (projects.data?.length === 0) return <Empty title="No projects" />;
  return (
    <Rows
      headers={['Name', 'Status', 'Task', 'Visibility', 'Datasets', 'Models', 'Model', 'Created']}
      items={projects.data ?? []}
      rowKey={(p) => p.id}
      cells={(p) => [
        p.name,
        <StatusBadge key="s" status={p.status} />,
        p.task_type ?? '-',
        p.visibility,
        p.dataset_count,
        p.model_count,
        p.model_name ? `${p.model_name} ${p.model_version ?? ''}`.trim() : '-',
        when(p.created_at),
      ]}
    />
  );
}

function DatasetsTab({ tenantId }: { tenantId: string }) {
  const datasets = useQuery({
    queryKey: [...adminKeys.tenant(tenantId), 'datasets'],
    queryFn: () => unwrap(client.GET('/v1/admin/tenants/{tenant_id}/datasets', { params: { path: { tenant_id: tenantId } } })),
  });
  if (datasets.error) return <ErrorAlert error={datasets.error} />;
  if (datasets.data?.length === 0) return <Empty title="No datasets" />;
  return (
    <Rows
      headers={['Name', 'Project', 'Last import', 'Created']}
      items={datasets.data ?? []}
      rowKey={(d) => d.id}
      cells={(d) => [d.name, d.project_name, d.import_status ? <StatusBadge key="s" status={d.import_status} /> : '-', when(d.created_at)]}
    />
  );
}
