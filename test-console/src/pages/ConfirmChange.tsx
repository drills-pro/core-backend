import { useState } from 'react';
import { Button, Code, Dialog, Input, Portal, Stack, Text } from '@chakra-ui/react';
import type { Schemas } from '@/api/client';
import { useSetPackageStatus, type PackageStatusBody, type Pkg } from '@/api/packages';
import { PACKAGE_ERROR_MESSAGES, PackageStatus, SHA_CONFIRM_CHARS } from '@/constants';
import { CodedError } from '@/ui';

/** A status change the operator asked for; `slug` is set for a task bundle, `replaces` is the active package it retires. */
export type Change = { pkg: Pkg; status: Pkg['status']; slug?: Schemas['TaskType']; rollback?: boolean; replaces?: Pkg };

const phrase = ({ pkg }: Change) => `${pkg.name} ${pkg.sha256.slice(0, SHA_CONFIRM_CHARS)}`;
const short = (p: Pkg) => p.sha256.slice(0, SHA_CONFIRM_CHARS);

/** Typing the package name and the start of its sha back is the guard: a misclick cannot promote the wrong package. */
export function ConfirmChange({ change, onClose }: { change: Change; onClose: () => void }) {
  const [typed, setTyped] = useState('');
  const { pkg, status, rollback, replaces } = change;
  const save = useSetPackageStatus();
  const promote = status === PackageStatus.STABLE;
  return (
    <Dialog.Root open onOpenChange={(e) => !e.open && onClose()}>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>{rollback ? `Roll ${pkg.name} back to an older sha` : promote ? `Promote ${pkg.name}` : `Retire ${pkg.name}`}</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Stack gap="3">
                {promote && (
                  <Text>
                    <Code>{short(pkg)}</Code> becomes the active package for every tenant
                    {replaces ? (
                      <>
                        ; the current one, <Code>{short(replaces)}</Code>, is retired automatically.
                      </>
                    ) : (
                      '.'
                    )}{' '}
                    Runs already created keep theirs.
                  </Text>
                )}
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
              <Button
                colorPalette={promote ? 'green' : 'red'}
                disabled={typed !== phrase(change)}
                loading={save.isPending}
                onClick={() => save.mutate({ pkg, slug: change.slug, status: status as PackageStatusBody }, { onSuccess: onClose })}
              >
                {rollback ? 'Roll back' : promote ? 'Promote' : 'Retire'}
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
