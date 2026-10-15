import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useEffect, useRef, useState } from 'react'
import { Box, Button, Heading, Input, Stack, Text, VStack, Alert, Link as ChakraLink } from '@chakra-ui/react'
import { completeLogin, signup, startLogin } from '../lib/auth'

export const Route = createFileRoute('/login')({
  component: LoginPage,
})

function LoginPage() {
  const navigate = useNavigate()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [orgName, setOrgName] = useState('')
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // One key per form session: a retry after a network error resumes the same signup.
  const signupKey = useRef(crypto.randomUUID())
  const handledCallback = useRef(false)

  // Zitadel redirects back here with ?code&state; finish the login.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const code = params.get('code')
    const state = params.get('state')
    if (!code || !state || handledCallback.current) return
    handledCallback.current = true
    setBusy(true)
    completeLogin(code, state)
      .then(() => navigate({ to: '/', replace: true }))
      .catch((err) => {
        setError(String(err instanceof Error ? err.message : err))
        window.history.replaceState({}, '', '/login')
        setBusy(false)
      })
  }, [navigate])

  async function onSignup(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await signup(orgName, email, password, firstName, lastName, signupKey.current)
      signupKey.current = crypto.randomUUID()
      await startLogin(email)
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
      setBusy(false)
    }
  }

  return (
    <Box display="flex" alignItems="center" justifyContent="center" minH="80vh">
      <VStack gap={6} maxW="380px" w="100%" textAlign="center">
        <Heading size="lg">{mode === 'login' ? 'Sign in' : 'Create organisation'}</Heading>

        {error && (
          <Alert.Root status="error" w="100%">
            <Alert.Indicator />
            <Alert.Description>{error}</Alert.Description>
          </Alert.Root>
        )}

        {mode === 'login' ? (
          <Button size="lg" colorPalette="blue" w="100%" loading={busy} onClick={() => startLogin().catch((err) => setError(String(err)))}>
            Continue to sign in
          </Button>
        ) : (
          <Stack as="form" onSubmit={onSignup} gap={3} w="100%">
            <Input placeholder="Organisation name" value={orgName} onChange={(e) => setOrgName(e.target.value)} required />
            <Stack direction="row" gap={3}>
              <Input placeholder="First name" value={firstName} onChange={(e) => setFirstName(e.target.value)} required />
              <Input placeholder="Last name" value={lastName} onChange={(e) => setLastName(e.target.value)} required />
            </Stack>
            <Input type="email" placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} required />
            <Input type="password" placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            <Button type="submit" size="lg" colorPalette="blue" loading={busy}>
              Create organisation
            </Button>
          </Stack>
        )}

        <Text fontSize="sm" color="gray.500">
          {mode === 'login' ? (
            <>
              New here?{' '}
              <ChakraLink onClick={() => setMode('register')} cursor="pointer" color="blue.600">
                Create an organisation
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
