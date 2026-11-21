import { useState, type ReactNode } from 'react';
import { Alert, Badge, Button, Code, HStack, Stack, Table, Text } from '@chakra-ui/react';
import { PackageStatus, SmokeStatus } from '@/constants';
import { StatusBadge, when } from '@/ui';
import type { Pkg } from '@/api/packages';

export type PackageAction = { pkg: Pkg; status: Pkg['status']; rollback?: boolean };

const RANK: Record<string, number> = { [PackageStatus.STABLE]: 0, [PackageStatus.CANDIDATE]: 1, [PackageStatus.RETIRED]: 2 };

/** One package name (or task slug): its single active (stable) package on top, candidates next, retired ones folded away.
 * Pure view: what a button does is the caller's (`onAction`, `onRetry`), so runtime packages and bundles share it. */
export function PackageTable({
  packages,
  onAction,
  onRetry,
  retrying,
  extra,
}: {
  packages: Pkg[];
  onAction: (a: PackageAction) => void;
  onRetry?: (p: Pkg) => void;
  retrying?: string;
  extra?: (p: Pkg) => ReactNode;
}) {
  const [showRetired, setShowRetired] = useState(false);
  const active = packages.find((p) => p.status === PackageStatus.STABLE);
  const retired = packages.filter((p) => p.status === PackageStatus.RETIRED);
  const rows = [...packages].sort((a, b) => RANK[a.status] - RANK[b.status]).filter((p) => showRetired || p.status !== PackageStatus.RETIRED);

  return (
    <Stack gap="3">
      {active ? (
        <HStack gap="2">
          <Badge colorPalette="green" variant="solid">
            Active
          </Badge>
          <Code>{active.sha256.slice(0, 12)}</Code>
          <Text color="fg.muted" fontSize="sm">
            since {when(active.updated_at)}
          </Text>
        </HStack>
      ) : (
        <Alert.Root status="warning" size="sm">
          <Alert.Indicator />
          <Alert.Title>No active package: promote a candidate whose smoke passed.</Alert.Title>
        </Alert.Root>
      )}
      <Table.Root size="sm">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>sha256</Table.ColumnHeader>
            <Table.ColumnHeader>Status</Table.ColumnHeader>
            <Table.ColumnHeader>Smoke</Table.ColumnHeader>
            <Table.ColumnHeader>Published by</Table.ColumnHeader>
            <Table.ColumnHeader>Updated</Table.ColumnHeader>
            <Table.ColumnHeader />
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {rows.map((p) => {
            const isActive = p === active;
            const canPromote = !isActive && p.smoke_status === SmokeStatus.PASSED;
            return (
              <Table.Row key={p.sha256} bg={isActive ? 'green.subtle' : undefined} opacity={p.status === PackageStatus.RETIRED ? 0.7 : 1}>
                <Table.Cell>
                  <Code>{p.sha256.slice(0, 12)}</Code>
                </Table.Cell>
                <Table.Cell>
                  <StatusBadge status={p.status} />
                </Table.Cell>
                <Table.Cell>
                  <StatusBadge status={p.smoke_status} />
                  {p.smoke_job_id && (
                    <Code ml="1" fontSize="xs">
                      {p.smoke_job_id}
                    </Code>
                  )}
                </Table.Cell>
                <Table.Cell>{p.published_by}</Table.Cell>
                <Table.Cell>
                  {when(p.updated_at)} by {p.updated_by}
                </Table.Cell>
                <Table.Cell>
                  <HStack justify="end">
                    {extra?.(p)}
                    {onRetry && p.status === PackageStatus.CANDIDATE && p.smoke_status === SmokeStatus.FAILED && (
                      <Button size="xs" variant="outline" loading={retrying === p.sha256} onClick={() => onRetry(p)}>
                        Retry smoke
                      </Button>
                    )}
                    {canPromote && (
                      <Button size="xs" colorPalette="green" onClick={() => onAction({ pkg: p, status: PackageStatus.STABLE, rollback: p.status === PackageStatus.RETIRED })}>
                        {p.status === PackageStatus.RETIRED ? 'Roll back to this' : 'Promote'}
                      </Button>
                    )}
                    {p.status === PackageStatus.CANDIDATE && (
                      <Button size="xs" variant="outline" colorPalette="red" onClick={() => onAction({ pkg: p, status: PackageStatus.RETIRED })}>
                        Retire
                      </Button>
                    )}
                  </HStack>
                </Table.Cell>
              </Table.Row>
            );
          })}
        </Table.Body>
      </Table.Root>
      {retired.length > 0 && (
        <Button size="xs" variant="ghost" alignSelf="start" onClick={() => setShowRetired((v) => !v)}>
          {showRetired ? 'Hide' : 'Show'} {retired.length} retired
        </Button>
      )}
      {packages.length === 0 && <Text color="fg.muted">Nothing registered yet.</Text>}
    </Stack>
  );
}
