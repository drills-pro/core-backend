import { Badge, Card, Code, HStack, LinkBox, LinkOverlay, Stack, Text } from '@chakra-ui/react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { Link as RouterLink } from '@tanstack/react-router';
import { meQuery, tenantQuery } from '@/api/queries';
import { cfg } from '@/config';
import { INTERNAL_PLAN_TIER } from '@/constants';
import { CardGrid, CardSkeletons, Empty, ErrorAlert, Facts, Id, PageHeader, when } from '@/ui';

export function TenantsPage() {
  const me = useQuery(meQuery);
  const details = useQueries({ queries: (me.data?.tenants ?? []).map((t) => tenantQuery(t.id)) });
  const internal = details.flatMap((d) => (d.data?.plan_tier === INTERNAL_PLAN_TIER ? [d.data] : []));
  const hidden = details.filter((d) => d.data && d.data.plan_tier !== INTERNAL_PLAN_TIER).length;
  const loading = me.isPending || details.some((d) => d.isPending);

  return (
    <Stack gap="4">
      <PageHeader title="Internal test tenants" description="The console only acts inside internal tenants, so a staff account can never train or deploy in a customer's by mistake." />
      <ErrorAlert error={me.error ?? details.find((d) => d.error)?.error} />
      {loading ? (
        <CardSkeletons />
      ) : (
        <CardGrid>
          {internal.map((t) => (
            <LinkBox key={t.id} asChild>
              <Card.Root variant="outline" _hover={{ borderColor: 'orange.emphasized', shadow: 'sm' }}>
                <Card.Header>
                  <HStack justify="space-between">
                    <Card.Title>
                      <LinkOverlay asChild>
                        <RouterLink to="/t/$tenantId" params={{ tenantId: t.id }}>
                          {t.name}
                        </RouterLink>
                      </LinkOverlay>
                    </Card.Title>
                    <Badge colorPalette="orange">{t.role}</Badge>
                  </HStack>
                </Card.Header>
                <Card.Body>
                  <Facts
                    items={[
                      ['Id', <Id key="id" value={t.id} />],
                      ['Created', when(t.created_at)],
                    ]}
                  />
                </Card.Body>
              </Card.Root>
            </LinkBox>
          ))}
        </CardGrid>
      )}
      {hidden > 0 && <Text color="fg.muted">{hidden} customer tenant(s) you belong to are hidden.</Text>}
      {!loading && internal.length === 0 && (
        <Empty title="You are in no internal tenant">
          <Stack gap="2" textAlign="start">
            <Text>A platform admin creates one (and becomes its owner) with:</Text>
            <Code display="block" whiteSpace="pre-wrap" p="2">
              {`curl -X POST ${cfg.apiUrl}/v1/admin/tenants -H "Authorization: Bearer $ACCESS" \\\n  -H 'Content-Type: application/json' -d '{"name": "QA", "plan_tier": "${INTERNAL_PLAN_TIER}"}'`}
            </Code>
            <Text color="fg.muted">Other staff join through the tenant's invitations.</Text>
          </Stack>
        </Empty>
      )}
    </Stack>
  );
}
