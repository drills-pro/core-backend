#!/bin/sh
# Points git at the repo's hooks (.githooks). Idempotent.
cd "$(git rev-parse --show-toplevel)" && git config core.hooksPath .githooks
