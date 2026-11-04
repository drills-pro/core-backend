import { Badge, Box, Button, Container, HStack, Link, Text } from '@chakra-ui/react';
import { Link as RouterLink, Outlet } from '@tanstack/react-router';
import { signOut } from '@/auth';
import { cfg } from '@/config';

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
            <Link asChild color="white">
              <RouterLink to="/">Tenants</RouterLink>
            </Link>
            <Link asChild color="white">
              <RouterLink to="/packages">Job packages</RouterLink>
            </Link>
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
