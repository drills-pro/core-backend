import { createFileRoute, Outlet } from '@tanstack/react-router'

// Pure layout — see p.$projectId.tsx's comment: this file is the
// auto-nested parent of p.$projectId.datasets.$datasetId.tsx and must
// render <Outlet/>. The list page lives in p.$projectId.datasets.index.tsx.
export const Route = createFileRoute('/p/$projectId/datasets')({
  component: () => <Outlet />,
})
