import { createFileRoute } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import {
  VStack, HStack, Button, Input, Table, Badge, Text, Box, Code, NativeSelect, Image, SimpleGrid, Tabs, Switch, Heading,
} from '@chakra-ui/react'
import { api } from '../lib/api'
import { ProjectNav } from '../components/ProjectNav'

export const Route = createFileRoute('/p/$projectId/datasets/$datasetId')({
  component: DatasetDetailPage,
})

function DatasetDetailPage() {
  const { projectId, datasetId } = Route.useParams()
  const { data: files } = useQuery({
    queryKey: ['dataset-files', projectId, datasetId],
    queryFn: () => api.listDatasetFiles(projectId, datasetId),
  })

  return (
    <VStack align="stretch" gap={6}>
      <ProjectNav projectId={projectId} />
      <Heading size="md">Dataset detail</Heading>
      <Box borderWidth={1} borderRadius="md" p={4} bg="white">
        <Tabs.Root defaultValue="files" size="sm" variant="enclosed">
          <Tabs.List>
            <Tabs.Trigger value="files">Upload images</Tabs.Trigger>
            <Tabs.Trigger value="zip">Upload zip</Tabs.Trigger>
            <Tabs.Trigger value="yolo">Upload YOLO</Tabs.Trigger>
            <Tabs.Trigger value="coco">Upload COCO</Tabs.Trigger>
            <Tabs.Trigger value="tabular">Tabular (Ludwig)</Tabs.Trigger>
            <Tabs.Trigger value="hf">Import from Hugging Face</Tabs.Trigger>
          </Tabs.List>
          <Box pt={4}>
            <Tabs.Content value="files"><ImageUpload projectId={projectId} datasetId={datasetId} /></Tabs.Content>
            <Tabs.Content value="zip"><ZipUpload projectId={projectId} datasetId={datasetId} /></Tabs.Content>
            <Tabs.Content value="yolo"><YoloUpload projectId={projectId} datasetId={datasetId} /></Tabs.Content>
            <Tabs.Content value="coco"><CocoUpload projectId={projectId} datasetId={datasetId} /></Tabs.Content>
            <Tabs.Content value="tabular"><TabularUpload projectId={projectId} datasetId={datasetId} /></Tabs.Content>
            <Tabs.Content value="hf"><HfImport projectId={projectId} datasetId={datasetId} /></Tabs.Content>
          </Box>
        </Tabs.Root>
        <Text fontSize="xs" color="gray.500" mt={4} mb={1}>{files?.files.length ?? 0} file(s) in this dataset so far</Text>
        <DatasetVersions projectId={projectId} datasetId={datasetId} />
        <PreprocessPanel projectId={projectId} datasetId={datasetId} />
        <PublishPanel projectId={projectId} datasetId={datasetId} />
        <DatasetPreviewGallery projectId={projectId} datasetId={datasetId} />
        <TabularPreviewPanel projectId={projectId} datasetId={datasetId} />
      </Box>
    </VStack>
  )
}

function ImageUpload({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const queryClient = useQueryClient()
  const [label, setLabel] = useState('classA')
  const fileInput = useRef<HTMLInputElement>(null)

  const upload = useMutation({
    mutationFn: async (fileList: FileList) => {
      for (const file of Array.from(fileList)) {
        const { key, url } = await api.getUploadUrl(projectId, datasetId, file.name, label)
        await api.uploadFile(url, file)
        void key
      }
    },
    onSuccess: () => {
      if (fileInput.current) fileInput.current.value = ''
      queryClient.invalidateQueries({ queryKey: ['dataset-files', projectId, datasetId] })
      queryClient.invalidateQueries({ queryKey: ['dataset-preview', projectId, datasetId] })
    },
  })

  return (
    <VStack align="stretch" gap={2}>
      <Text fontSize="xs" color="gray.600">
        Each file lands under its class-label folder (Ultralytics/YOLO-classify layout).
      </Text>
      <HStack>
        <Input placeholder="class label (e.g. cat)" value={label} onChange={(e) => setLabel(e.target.value)} maxW="220px" size="sm" />
        <input ref={fileInput} type="file" multiple onChange={(e) => e.target.files && label && upload.mutate(e.target.files)} />
        {upload.isPending && <Text fontSize="sm" color="gray.500">uploading…</Text>}
      </HStack>
    </VStack>
  )
}

function ZipUpload({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadZip(projectId, datasetId, file),
    onSuccess: () => {
      if (fileInput.current) fileInput.current.value = ''
      queryClient.invalidateQueries({ queryKey: ['dataset-files', projectId, datasetId] })
      queryClient.invalidateQueries({ queryKey: ['dataset-preview', projectId, datasetId] })
    },
  })

  return (
    <VStack align="stretch" gap={2}>
      <Text fontSize="xs" color="gray.600">
        A .zip of `&lt;label&gt;/&lt;file&gt;` folders — each top-level folder becomes a class label.
      </Text>
      <input ref={fileInput} type="file" accept=".zip" onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
      {upload.isPending && <Text fontSize="sm" color="gray.500">uploading and extracting…</Text>}
      {upload.data && (
        <Text fontSize="sm" color="green.700">
          Imported {upload.data.uploaded} file(s) across {Object.keys(upload.data.labels).length} label(s)
          {upload.data.skipped > 0 && `, skipped ${upload.data.skipped} non-image entries`}.
        </Text>
      )}
      {upload.isError && <Text fontSize="sm" color="red.600">{String(upload.error)}</Text>}
    </VStack>
  )
}

function YoloUpload({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadYolo(projectId, datasetId, file),
    onSuccess: () => {
      if (fileInput.current) fileInput.current.value = ''
      queryClient.invalidateQueries({ queryKey: ['dataset-files', projectId, datasetId] })
    },
  })
  return (
    <VStack align="stretch" gap={2}>
      <Text fontSize="xs" color="gray.600">
        A .zip in YOLO layout — `images/&lt;file&gt;` + `labels/&lt;file&gt;.txt` bounding boxes, for object_detection projects.
      </Text>
      <input ref={fileInput} type="file" accept=".zip" onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
      {upload.isPending && <Text fontSize="sm" color="gray.500">uploading…</Text>}
      {upload.data && <Text fontSize="sm" color="green.700">{upload.data.images} images, {upload.data.labels} label files.</Text>}
      {upload.isError && <Text fontSize="sm" color="red.600">{String(upload.error)}</Text>}
    </VStack>
  )
}

function CocoUpload({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadCoco(projectId, datasetId, file),
    onSuccess: () => {
      if (fileInput.current) fileInput.current.value = ''
      queryClient.invalidateQueries({ queryKey: ['dataset-files', projectId, datasetId] })
    },
  })
  return (
    <VStack align="stretch" gap={2}>
      <Text fontSize="xs" color="gray.600">
        A .zip with a COCO `annotations.json` + image files — converted to YOLO-style boxes on upload.
      </Text>
      <input ref={fileInput} type="file" accept=".zip" onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
      {upload.isPending && <Text fontSize="sm" color="gray.500">uploading…</Text>}
      {upload.data && <Text fontSize="sm" color="green.700">{upload.data.images} images, {upload.data.categories} categories.</Text>}
      {upload.isError && <Text fontSize="sm" color="red.600">{String(upload.error)}</Text>}
    </VStack>
  )
}

function TabularUpload({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const upload = useMutation({
    mutationFn: async (file: File) => api.uploadTabular(projectId, datasetId, await file.text()),
    onSuccess: () => {
      if (fileInput.current) fileInput.current.value = ''
      queryClient.invalidateQueries({ queryKey: ['dataset-tabular', projectId, datasetId] })
    },
  })

  return (
    <VStack align="stretch" gap={2}>
      <Text fontSize="xs" color="gray.600">
        A CSV of feature columns + a label column, for the Ludwig backend. Upload referenced image files first via the "Upload images"/"Upload zip" tabs.
      </Text>
      <input ref={fileInput} type="file" accept=".csv" onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
      {upload.isPending && <Text fontSize="sm" color="gray.500">uploading…</Text>}
      {upload.data && <Text fontSize="sm" color="green.700">Uploaded {upload.data.rows} rows, columns: {upload.data.columns.join(', ')}</Text>}
      {upload.isError && <Text fontSize="sm" color="red.600">{String(upload.error)}</Text>}
    </VStack>
  )
}

function HfImport({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const queryClient = useQueryClient()
  const [hfDataset, setHfDataset] = useState('')
  const [kind, setKind] = useState<'images' | 'tabular'>('images')
  const [imageColumn, setImageColumn] = useState('image')
  const [labelColumn, setLabelColumn] = useState('label')
  const [maxRows, setMaxRows] = useState('200')

  const { data: imports } = useQuery({
    queryKey: ['dataset-imports', projectId, datasetId],
    queryFn: () => api.listDatasetImports(projectId, datasetId),
    refetchInterval: 4000,
  })

  const submit = useMutation({
    mutationFn: () =>
      api.importHfDataset(projectId, datasetId, {
        hf_dataset: hfDataset,
        kind,
        image_column: imageColumn,
        label_column: labelColumn,
        max_rows: Number(maxRows) || 200,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dataset-imports', projectId, datasetId] })
      queryClient.invalidateQueries({ queryKey: ['dataset-files', projectId, datasetId] })
      queryClient.invalidateQueries({ queryKey: ['dataset-preview', projectId, datasetId] })
      queryClient.invalidateQueries({ queryKey: ['dataset-tabular', projectId, datasetId] })
    },
  })

  return (
    <VStack align="stretch" gap={2}>
      <Text fontSize="xs" color="gray.600">
        Pulls a real dataset from the Hugging Face Hub — use the canonical `namespace/name` id (e.g. "ylecun/mnist").
      </Text>
      <HStack wrap="wrap" gap={2}>
        <Input placeholder="HF dataset (namespace/name)" value={hfDataset} onChange={(e) => setHfDataset(e.target.value)} maxW="260px" size="sm" />
        <NativeSelect.Root size="sm" maxW="160px">
          <NativeSelect.Field value={kind} onChange={(e) => setKind(e.target.value as 'images' | 'tabular')}>
            <option value="images">Images (folders)</option>
            <option value="tabular">Tabular (CSV)</option>
          </NativeSelect.Field>
          <NativeSelect.Indicator />
        </NativeSelect.Root>
        <Input placeholder="max rows" value={maxRows} onChange={(e) => setMaxRows(e.target.value)} maxW="100px" size="sm" />
      </HStack>
      {kind === 'images' && (
        <HStack gap={2}>
          <Input placeholder="image column" value={imageColumn} onChange={(e) => setImageColumn(e.target.value)} maxW="160px" size="sm" />
          <Input placeholder="label column" value={labelColumn} onChange={(e) => setLabelColumn(e.target.value)} maxW="160px" size="sm" />
        </HStack>
      )}
      <Button size="sm" alignSelf="start" onClick={() => hfDataset && submit.mutate()} loading={submit.isPending}>
        Import
      </Button>
      {imports && imports.length > 0 && (
        <VStack align="stretch" gap={1} mt={2}>
          {imports.map((imp) => (
            <HStack key={imp.id} fontSize="xs">
              <Badge colorPalette={imp.status === 'succeeded' ? 'green' : imp.status === 'failed' ? 'red' : 'yellow'}>{imp.status}</Badge>
              <Code fontSize="xs">{imp.hf_dataset}</Code>
              <Text color="gray.500">({imp.kind})</Text>
              {imp.error && <Text color="red.600" title={imp.error} maxW="300px" truncate>{imp.error.split('\n').pop()}</Text>}
            </HStack>
          ))}
        </VStack>
      )}
    </VStack>
  )
}

function TabularPreviewPanel({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const { data: tabular } = useQuery({
    queryKey: ['dataset-tabular', projectId, datasetId],
    queryFn: () => api.getTabular(projectId, datasetId),
    retry: false,
  })
  if (!tabular || tabular.rows.length === 0) return null
  return (
    <Box mt={4} pt={4} borderTopWidth={1}>
      <Text fontWeight="medium" fontSize="sm" mb={2}>Tabular preview ({tabular.total_rows} rows, {tabular.columns.length} columns)</Text>
      <Box overflowX="auto">
        <Table.Root size="sm">
          <Table.Header>
            <Table.Row>{tabular.columns.map((c) => <Table.ColumnHeader key={c}>{c}</Table.ColumnHeader>)}</Table.Row>
          </Table.Header>
          <Table.Body>
            {tabular.rows.slice(0, 10).map((row, i) => (
              <Table.Row key={i}>{tabular.columns.map((c) => <Table.Cell key={c} fontSize="xs">{row[c]}</Table.Cell>)}</Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      </Box>
    </Box>
  )
}

function DatasetVersions({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')
  const { data } = useQuery({
    queryKey: ['dataset-versions', projectId, datasetId],
    queryFn: () => api.listDatasetVersions(projectId, datasetId),
    refetchInterval: 5000,
  })

  const commit = useMutation({
    mutationFn: () => api.commitDatasetVersion(projectId, datasetId, message || 'snapshot'),
    onSuccess: () => {
      setMessage('')
      queryClient.invalidateQueries({ queryKey: ['dataset-versions', projectId, datasetId] })
    },
  })

  return (
    <Box mt={4} pt={4} borderTopWidth={1}>
      <Text fontWeight="medium" fontSize="sm" mb={2}>Data versioning</Text>
      <HStack mb={3}>
        <Input placeholder="version message (e.g. 'added 200 more cat photos')" value={message} onChange={(e) => setMessage(e.target.value)} size="sm" maxW="360px" />
        <Button size="sm" onClick={() => commit.mutate()} loading={commit.isPending} disabled={!data?.has_uncommitted_changes}>
          Commit version
        </Button>
        {data && !data.has_uncommitted_changes && <Text fontSize="xs" color="gray.500">no changes since the last version</Text>}
        {data?.has_uncommitted_changes && <Badge colorPalette="yellow" size="sm">uncommitted changes</Badge>}
      </HStack>
      {data && data.versions.length > 1 && (
        <Table.Root size="sm">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>Version</Table.ColumnHeader>
              <Table.ColumnHeader>Message</Table.ColumnHeader>
              <Table.ColumnHeader>When</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {data.versions.filter((v) => v.message !== 'Repository created').map((v) => (
              <Table.Row key={v.id}>
                <Table.Cell><Code fontSize="xs">{v.id.slice(0, 10)}</Code></Table.Cell>
                <Table.Cell fontSize="sm">{v.message}</Table.Cell>
                <Table.Cell fontSize="xs" color="gray.500">{new Date(v.creation_date * 1000).toLocaleString()}</Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      )}
    </Box>
  )
}

function DatasetPreviewGallery({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const { data: preview } = useQuery({
    queryKey: ['dataset-preview', projectId, datasetId],
    queryFn: () => api.previewDataset(projectId, datasetId),
  })
  if (!preview || preview.total_images === 0) return null
  return (
    <Box mt={4} pt={4} borderTopWidth={1}>
      <HStack mb={2} wrap="wrap">
        <Text fontWeight="medium" fontSize="sm">Preview</Text>
        {Object.entries(preview.labels).map(([label, count]) => (
          <Badge key={label} variant="subtle">{label}: {count}</Badge>
        ))}
      </HStack>
      <SimpleGrid columns={{ base: 3, sm: 5, md: 8 }} gap={2}>
        {preview.items.map((item) => (
          <Box key={item.key} position="relative">
            <Image src={item.view_url} alt={item.label} borderRadius="sm" objectFit="cover" aspectRatio={1} w="100%" />
            <Text fontSize="2xs" color="white" position="absolute" bottom={0} left={0} bg="blackAlpha.700" px={1} borderRadius="sm">
              {item.label}
            </Text>
          </Box>
        ))}
      </SimpleGrid>
    </Box>
  )
}

// ponytail: Label Studio's server-side integration (create/export/import,
// backed by app/labeling.py + app/labelstudio_client.py) is intentionally
// not surfaced here -- decided against a separate app with its own
// login/RBAC that doesn't share this platform's auth or permissions.
// Backend code is left in place for a possible embedded-widget approach
// later (own auth, own task/annotation storage via LakeFS directly).

function PreprocessPanel({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const queryClient = useQueryClient()
  const [dedupe, setDedupe] = useState(false)
  const [classBalance, setClassBalance] = useState('')
  const [width, setWidth] = useState('')
  const [height, setHeight] = useState('')
  const { data: jobs } = useQuery({
    queryKey: ['preprocess-jobs', projectId, datasetId],
    queryFn: () => api.listPreprocessingJobs(projectId, datasetId),
    refetchInterval: 5000,
  })
  const submit = useMutation({
    mutationFn: () =>
      api.preprocessDataset(projectId, datasetId, {
        dedupe,
        class_balance: classBalance || null,
        resize: width && height ? { width: Number(width), height: Number(height) } : undefined,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['preprocess-jobs', projectId, datasetId] }),
  })

  return (
    <Box mt={4} pt={4} borderTopWidth={1}>
      <Text fontWeight="medium" fontSize="sm" mb={2}>Preprocessing</Text>
      <HStack wrap="wrap" gap={3} mb={2}>
        <Switch.Root checked={dedupe} onCheckedChange={(e) => setDedupe(e.checked)} size="sm">
          <Switch.HiddenInput />
          <Switch.Control />
          <Switch.Label>Dedupe</Switch.Label>
        </Switch.Root>
        <NativeSelect.Root size="sm" maxW="180px">
          <NativeSelect.Field value={classBalance} onChange={(e) => setClassBalance(e.target.value)}>
            <option value="">No class balancing</option>
            <option value="oversample">Oversample</option>
            <option value="undersample">Undersample</option>
          </NativeSelect.Field>
          <NativeSelect.Indicator />
        </NativeSelect.Root>
        <Input placeholder="resize width" value={width} onChange={(e) => setWidth(e.target.value)} maxW="120px" size="sm" />
        <Input placeholder="resize height" value={height} onChange={(e) => setHeight(e.target.value)} maxW="120px" size="sm" />
        <Button size="sm" onClick={() => submit.mutate()} loading={submit.isPending}>Run preprocessing</Button>
      </HStack>
      {jobs && jobs.length > 0 && (
        <VStack align="stretch" gap={1}>
          {jobs.map((j) => (
            <HStack key={j.id} fontSize="xs">
              <Badge colorPalette={j.status === 'succeeded' ? 'green' : j.status === 'failed' ? 'red' : 'yellow'}>{j.status}</Badge>
              <Text color="gray.500">{new Date(j.created_at).toLocaleString()}</Text>
              {j.error && <Text color="red.600" truncate maxW="300px">{j.error}</Text>}
            </HStack>
          ))}
        </VStack>
      )}
    </Box>
  )
}

function PublishPanel({ projectId, datasetId }: { projectId: string; datasetId: string }) {
  const queryClient = useQueryClient()
  const publish = useMutation({
    mutationFn: (published: boolean) => api.publishDataset(projectId, datasetId, published),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dataset-versions', projectId, datasetId] }),
  })
  return (
    <Box mt={4} pt={4} borderTopWidth={1}>
      <Text fontWeight="medium" fontSize="sm" mb={2}>Publish to Explore</Text>
      <HStack>
        <Button size="xs" onClick={() => publish.mutate(true)} loading={publish.isPending}>Publish current version</Button>
        <Button size="xs" variant="outline" onClick={() => publish.mutate(false)} loading={publish.isPending}>Unpublish</Button>
      </HStack>
    </Box>
  )
}
