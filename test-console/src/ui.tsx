import { type ReactNode, useState } from 'react';
import { Alert, Badge, Button, Card, Code, DataList, Dialog, EmptyState, Heading, HStack, NativeSelect, Portal, SimpleGrid, Skeleton, Stack, Text, VStack } from '@chakra-ui/react';
import { ApiError } from '@/api/client';
import { DeployStatus, statusColor } from '@/constants';

export const StatusBadge = ({ status }: { status: string }) => <Badge colorPalette={statusColor(status)} variant={status === DeployStatus.DELETED ? 'outline' : undefined}>{status}</Badge>;

/** Shows the API's own error text: this is a staff tool, the detail is the useful part. */
export function ErrorAlert({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <Alert.Root status="error" size="sm">
      <Alert.Indicator />
      <Alert.Title>{error instanceof Error ? error.message : String(error)}</Alert.Title>
    </Alert.Root>
  );
}

/** The message for the error's `code` when `messages` has one; otherwise the API's own text. */
export function CodedError({ error, messages }: { error: unknown; messages: Record<string, string> }) {
  const text = error instanceof ApiError && error.code ? messages[error.code] : undefined;
  if (!text) return <ErrorAlert error={error} />;
  return (
    <Alert.Root status="error" size="sm">
      <Alert.Indicator />
      <Alert.Title>{text}</Alert.Title>
    </Alert.Root>
  );
}

/** A titled card: the console's one container for a block of content. */
export function Section({ title, description, actions, children }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <Card.Root size="sm" variant="outline">
      <Card.Header>
        <HStack justify="space-between" align="start" wrap="wrap" gap="2">
          <Stack gap="0.5">
            <Heading size="md">{title}</Heading>
            {description && (
              <Text color="fg.muted" fontSize="sm">
                {description}
              </Text>
            )}
          </Stack>
          {actions}
        </HStack>
      </Card.Header>
      <Card.Body gap="3">{children}</Card.Body>
    </Card.Root>
  );
}

/** The top of a screen: what it is, what it is for, and its primary actions. */
export function PageHeader({ title, description, actions, back }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; back?: ReactNode }) {
  return (
    <Stack gap="1" mb="2">
      {back}
      <HStack justify="space-between" align="start" wrap="wrap" gap="3">
        <Stack gap="1" maxW="3xl">
          <Heading size="2xl">{title}</Heading>
          {description && <Text color="fg.muted">{description}</Text>}
        </Stack>
        {actions}
      </HStack>
    </Stack>
  );
}

const CARD_MIN_WIDTH = '20rem';

/** Cards flow into as many columns as fit. */
export const CardGrid = ({ children, min = CARD_MIN_WIDTH }: { children: ReactNode; min?: string }) => (
  <SimpleGrid minChildWidth={min} gap="4">
    {children}
  </SimpleGrid>
);

/** Label / value pairs inside a card. */
export function Facts({ items }: { items: [label: string, value: ReactNode][] }) {
  return (
    <DataList.Root size="sm" orientation="horizontal" gap="1">
      {items.map(([label, value]) => (
        <DataList.Item key={label}>
          <DataList.ItemLabel minW="24">{label}</DataList.ItemLabel>
          <DataList.ItemValue>{value}</DataList.ItemValue>
        </DataList.Item>
      ))}
    </DataList.Root>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <EmptyState.Root size="sm">
      <EmptyState.Content>
        <EmptyState.Title>{title}</EmptyState.Title>
        {children && <EmptyState.Description>{children}</EmptyState.Description>}
      </EmptyState.Content>
    </EmptyState.Root>
  );
}

const SKELETON_HEIGHT = '32';

/** Placeholder cards while a list loads. */
export const CardSkeletons = ({ count = 3 }: { count?: number }) => (
  <CardGrid>
    {Array.from({ length: count }, (_, i) => (
      <Skeleton key={i} height={SKELETON_HEIGHT} rounded="md" />
    ))}
  </CardGrid>
);

/** A modal yes/no, in place of window.confirm: says what will happen and is styled by how dangerous it is. */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger,
  loading,
  error,
  onConfirm,
  onClose,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  loading?: boolean;
  error?: unknown;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog.Root open role="alertdialog" onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>{title}</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <VStack align="stretch" gap="3">
                {children}
                <ErrorAlert error={error} />
              </VStack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button colorPalette={danger ? 'red' : 'orange'} loading={loading} onClick={onConfirm}>
                {confirmLabel}
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

/** A short id, monospace; the full value is the hover text. */
export const Id = ({ value }: { value: string }) => (
  <Code title={value} fontSize="xs">
    {short(value)}
  </Code>
);

const SELECT_MAX_WIDTH = '20rem';

export function Select({
  value,
  onChange,
  options,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  placeholder?: string;
}) {
  return (
    <NativeSelect.Root size="sm" w="full" maxW={SELECT_MAX_WIDTH}>
      <NativeSelect.Field value={value} onChange={(e) => onChange(e.currentTarget.value)} placeholder={placeholder}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </NativeSelect.Field>
      <NativeSelect.Indicator />
    </NativeSelect.Root>
  );
}

export const short = (id: string) => id.slice(0, 8);
export const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : '-');

const COPIED_MS = 1_500;

/** Copies `text` to the clipboard and says so for a moment. */
export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      size="xs"
      variant="outline"
      onClick={() =>
        navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), COPIED_MS);
        })
      }
    >
      {copied ? 'Copied' : 'Copy'}
    </Button>
  );
}
