import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { Box, Button, Heading, Input, Stack, Text, VStack, Alert, Link as ChakraLink } from '@chakra-ui/react'
import { login, register } from '../lib/auth'

export const Route = createFileRoute('/login')({
  component: LoginPage,
})

function LoginPage() {
  const navigate = useNavigate()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      if (mode === 'register') {
        await register(email, password, firstName, lastName)
      }
      await login(email, password)
      navigate({ to: '/' })
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Box display="flex" alignItems="center" justifyContent="center" minH="80vh">
      <VStack gap={6} maxW="380px" w="100%" textAlign="center">
        <Heading size="lg">{mode === 'login' ? 'Sign in' : 'Create account'}</Heading>
        <Text color="gray.600" fontSize="sm">
          Real accounts, checked directly against this platform's identity
          provider — no redirect to another site.
        </Text>

        {error && (
          <Alert.Root status="error" w="100%">
            <Alert.Indicator />
            <Alert.Description>{error}</Alert.Description>
          </Alert.Root>
        )}

        <Stack as="form" onSubmit={onSubmit} gap={3} w="100%">
          {mode === 'register' && (
            <Stack direction="row" gap={3}>
              <Input placeholder="First name" value={firstName} onChange={(e) => setFirstName(e.target.value)} required />
              <Input placeholder="Last name" value={lastName} onChange={(e) => setLastName(e.target.value)} required />
            </Stack>
          )}
          <Input type="email" placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          <Input type="password" placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          <Button type="submit" size="lg" colorPalette="blue" loading={busy}>
            {mode === 'login' ? 'Sign in' : 'Create account'}
          </Button>
        </Stack>

        <Text fontSize="sm" color="gray.500">
          {mode === 'login' ? (
            <>
              No account?{' '}
              <ChakraLink onClick={() => setMode('register')} cursor="pointer" color="blue.600">
                Sign up
              </ChakraLink>
            </>
          ) : (
            <>
              Already have an account?{' '}
              <ChakraLink onClick={() => setMode('login')} cursor="pointer" color="blue.600">
                Sign in
              </ChakraLink>
            </>
          )}
        </Text>
      </VStack>
    </Box>
  )
}
