import type { ReactNode } from 'react';
import { Alert, Badge, Card, Heading, HStack, NativeSelect } from '@chakra-ui/react';
import { statusColor } from '@/constants';

export const StatusBadge = ({ status }: { status: string }) => <Badge colorPalette={statusColor(status)}>{status}</Badge>;

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

export function Section({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <Card.Root size="sm">
      <Card.Header>
        <HStack justify="space-between">
          <Heading size="md">{title}</Heading>
          {actions}
        </HStack>
      </Card.Header>
      <Card.Body gap="3">{children}</Card.Body>
    </Card.Root>
  );
}

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
    <NativeSelect.Root size="sm">
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
