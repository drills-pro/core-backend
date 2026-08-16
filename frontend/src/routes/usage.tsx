import { createFileRoute } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Heading, Table, VStack, Text, Box } from '@chakra-ui/react'
import { api } from '../lib/api'

export const Route = createFileRoute('/usage')({
  component: UsagePage,
})

const ILLUSTRATIVE_RATES: Record<string, number> = { gpu_seconds: 0.05, gb_days: 0.01 }

function UsagePage() {
  const { data: rows } = useQuery({ queryKey: ['usage'], queryFn: api.orgUsage })

  return (
    <VStack align="stretch" gap={4}>
      <Heading size="lg">Usage &amp; cost</Heading>
      <Text color="gray.600" fontSize="sm">
        Reads directly from <code>usage_events</code> (req/09's billing source of truth).
        Rate shown is illustrative only — req/09 explicitly defers the real pricing model.
      </Text>
      <Box bg="white" borderWidth={1} borderRadius="md">
        <Table.Root>
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>Resource</Table.ColumnHeader>
              <Table.ColumnHeader>Unit</Table.ColumnHeader>
              <Table.ColumnHeader>Quantity</Table.ColumnHeader>
              <Table.ColumnHeader>Est. cost*</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {rows?.map((r, i) => (
              <Table.Row key={i}>
                <Table.Cell>{r.resource_type}</Table.Cell>
                <Table.Cell>{r.unit}</Table.Cell>
                <Table.Cell>{r.total_quantity}</Table.Cell>
                <Table.Cell>${((ILLUSTRATIVE_RATES[r.unit] ?? 0) * r.total_quantity).toFixed(4)}</Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
        {rows?.length === 0 && <Text p={6} color="gray.500">No usage yet.</Text>}
      </Box>
    </VStack>
  )
}
