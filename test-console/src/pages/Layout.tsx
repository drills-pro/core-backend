import { Badge, Box, Button, Container, HStack, Link, Text } from '@chakra-ui/react';
import { Link as RouterLink, Outlet } from '@tanstack/react-router';
import { signOut } from '@/auth';
import { cfg } from '@/config';

/** The console's own (non-tenant) screens: one entry per admin route. */
const NAV = [
  { to: '/', label: 'Tenants' },
  { to: '/packages', label: 'Packages' },
  { to: '/model-catalog', label: 'Model catalog' },
  { to: '/task-catalog', label: 'Task catalog' },
] as const;

export function Layout() {
  return (
    <Box minH="100vh" bg="bg.subtle">
      <Box bg="orange.600" color="white" px="4" py="2">
        <HStack justify="space-between" wrap="wrap" gap="2">
          <HStack gap="4">
            <Text fontWeight="bold">drills.pro test console</Text>
            <Badge colorPalette="red" variant="solid">
              {new URL(cfg.apiUrl).host}
            </Badge>
            {NAV.map((n) => (
              <Link key={n.to} asChild color="white">
                <RouterLink to={n.to} activeOptions={{ exact: n.to === '/' }} activeProps={{ style: { fontWeight: 700, textDecoration: 'underline' } }}>
                  {n.label}
                </RouterLink>
              </Link>
            ))}
          </HStack>
          <Button size="xs" variant="subtle" onClick={() => void signOut()}>
            Sign out
          </Button>
        </HStack>
      </Box>
      <Container maxW="6xl" py="6">
        <Outlet />
      </Container>
    </Box>
  );
}
