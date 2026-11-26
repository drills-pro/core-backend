import { useState } from 'react';
import { Code, HStack, Input, SimpleGrid, Stack, Tabs } from '@chakra-ui/react';
import { useQuery } from '@tanstack/react-query';
import { adminKeys } from '@/api/admin';
import { client, unwrap, type Schemas } from '@/api/client';
import { usePagedList } from '@/api/paged';
import { ACTIVITY_DAYS, AUDIT_ENTITIES } from '@/constants';
import { Empty, ErrorAlert, Id, LoadMore, PageHeader, Rows, Select, Stat, when } from '@/ui';

/** The tenants' own trails, read across every tenant (Activity), and the writes made through /v1/admin (Audit log). */
export function ActivityPage() {
  return (
    <Stack gap="4">
      <PageHeader
        title="Activity"
        description="What happened across every tenant, newest first. Activity is the tenants' own audit trails; the admin audit log is the writes made through the platform admin API."
      />
      <Tabs.Root defaultValue="activity" variant="line" colorPalette="orange" lazyMount>
        <Tabs.List>
          <Tabs.Trigger value="activity">Activity</Tabs.Trigger>
          <Tabs.Trigger value="audit">Admin audit log</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content colorPalette="gray" value="activity">
          <ActivityTab />
        </Tabs.Content>
        <Tabs.Content colorPalette="gray" value="audit">
          <AuditTab />
        </Tabs.Content>
      </Tabs.Root>
    </Stack>
  );
}

function ActivityTab() {
  const [q, setQ] = useState('');
  const [entityType, setEntityType] = useState('');
  const [days, setDays] = useState('');
  const stats = useQuery({ queryKey: adminKeys.activityStats, queryFn: () => unwrap(client.GET('/v1/admin/activity/summary')) });
  const filters = { q: q.trim() || undefined, entity_type: entityType.trim() || undefined, days: days ? Number(days) : undefined };
  const list = usePagedList([...adminKeys.activity, filters], (page_token) =>
    unwrap(client.GET('/v1/admin/activity', { params: { query: { ...filters, page_token } } })),
  );

  return (
    <Stack gap="4">
      <ErrorAlert error={stats.error} />
      {stats.data && (
        <SimpleGrid minChildWidth="10rem" gap="3">
          <Stat label="Events today" value={stats.data.events_today} />
          <Stat label="Tenants active today" value={stats.data.tenants_today} />
        </SimpleGrid>
      )}
      <HStack wrap="wrap" gap="2">
        <Input size="sm" maxW="xs" placeholder="Search actor, action or entity" value={q} onChange={(e) => setQ(e.target.value)} />
        <Input size="sm" maxW="xs" placeholder="Entity type, e.g. project" value={entityType} onChange={(e) => setEntityType(e.target.value)} />
        <Select value={days} onChange={setDays} options={[{ value: '', label: 'Any time' }, ...ACTIVITY_DAYS.map((d) => ({ value: String(d), label: `Last ${d} day${d > 1 ? 's' : ''}` }))]} />
      </HStack>
      <ErrorAlert error={list.error} />
      {!list.isPending && list.items.length === 0 && <Empty title="Nothing matches" />}
      {list.items.length > 0 && (
        <Rows
          headers={['When', 'Who', 'Organisation', 'Project', 'Action', 'Entity']}
          items={list.items}
          rowKey={(a) => a.id}
          cells={(a) => [
            when(a.created_at),
            a.actor_name ?? a.actor,
            a.tenant_name ?? '-',
            a.project_name ?? '-',
            <Code key="a" fontSize="xs">
              {a.action}
            </Code>,
            a.entity_id ? (
              <HStack key="e" gap="1">
                <span>{a.entity_type}</span>
                <Id value={a.entity_id} />
              </HStack>
            ) : (
              a.entity_type
            ),
          ]}
        />
      )}
      <LoadMore hasMore={list.hasMore} loading={list.loadingMore} onClick={list.loadMore} />
    </Stack>
  );
}

function AuditTab() {
  const [entityType, setEntityType] = useState('');
  const [actor, setActor] = useState('');
  const filters = {
    entity_type: (entityType || undefined) as Schemas['AdminAuditEntity'] | undefined,
    actor: actor.trim() || undefined,
  };
  const list = usePagedList([...adminKeys.audit, filters], (page_token) =>
    unwrap(client.GET('/v1/admin/audit-log', { params: { query: { ...filters, page_token } } })),
  );

  return (
    <Stack gap="4">
      <HStack wrap="wrap" gap="2">
        <Select
          value={entityType}
          onChange={setEntityType}
          options={[{ value: '', label: 'Any entity' }, ...AUDIT_ENTITIES.map((e) => ({ value: e, label: e }))]}
        />
        <Input size="sm" maxW="xs" placeholder="Actor (user sub)" value={actor} onChange={(e) => setActor(e.target.value)} />
      </HStack>
      <ErrorAlert error={list.error} />
      {!list.isPending && list.items.length === 0 && <Empty title="No admin writes match" />}
      {list.items.length > 0 && (
        <Rows
          headers={['When', 'Actor', 'Action', 'Entity', 'Id', 'Detail']}
          items={list.items}
          rowKey={(e) => e.id}
          cells={(e) => [
            when(e.created_at),
            e.actor,
            <Code key="a" fontSize="xs">
              {e.action}
            </Code>,
            e.entity_type,
            <Id key="i" value={e.entity_id} />,
            <Code key="d" fontSize="xs">
              {JSON.stringify(e.detail)}
            </Code>,
          ]}
        />
      )}
      <LoadMore hasMore={list.hasMore} loading={list.loadingMore} onClick={list.loadMore} />
    </Stack>
  );
}
