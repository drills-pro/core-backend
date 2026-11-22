import { useEffect, useState } from 'react';
import { Alert, Button, Card, Center, Heading, Text } from '@chakra-ui/react';
import { useRouter, useSearch } from '@tanstack/react-router';
import { finishLogin, signIn } from '@/auth';
import { cfg } from '@/config';

export function LoginPage() {
  const returnTo = useSearch({ from: '/login' }).return_to ?? '/';
  return (
    <Center minH="100vh" p="4" bg="bg.subtle">
      <Card.Root w="full" maxW="md" variant="elevated">
        <Card.Body gap="4">
          <Heading size="xl">drills.pro admin console</Heading>
          <Text color="fg.muted">Staff only. Drives the pipeline on prod inside an internal test tenant.</Text>
          {!cfg.clientId && (
            <Alert.Root status="warning">
              <Alert.Indicator />
              <Alert.Title>VITE_ZITADEL_CLIENT_ID is not set (see .env.example)</Alert.Title>
            </Alert.Root>
          )}
          <Button colorPalette="orange" disabled={!cfg.clientId} onClick={() => signIn(returnTo)}>
            Sign in with Zitadel
          </Button>
        </Card.Body>
      </Card.Root>
    </Center>
  );
}

export function CallbackPage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    finishLogin()
      .then((u) => router.history.replace(typeof u?.state === 'string' && u.state.startsWith('/') ? u.state : '/'))
      .catch((e) => setError(String(e)));
  }, [router]);
  return <Center minH="100vh">{error ? <Text color="fg.error">Sign-in failed: {error}</Text> : <Text>Signing in…</Text>}</Center>;
}
