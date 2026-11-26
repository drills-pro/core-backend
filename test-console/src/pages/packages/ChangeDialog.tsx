import { useState } from 'react';
import { Button, Code, Dialog, Input, Portal, Stack, Text } from '@chakra-ui/react';
import { type PackageStatusBody, type Pkg, type Target, useSetPackageStatus } from '@/api/packages';
import { PACKAGE_ERROR_MESSAGES, PackageStatus, SHA_CONFIRM_CHARS } from '@/constants';
import type { Action } from '@/pages/packages/PackageCard';
import { CodedError } from '@/ui';

export type ChangeRequest = { target: Target; action: Action; pkg: Pkg; replaces?: Pkg };

const short = (p: Pkg) => p.sha256.slice(0, SHA_CONFIRM_CHARS);

/** Promote, roll back and retire all ask for the package's name and the start of its sha, typed back: a misclick cannot
 * change the package every tenant runs. */
export function ChangeDialog({ request, onClose }: { request: ChangeRequest; onClose: () => void }) {
  const { target, action, pkg, replaces } = request;
  const [typed, setTyped] = useState('');
  const save = useSetPackageStatus();
  const retiring = action === 'retire';
  const phrase = `${target.name} ${short(pkg)}`;
  const title = retiring ? `Retire ${target.name} ${short(pkg)}` : action === 'rollback' ? `Roll ${target.name} back to ${short(pkg)}` : `Promote ${short(pkg)}`;
  const confirmLabel = retiring ? 'Retire' : action === 'rollback' ? 'Roll back' : 'Promote';
  const effect = retiring
    ? 'It is taken out of use. Runs that already used it keep their model.'
    : `${short(pkg)} becomes the active package for every tenant${replaces ? `; the current one, ${short(replaces)}, is retired automatically` : ''}. Runs already created keep theirs.`;

  return (
    <Dialog.Root open onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>{title}</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                <Text>{effect}</Text>
                <Text>
                  Type <Code>{phrase}</Code> to confirm.
                </Text>
                <Input autoFocus value={typed} onChange={(e) => setTyped(e.target.value)} />
                <CodedError error={save.error} messages={PACKAGE_ERROR_MESSAGES} />
              </Stack>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button
                colorPalette={retiring ? 'red' : 'green'}
                disabled={typed !== phrase}
                loading={save.isPending}
                onClick={() =>
                  save.mutate({ target, status: (retiring ? PackageStatus.RETIRED : PackageStatus.STABLE) as PackageStatusBody }, { onSuccess: onClose })
                }
              >
                {confirmLabel}
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
