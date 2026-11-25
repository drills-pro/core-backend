import { useState, type ReactNode } from 'react';
import { Alert, Badge, Button, Card, Code, HStack, Stack, Text } from '@chakra-ui/react';
import type { Pkg } from '@/api/packages';
import { PackageStatus, SmokeStatus } from '@/constants';
import { CardGrid, Facts, StatusBadge, when } from '@/ui';

const SHA_SHOWN = 12;
const sha = (p: Pkg) => p.sha256.slice(0, SHA_SHOWN);

type Handlers = {
  onRetry?: (p: Pkg) => void;
  retrying?: string;
  extra?: (p: Pkg) => ReactNode;
};

/** One package name (or task slug): its single active (stable) package as a hero card, candidates as cards beside it,
 * retired ones folded away. Read-only: CI moves the active package, so the only action here is retrying a failed smoke. */
export function PackageList({ packages, ...h }: { packages: Pkg[] } & Handlers) {
  const [showRetired, setShowRetired] = useState(false);
  const active = packages.find((p) => p.status === PackageStatus.STABLE);
  const candidates = packages.filter((p) => p.status === PackageStatus.CANDIDATE);
  const retired = packages.filter((p) => p.status === PackageStatus.RETIRED);

  return (
    <Stack gap="3">
      {active ? (
        <PackageCard pkg={active} hero {...h} />
      ) : (
        <Alert.Root status="warning" size="sm">
          <Alert.Indicator />
          <Alert.Title>No active package: a candidate becomes active once its smoke passed.</Alert.Title>
        </Alert.Root>
      )}
      {candidates.length > 0 && (
        <CardGrid min="22rem">
          {candidates.map((p) => (
            <PackageCard key={p.sha256} pkg={p} {...h} />
          ))}
        </CardGrid>
      )}
      {retired.length > 0 && (
        <>
          <Button size="xs" variant="ghost" alignSelf="start" onClick={() => setShowRetired((v) => !v)}>
            {showRetired ? 'Hide' : 'Show'} {retired.length} retired
          </Button>
          {showRetired && (
            <CardGrid min="22rem">
              {retired.map((p) => (
                <PackageCard key={p.sha256} pkg={p} {...h} />
              ))}
            </CardGrid>
          )}
        </>
      )}
      {packages.length === 0 && <Text color="fg.muted">Nothing registered yet.</Text>}
    </Stack>
  );
}

function PackageCard({ pkg: p, hero, onRetry, retrying, extra }: { pkg: Pkg; hero?: boolean } & Handlers) {
  const retired = p.status === PackageStatus.RETIRED;
  const smokeFailed = p.status === PackageStatus.CANDIDATE && p.smoke_status === SmokeStatus.FAILED;
  return (
    <Card.Root size="sm" variant="outline" borderColor={hero ? 'green.emphasized' : undefined} bg={hero ? 'green.subtle' : undefined} opacity={retired ? 0.75 : 1}>
      <Card.Header>
        <HStack justify="space-between">
          <HStack>
            {hero && (
              <Badge colorPalette="green" variant="solid">
                Active
              </Badge>
            )}
            <Code>{sha(p)}</Code>
          </HStack>
          <HStack>
            <StatusBadge status={p.smoke_status} />
            {!hero && <StatusBadge status={p.status} />}
          </HStack>
        </HStack>
      </Card.Header>
      <Card.Body gap="2">
        <Facts
          items={[
            ['Smoke job', p.smoke_job_id ? <Code key="j" fontSize="xs">{p.smoke_job_id}</Code> : '-'],
            ['Published by', p.published_by],
            [hero ? 'Active since' : 'Updated', `${when(p.updated_at)} by ${p.updated_by}`],
          ]}
        />
      </Card.Body>
      <Card.Footer justifyContent="flex-end" flexWrap="wrap">
        {extra?.(p)}
        {onRetry && smokeFailed && (
          <Button size="xs" variant="outline" loading={retrying === p.sha256} onClick={() => onRetry(p)}>
            Retry smoke
          </Button>
        )}
      </Card.Footer>
    </Card.Root>
  );
}
