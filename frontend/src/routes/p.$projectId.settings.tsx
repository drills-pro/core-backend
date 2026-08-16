import { createFileRoute } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { VStack, HStack, Button, Input, NativeSelect, Text, Box, Heading, Switch } from '@chakra-ui/react'
import { api } from '../lib/api'
import { ProjectNav } from '../components/ProjectNav'

export const Route = createFileRoute('/p/$projectId/settings')({
  component: ProjectSettingsPage,
})

function ProjectSettingsPage() {
  const { projectId } = Route.useParams()
  const queryClient = useQueryClient()
  const { data: project } = useQuery({ queryKey: ['project', projectId], queryFn: () => api.getProject(projectId) })
  const { data: datasets } = useQuery({ queryKey: ['datasets', projectId], queryFn: () => api.listDatasets(projectId) })

  const [computeTarget, setComputeTarget] = useState('cpu')
  const [baseModel, setBaseModel] = useState('')
  const [deployStrategy, setDeployStrategy] = useState('latest')
  const [visibility, setVisibility] = useState('private')
  const [publishMeta, setPublishMeta] = useState(false)
  const [defaultTestDataset, setDefaultTestDataset] = useState('')

  useEffect(() => {
    if (!project) return
    setComputeTarget(project.settings?.compute_target ?? 'cpu')
    setBaseModel(project.settings?.base_model ?? '')
    setDeployStrategy(project.settings?.deploy_strategy ?? 'latest')
    setVisibility(project.visibility ?? 'private')
    setPublishMeta(!!project.publish_project_metadata)
    setDefaultTestDataset(project.default_test_dataset_id ?? '')
  }, [project])

  const saveSettings = useMutation({
    mutationFn: () => api.updateProjectSettings(projectId, { compute_target: computeTarget, base_model: baseModel || undefined, deploy_strategy: deployStrategy }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['project', projectId] }),
  })
  const saveVisibility = useMutation({
    mutationFn: () => api.updateProjectVisibility(projectId, { visibility, publish_project_metadata: publishMeta }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['project', projectId] }),
  })
  const saveDefaultTestDataset = useMutation({
    mutationFn: () => api.updateProject(projectId, { default_test_dataset_id: defaultTestDataset }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['project', projectId] }),
  })

  return (
    <VStack align="stretch" gap={6}>
      <ProjectNav projectId={projectId} />

      <Box bg="white" borderWidth={1} borderRadius="md" p={5}>
        <Heading size="sm" mb={3}>Compute &amp; deployment</Heading>
        <VStack align="stretch" gap={3} maxW="400px">
          <Box>
            <Text fontSize="xs" color="gray.500" mb={1}>Compute target</Text>
            <NativeSelect.Root size="sm">
              <NativeSelect.Field value={computeTarget} onChange={(e) => setComputeTarget(e.target.value)}>
                <option value="cpu">CPU</option>
                <option value="gpu">GPU</option>
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Box>
          <Box>
            <Text fontSize="xs" color="gray.500" mb={1}>Base model (default for training runs)</Text>
            <Input size="sm" value={baseModel} onChange={(e) => setBaseModel(e.target.value)} placeholder="e.g. yolov8n" />
          </Box>
          <Box>
            <Text fontSize="xs" color="gray.500" mb={1}>Deploy strategy</Text>
            <NativeSelect.Root size="sm">
              <NativeSelect.Field value={deployStrategy} onChange={(e) => setDeployStrategy(e.target.value)}>
                <option value="latest">Latest (new deployment each time)</option>
                <option value="main">Main (single stable route, updated in place)</option>
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Box>
          <Button size="sm" alignSelf="start" onClick={() => saveSettings.mutate()} loading={saveSettings.isPending}>Save</Button>
        </VStack>
      </Box>

      <Box bg="white" borderWidth={1} borderRadius="md" p={5}>
        <Heading size="sm" mb={3}>Visibility</Heading>
        <VStack align="stretch" gap={3} maxW="400px">
          <NativeSelect.Root size="sm">
            <NativeSelect.Field value={visibility} onChange={(e) => setVisibility(e.target.value)}>
              <option value="private">Private</option>
              <option value="public">Public</option>
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
          <Switch.Root checked={publishMeta} onCheckedChange={(e) => setPublishMeta(e.checked)} size="sm">
            <Switch.HiddenInput />
            <Switch.Control />
            <Switch.Label>Publish project metadata to Explore</Switch.Label>
          </Switch.Root>
          <Button size="sm" alignSelf="start" onClick={() => saveVisibility.mutate()} loading={saveVisibility.isPending}>Save</Button>
        </VStack>
      </Box>

      <Box bg="white" borderWidth={1} borderRadius="md" p={5}>
        <Heading size="sm" mb={3}>Default test dataset</Heading>
        <HStack maxW="500px">
          <NativeSelect.Root size="sm" flex="1">
            <NativeSelect.Field value={defaultTestDataset} onChange={(e) => setDefaultTestDataset(e.target.value)}>
              <option value="">None</option>
              {datasets?.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
          <Button size="sm" onClick={() => saveDefaultTestDataset.mutate()} loading={saveDefaultTestDataset.isPending}>Save</Button>
        </HStack>
      </Box>
    </VStack>
  )
}
