#!/usr/bin/env bash
# Generate a dedicated SSH key for the Kindle and install the public key
# onto a Kindle that is mounted at KINDLE_MOUNT (default /media/$USER/Kindle).
#
# Usage:
#   ./setup-ssh-key.sh
#   KINDLE_MOUNT=/mnt/kindle ./setup-ssh-key.sh
set -euo pipefail

KEY_FILE="${HOME}/.ssh/kindle_lyrical"
KINDLE_MOUNT="${KINDLE_MOUNT:-/media/${USER}/Kindle}"
AUTHORIZED_KEYS="${KINDLE_MOUNT}/koreader/settings/SSH/authorized_keys"

if [[ ! -d "${KINDLE_MOUNT}/koreader" ]]; then
    echo "ERROR: Kindle not found at ${KINDLE_MOUNT}." >&2
    echo "       Mount it, then re-run, or set KINDLE_MOUNT=/path/to/mount." >&2
    exit 1
fi

if [[ -f "${KEY_FILE}" ]]; then
    echo "Key already exists at ${KEY_FILE} — skipping generation."
else
    ssh-keygen -t ed25519 -C "kindle-lyrical" -N "" -f "${KEY_FILE}"
    echo "Key generated: ${KEY_FILE}"
fi

install -D -m 600 "${KEY_FILE}.pub" "${AUTHORIZED_KEYS}"
echo "Public key installed to ${AUTHORIZED_KEYS}"
echo
echo "Add this to your .env:"
echo "  KINDLE_KEY=${KEY_FILE}"
