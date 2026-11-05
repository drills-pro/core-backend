import { Code, Link, Spinner, Stack, Table, Text } from '@chakra-ui/react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { Link as RouterLink } from '@tanstack/react-router';
import { meQuery, tenantQuery } from '@/api/queries';
import { cfg } from '@/config';
import { INTERNAL_PLAN_TIER } from '@/constants';
import { ErrorAlert, Section, short, when } from '@/ui';

export function TenantsPage() {
  const me = useQuery(meQuery);
  const details = useQueries({ queries: (me.data?.tenants ?? []).map((t) => tenantQuery(t.id)) });
  const internal = details.flatMap((d) => (d.data?.plan_tier === INTERNAL_PLAN_TIER ? [d.data] : []));
  const hidden = details.filter((d) => d.data && d.data.plan_tier !== INTERNAL_PLAN_TIER).length;
  const loading = me.isPending || details.some((d) => d.isPending);

  return (
    <Stack gap="4">
      <Section title="Internal test tenants">
        <ErrorAlert error={me.error ?? details.find((d) => d.error)?.error} />
        {loading ? (
          <Spinner />
        ) : (
          <Table.Root size="sm">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>Name</Table.ColumnHeader>
                <Table.ColumnHeader>Id</Table.ColumnHeader>
                <Table.ColumnHeader>Role</Table.ColumnHeader>
                <Table.ColumnHeader>Created</Table.ColumnHeader>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {internal.map((t) => (
                <Table.Row key={t.id}>
                  <Table.Cell>
                    <Link asChild colorPalette="orange">
                      <RouterLink to="/t/$tenantId" params={{ tenantId: t.id }}>
                        {t.name}
                      </RouterLink>
                    </Link>
                  </Table.Cell>
                  <Table.Cell>
                    <Code>{short(t.id)}</Code>
                  </Table.Cell>
                  <Table.Cell>{t.role}</Table.Cell>
                  <Table.Cell>{when(t.created_at)}</Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table.Root>
        )}
        {hidden > 0 && <Text color="fg.muted">{hidden} customer tenant(s) you belong to are hidden.</Text>}
        {!loading && internal.length === 0 && (
          <Stack gap="1">
            <Text>You are in no internal tenant. A platform admin creates one (and becomes its owner) with:</Text>
            <Code display="block" whiteSpace="pre-wrap" p="2">
              {`curl -X POST ${cfg.apiUrl}/v1/admin/tenants -H "Authorization: Bearer $ACCESS" \\\n  -H 'Content-Type: application/json' -d '{"name": "QA", "plan_tier": "${INTERNAL_PLAN_TIER}"}'`}
            </Code>
            <Text color="fg.muted">Other staff join through the tenant's invitations.</Text>
          </Stack>
        )}
      </Section>
    </Stack>
  );
}
