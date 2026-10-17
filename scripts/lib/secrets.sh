# Sourced by the bootstrap scripts. Writes core-api's local secret files (.local/secrets/*.json), the
# same shape the Vault Agent Injector renders in the cluster. core-api re-reads a file when it
# changes, so no restart is needed. Values must not contain double quotes or backslashes.
# The repo root via git, since callers sit at different depths (scripts/, scripts/steps/).
SECRETS_DIR="${SECRETS_DIR:-$(git -C "$(dirname "$0")" rev-parse --show-toplevel)/.local/secrets}"

# write_secret <file> <key> <value> [<key> <value> ...]
write_secret() {
  file="$1"; shift
  mkdir -p "$SECRETS_DIR"
  out="{"; sep=""
  while [ "$#" -gt 1 ]; do
    out="$out$sep\"$1\":\"$2\""; sep=","; shift 2
  done
  printf '%s}\n' "$out" > "$SECRETS_DIR/$file"
  chmod 644 "$SECRETS_DIR/$file"   # the container's user is not the host's
}
