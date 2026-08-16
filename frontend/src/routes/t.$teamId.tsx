import { createFileRoute, Outlet } from '@tanstack/react-router'

// Pure layout — see p.$projectId.tsx's comment for why: this file is the
// auto-nested parent of t.$teamId.settings.tsx and must render <Outlet/>.
// Dashboard content lives in t.$teamId.index.tsx.
export const Route = createFileRoute('/t/$teamId')({
  component: () => <Outlet />,
})
