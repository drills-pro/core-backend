import { Box, HStack, VStack, Table, Text } from '@chakra-ui/react'
import type { TrainingRun } from '../lib/api'

export function EpochChart({ history }: { history: { epoch: number; loss?: number; accuracy?: number }[] }) {
  const w = 480
  const h = 160
  const pad = 28
  const epochs = history.map((h) => h.epoch)
  const minE = Math.min(...epochs)
  const maxE = Math.max(...epochs)
  const x = (e: number) => pad + ((e - minE) / Math.max(maxE - minE, 1)) * (w - pad * 2)

  const series: { key: 'loss' | 'accuracy'; color: string; points: { epoch: number; v: number }[] }[] = (
    ['loss', 'accuracy'] as const
  )
    .map((key) => ({
      key,
      color: key === 'loss' ? '#e53e3e' : '#3182ce',
      points: history.filter((p) => typeof p[key] === 'number').map((p) => ({ epoch: p.epoch, v: p[key] as number })),
    }))
    .filter((s) => s.points.length > 0)

  return (
    <VStack align="stretch" gap={1}>
      <HStack gap={4} fontSize="xs">
        {series.map((s) => (
          <HStack key={s.key} gap={1}>
            <Box w="10px" h="10px" bg={s.color} borderRadius="full" />
            <Text>{s.key}</Text>
          </HStack>
        ))}
      </HStack>
      <svg width={w} height={h} role="img" aria-label="training curves">
        {series.map((s) => {
          const maxV = Math.max(...s.points.map((p) => p.v), 0.0001)
          const y = (v: number) => h - pad - (v / maxV) * (h - pad * 2)
          const path = s.points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(p.epoch)} ${y(p.v)}`).join(' ')
          return <path key={s.key} d={path} fill="none" stroke={s.color} strokeWidth={2} />
        })}
        <line x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} stroke="#CBD5E0" />
        <text x={pad} y={h - 8} fontSize="10" fill="#718096">epoch {minE}</text>
        <text x={w - pad - 40} y={h - 8} fontSize="10" fill="#718096">epoch {maxE}</text>
      </svg>
    </VStack>
  )
}

export function ConfusionMatrixTable({ labels, matrix }: { labels: string[]; matrix: number[][] }) {
  const max = Math.max(...matrix.flat(), 1)
  return (
    <Box overflowX="auto">
      <Table.Root size="sm" variant="outline">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>actual \ predicted</Table.ColumnHeader>
            {labels.map((l) => (
              <Table.ColumnHeader key={l}>{l}</Table.ColumnHeader>
            ))}
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {matrix.map((row, i) => (
            <Table.Row key={i}>
              <Table.Cell fontWeight="medium">{labels[i]}</Table.Cell>
              {row.map((v, j) => (
                <Table.Cell key={j} bg={`rgba(49, 130, 206, ${0.08 + 0.5 * (v / max)})`} textAlign="center">
                  {v}
                </Table.Cell>
              ))}
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
    </Box>
  )
}

export function RunMetricsDetail({ metrics }: { metrics: NonNullable<TrainingRun['metrics']> }) {
  return (
    <HStack align="start" gap={8} py={2} wrap="wrap">
      {metrics.epoch_history && metrics.epoch_history.length > 0 && (
        <Box>
          <Text fontSize="xs" fontWeight="medium" mb={1}>Loss / accuracy per epoch</Text>
          <EpochChart history={metrics.epoch_history} />
        </Box>
      )}
      {metrics.confusion_matrix && (
        <Box>
          <Text fontSize="xs" fontWeight="medium" mb={1}>Confusion matrix (test set)</Text>
          <ConfusionMatrixTable labels={metrics.confusion_matrix.labels} matrix={metrics.confusion_matrix.matrix} />
        </Box>
      )}
    </HStack>
  )
}
