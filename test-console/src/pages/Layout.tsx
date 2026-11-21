import { Badge, Box, Button, Flex, HStack, Separator, Stack, Text } from '@chakra-ui/react';
import { Link as RouterLink, Outlet } from '@tanstack/react-router';
import { signOut } from '@/auth';
import { cfg } from '@/config';

/** The console's own (non-tenant) screens, grouped: one entry per admin route. */
const NAV = [
  { group: 'Workspace', items: [{ to: '/', label: 'Tenants', hint: 'Internal test tenants' }] },
  {
    group: 'Platform',
    items: [
      { to: '/packages', label: 'Packages', hint: 'Runtime code and task bundles' },
      { to: '/task-catalog', label: 'Task catalog', hint: 'What tenants can train' },
      { to: '/model-catalog', label: 'Model catalog', hint: 'Base models' },
    ],
  },
] as const;

const SIDEBAR_WIDTH = '15rem';

function NavLink({ to, label, hint }: { to: string; label: string; hint: string }) {
  return (
    <RouterLink to={to} activeOptions={{ exact: to === '/' }}>
      {({ isActive }: { isActive: boolean }) => (
        <Box
          px="3"
          py="2"
          rounded="md"
          bg={isActive ? 'orange.subtle' : undefined}
          color={isActive ? 'orange.fg' : 'fg'}
          _hover={{ bg: isActive ? 'orange.subtle' : 'bg.muted' }}
          aria-current={isActive ? 'page' : undefined}
        >
          <Text fontWeight={isActive ? 'semibold' : 'medium'} fontSize="sm">
            {label}
          </Text>
          <Text fontSize="xs" color="fg.muted" display={{ base: 'none', md: 'block' }}>
            {hint}
          </Text>
        </Box>
      )}
    </RouterLink>
  );
}

export function Layout() {
  return (
    <Flex minH="100vh" direction={{ base: 'column', md: 'row' }} bg="bg.subtle">
      <Flex
        as="nav"
        direction="column"
        w={{ base: 'full', md: SIDEBAR_WIDTH }}
        flexShrink={0}
        bg="bg"
        borderRightWidth={{ md: '1px' }}
        borderBottomWidth={{ base: '1px', md: '0' }}
        p="4"
        gap="4"
        position={{ md: 'sticky' }}
        top="0"
        h={{ md: '100vh' }}
      >
        <Stack gap="1">
          <Text fontWeight="bold" fontSize="lg" color="orange.fg">
            drills.pro
          </Text>
          <Text fontSize="xs" color="fg.muted">
            Admin console
          </Text>
        </Stack>
        <Stack gap="4" direction={{ base: 'row', md: 'column' }} wrap="wrap">
          {NAV.map((g) => (
            <Stack key={g.group} gap="1">
              <Text fontSize="xs" fontWeight="semibold" color="fg.muted" textTransform="uppercase" display={{ base: 'none', md: 'block' }}>
                {g.group}
              </Text>
              <Stack gap="0.5" direction={{ base: 'row', md: 'column' }}>
                {g.items.map((n) => (
                  <NavLink key={n.to} {...n} />
                ))}
              </Stack>
            </Stack>
          ))}
        </Stack>
        <Box flex="1" display={{ base: 'none', md: 'block' }} />
        <Separator display={{ base: 'none', md: 'block' }} />
        <HStack justify="space-between" wrap="wrap">
          <Badge colorPalette="red" variant="subtle" title="The API this console talks to">
            {new URL(cfg.apiUrl).host}
          </Badge>
          <Button size="xs" variant="outline" onClick={() => void signOut()}>
            Sign out
          </Button>
        </HStack>
      </Flex>
      <Box flex="1" minW="0" px={{ base: '4', md: '8' }} py="6">
        <Box maxW="6xl" mx="auto">
          <Outlet />
        </Box>
      </Box>
    </Flex>
  );
}
