import { useState } from 'react';
import { Button, Code, Dialog, Input, Portal, Stack, Text } from '@chakra-ui/react';
import { useMutation } from '@tanstack/react-query';
import type { Schemas } from '@/api/client';
import { PACKAGE_ERROR_MESSAGES, PackageStatus, SHA_CONFIRM_CHARS } from '@/constants';
import { CodedError } from '@/ui';

export type Change = { name: string; sha256: string; status: Schemas['PackageStatus']; rollback?: boolean };

const phrase = (c: Change) => `${c.name} ${c.sha256.slice(0, SHA_CONFIRM_CHARS)}`;

/** Typing the package name and the start of its sha back is the guard: a misclick cannot promote the wrong package.
 * `request` makes the status call; runtime packages and task bundles use different routes. */
export function ConfirmChange({ change, request, onClose, onDone }: { change: Change; request: () => Promise<unknown>; onClose: () => void; onDone: () => void }) {
  const [typed, setTyped] = useState('');
  const { name, status, rollback } = change;
  const save = useMutation({
    mutationFn: request,
    onSuccess: () => {
      onDone();
      onClose();
    },
  });
  return (
    <Dialog.Root open onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>{rollback ? `Roll ${name} back to an older sha` : `Set ${name} to ${status}`}</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                <Text>
                  Type <Code>{phrase(change)}</Code> to confirm.
                </Text>
                <Input autoFocus value={typed} onChange={(e) => setTyped(e.target.value)} />
                <CodedError error={save.error} messages={PACKAGE_ERROR_MESSAGES} />
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button colorPalette={status === PackageStatus.STABLE ? 'green' : 'red'} disabled={typed !== phrase(change)} loading={save.isPending} onClick={() => save.mutate()}>
                {rollback ? 'Roll back' : `Set ${status}`}
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
