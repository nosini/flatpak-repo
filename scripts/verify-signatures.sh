#!/usr/bin/env bash
# Verify all app/extension/catalog commits and the summary with a public key.
set -euo pipefail
: "${GPG_KEY:?}"
: "${GNUPGHOME:?}"
repo=$(realpath "${1:?Usage: verify-signatures.sh REPOSITORY}")
verification_dir=$(mktemp -d)
trap 'rm -rf "$verification_dir"' EXIT
gpg --batch --export --export-options export-minimal "$GPG_KEY" > "$verification_dir/key.gpg"
test -s "$verification_dir/key.gpg"
ostree --repo="$verification_dir/repo" init --mode=bare-user
ostree --repo="$verification_dir/repo" remote add \
  --gpg-import="$verification_dir/key.gpg" \
  --set=gpg-verify=true --set=gpg-verify-summary=true local "file://$repo"
mapfile -t refs < <(ostree --repo="$repo" refs)
test "${#refs[@]}" -gt 0
# Metadata-only pulls verify commit signatures without duplicating app files.
ostree --repo="$verification_dir/repo" pull --commit-metadata-only local "${refs[@]}"
# This also checks that Flatpak can consume the signed software catalog.
FLATPAK_USER_DIR="$verification_dir/flatpak" flatpak --user remote-add \
  --gpg-import="$verification_dir/key.gpg" local "$repo"
FLATPAK_USER_DIR="$verification_dir/flatpak" flatpak --user update --appstream local
