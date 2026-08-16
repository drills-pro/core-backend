import { createFileRoute, Outlet } from '@tanstack/react-router'

// Pure layout: TanStack's file-based router auto-nests every
// `p.$projectId.*` file under this one (matching dotted filenames become
// parent/child regardless of whether the parent renders anything), so this
// file must render an <Outlet/> or none of the child tab pages
// (datasets/training/models/deployments/settings/members/usage) ever mount.
// The actual overview content lives in p.$projectId.index.tsx.
export const Route = createFileRoute('/p/$projectId')({
  component: () => <Outlet />,
})
