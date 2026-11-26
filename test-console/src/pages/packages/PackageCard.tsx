import { useState, type ReactNode } from 'react';
import { Badge, Button, Card, Code, Heading, HStack, Stack, Text } from '@chakra-ui/react';
import type { Pkg, Target, TaskSlug } from '@/api/packages';
import { PackageStatus, SmokeStatus } from '@/constants';
import { CardGrid, Empty, Facts, StatusBadge, when } from '@/ui';

/** What an operator can do to a package from its card. Each one opens the typed confirmation, except a smoke retry. */
export type Action = 'promote' | 'retire' | 'rollback';

const SHA_SHOWN = 12;
const SHORT_SHA = 8;

/** One package (runtime or task bundle) as a card: its state and smoke result, the facts a reviewer needs, and the
 * actions that are allowed for that state. The active package has none: retiring the stable one is refused by the API. */
export function PackageCard({
  pkg,
  target,
  hero,
  retrying,
  onAction,
  onRetry,
}: {
  pkg: Pkg;
  target: Target;
  hero?: boolean;
  retrying: boolean;
  onAction: (action: Action) => void;
  onRetry: () => void;
}) {
  const [showRequirements, setShowRequirements] = useState(false);
  const [showManifest, setShowManifest] = useState(false);
  const requirements = pkg.requirements ?? [];
  const candidate = pkg.status === PackageStatus.CANDIDATE;
  const retired = pkg.status === PackageStatus.RETIRED;
  const passed = pkg.smoke_status === SmokeStatus.PASSED;
  const failed = pkg.smoke_status === SmokeStatus.FAILED;

  return (
    <Card.Root
      variant="outline"
      borderColor={hero ? 'green.emphasized' : undefined}
      bg={hero ? 'green.subtle' : undefined}
      opacity={retired ? 0.8 : 1}
    >
      <Card.Header>
        <HStack justify="space-between" wrap="wrap" gap="2">
          <HStack>
            <Code fontSize="sm" title={pkg.sha256}>
              {pkg.sha256.slice(0, SHA_SHOWN)}
            </Code>
            {hero && <Badge colorPalette="green">Active</Badge>}
          </HStack>
          <StatusBadge status={pkg.status} />
        </HStack>
      </Card.Header>
      <Card.Body gap="3">
        <HStack wrap="wrap" gap="2">
          <Text fontSize="sm" color="fg.muted">
            Smoke
          </Text>
          <StatusBadge status={pkg.smoke_status} />
          {pkg.smoke_job_id && (
            <Code fontSize="xs" wordBreak="break-all">
              {pkg.smoke_job_id}
            </Code>
          )}
        </HStack>
        <Facts
          items={[
            ['Published by', pkg.published_by],
            [hero ? 'Active since' : 'Updated', `${when(pkg.updated_at)} by ${pkg.updated_by}`],
          ]}
        />
        <Stack gap="2">
          <Button size="xs" variant="ghost" alignSelf="start" onClick={() => setShowRequirements((v) => !v)}>
            {requirements.length ? `${requirements.length} pinned requirement${requirements.length === 1 ? '' : 's'}` : "The image's own packages"}
          </Button>
          {showRequirements && requirements.length > 0 && (
            <Code display="block" whiteSpace="pre-wrap" fontSize="xs" p="2">
              {requirements.join('\n')}
            </Code>
          )}
        </Stack>
        {target.slug && pkg.manifest && (
          <Stack gap="2">
            <Button size="xs" variant="ghost" alignSelf="start" onClick={() => setShowManifest((v) => !v)}>
              {showManifest ? 'Hide manifest' : 'Show manifest'}
            </Button>
            {showManifest && (
              <Code display="block" whiteSpace="pre" fontSize="xs" p="2" maxH="xs" overflow="auto">
                {JSON.stringify(pkg.manifest, null, 2)}
              </Code>
            )}
          </Stack>
        )}
      </Card.Body>
      {!hero && (
        <Card.Footer justifyContent="flex-end" flexWrap="wrap" gap="2">
          {candidate && failed && (
            <Button size="xs" variant="outline" loading={retrying} onClick={onRetry}>
              Retry smoke
            </Button>
          )}
          {candidate && passed && (
            <Button size="xs" colorPalette="green" onClick={() => onAction('promote')}>
              Promote
            </Button>
          )}
          {retired && passed && (
            <Button size="xs" colorPalette="green" variant="outline" onClick={() => onAction('rollback')}>
              Roll back to this
            </Button>
          )}
          {candidate && (
            <Button size="xs" colorPalette="red" variant="outline" onClick={() => onAction('retire')}>
              Retire
            </Button>
          )}
        </Card.Footer>
      )}
    </Card.Root>
  );
}

/** One name's packages: the active one first, its candidates beside it, the retired ones folded away. `slug` is set for
 * a task's bundles, which act through the task routes. */
export function PackageGroup({
  title,
  description,
  packages,
  slug,
  headerAction,
  retryingSha,
  onAction,
  onRetry,
}: {
  title: string;
  description: string;
  packages: Pkg[];
  slug?: TaskSlug;
  headerAction?: ReactNode;
  retryingSha?: string;
  onAction: (target: Target, action: Action, pkg: Pkg, replaces?: Pkg) => void;
  onRetry: (target: Target) => void;
}) {
  const [showRetired, setShowRetired] = useState(false);
  const active = packages.find((p) => p.status === PackageStatus.STABLE);
  const candidates = packages.filter((p) => p.status === PackageStatus.CANDIDATE);
  const retired = packages.filter((p) => p.status === PackageStatus.RETIRED);
  const targetOf = (p: Pkg): Target => ({ name: p.name, sha256: p.sha256, slug });
  const card = (p: Pkg, hero?: boolean) => (
    <PackageCard
      key={p.sha256}
      pkg={p}
      target={targetOf(p)}
      hero={hero}
      retrying={retryingSha === p.sha256}
      onAction={(a) => onAction(targetOf(p), a, p, active)}
      onRetry={() => onRetry(targetOf(p))}
    />
  );

  return (
    <Stack gap="3">
      <HStack justify="space-between" wrap="wrap" gap="2">
        <Stack gap="0">
          <Heading size="md">{title}</Heading>
          <Text fontSize="sm" color="fg.muted">
            {description}
          </Text>
        </Stack>
        <HStack>
          {active ? <Badge colorPalette="green">active {active.sha256.slice(0, SHORT_SHA)}</Badge> : <Badge colorPalette="orange">none active</Badge>}
          {headerAction}
        </HStack>
      </HStack>
      {packages.length === 0 && <Empty title="Nothing registered yet" />}
      <CardGrid min="20rem">
        {active && card(active, true)}
        {candidates.map((p) => card(p))}
      </CardGrid>
      {retired.length > 0 && (
        <>
          <Button size="xs" variant="ghost" alignSelf="start" onClick={() => setShowRetired((v) => !v)}>
            {showRetired ? 'Hide' : 'Show'} {retired.length} retired
          </Button>
          {showRetired && <CardGrid min="20rem">{retired.map((p) => card(p))}</CardGrid>}
        </>
      )}
    </Stack>
  );
}
