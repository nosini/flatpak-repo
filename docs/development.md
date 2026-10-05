# Collecting and publishing packages

`apps.json` lists each app's published Flatpak repository, `.flatpakrepo`
descriptor, Flatpak branch, and expected bundled extensions, including
Pulsemeeter's `Locale` translations. Application projects build and publish
their own packages. This repository pulls the finished packages, signs them
with the shared key, and combines them into one remote. Reddit Migration
keeps its existing `master` Flatpak branch; gallery-dl and Pulsemeeter use
`stable`.

The GitHub workflow collects packages on pushes to `main`, pull requests,
manual runs, and daily at 07:41 UTC. It runs no app builds and needs no SDK
or privileged container. The daily run picks up new versions after the
individual app repositories finish publishing. Each source descriptor supplies
its repository's public key; the source URL must match `apps.json`.
The collector verifies the signed OSTree summary, which binds the app and
extension refs to their commit checksums. OSTree verifies the downloaded
objects against those checksums. Some producers leave automatically exported
translation commits unsigned, so input authentication uses the signed summary.
Every output commit receives the shared remote's signature.

Only complete downloads of every registered app can be published. Each
deployment contains the current app and extension refs, with a new signed
AppStream catalog and summary. Runtime and SDK refs stay on Flathub. Older
commits and static deltas are omitted to keep the repository small; OSTree
still reuses unchanged files when clients update.
`sources.json` records each source URL and the exact input commit checksum
for every app and extension. Packages come from the published repositories,
so collection needs no GitHub artifact token and has no artifact expiry.
Keep the individual app repositories' publishing enabled. If a source is
unavailable or missing a required ref, collection fails and the existing
shared publication remains available.

## GitHub Pages setup

Create `nosini/flatpak-repo` on GitHub and push this repository there. In
**Settings → Pages**, select **GitHub Actions** as the build source.

Configure these repository settings under **Settings → Secrets and variables
→ Actions**:

| Setting | Kind | Value |
| --- | --- | --- |
| `FLATPAK_GPG_PRIVATE_KEY` | Secret | ASCII-armored, unencrypted signing key export |
| `PUBLISH_FLATPAK` | Variable | `true` when signing and Pages are ready |
| `FLATPAK_REPO_URL` | Variable, optional | Public HTTPS URL if different from the default Pages URL |

Keep the signing key stable: clients trust the public key embedded in
`nosini.flatpakrepo` and the app's `.flatpakref`. An existing Flatpak signing
key can be reused if it is suitable for unattended CI signing. Otherwise,
generate a dedicated key on your own machine, outside source checkouts:

```sh
gpg --batch --passphrase '' --quick-generate-key 'Nosini Flatpak Repository' rsa3072 sign 0
gpg --list-secret-keys --keyid-format long
```

Copy its full primary fingerprint, then use the GitHub CLI to upload the
export from a private temporary file. These commands run on your machine;
the key is never committed:

```sh
(
  set -eu
  umask 077
  key_file=$(mktemp)
  trap 'rm -f "$key_file"' EXIT
  gpg --armor --export-secret-keys YOUR_FULL_FINGERPRINT > "$key_file"
  test -s "$key_file"
  gh secret set FLATPAK_GPG_PRIVATE_KEY --repo nosini/flatpak-repo < "$key_file"
)
gh variable set PUBLISH_FLATPAK --body true --repo nosini/flatpak-repo
gh workflow run flatpak.yml --repo nosini/flatpak-repo --ref main
```

The workflow downloads the packages before importing the signing secret.
It signs every app and extension, then regenerates and signs the catalog and
summary. It verifies signatures using a fresh repository with only the public
key before deploying. Pull requests assemble an unsigned repository for
validation and cannot deploy.

[GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
limits published sites to 1 GB. The workflow checks that limit. For a larger
repository, publish the generated directory to your own HTTPS server and set
`FLATPAK_REPO_URL` accordingly; replace the Pages upload and deploy steps with
your hosting provider's upload. A Flatpak remote is an
[OSTree archive repository served over HTTP](https://docs.flatpak.org/en/latest/hosting-a-repository.html).

## Collecting packages locally

Install Python 3, Flatpak, OSTree, and GnuPG. Collection needs network access
to the published repositories; it does not execute a Flatpak build sandbox:

```sh
python3 scripts/repository.py fetch all
python3 scripts/repository.py compose --unsigned --url "file://$PWD/public/"
flatpak remote-add --user --no-gpg-verify nosini-local "$PWD/public"
flatpak install --user nosini-local eu.nosini.GalleryDl//stable
```

Use an app name such as `pulsemeeter` instead of `all` to fetch one app.
Composing the shared remote requires repositories for every registered app.
Downloaded repositories live under `.build/repos/`; the output lives in
`public/`. Remove an app's `.build/repos/NAME` before fetching it again, and
`public/` before composing again. The collector refuses to overwrite existing
directories. Use `--inputs DIRECTORY` on both commands to use another input
location, including repositories exported by an app's own local build.

For a signed local publication, set `GNUPGHOME` to your GnuPG home and
`GPG_KEY` to the signing fingerprint, omit `--unsigned`, and supply the
public HTTPS URL. Upload the entire output directory, including `summary`,
`summary.sig`, `config`, `objects`, and `refs`, to that URL. Keep the previous
publication available until the replacement is completely uploaded.

## Adding an app

Add an entry to `apps.json` with a unique name and app ID, its HTTPS repository
URL, and its `.flatpakrepo` descriptor URL. The source must publish a signed
summary and provide the matching public key in the descriptor. Specify the
Flatpak branch and every bundled extension suffix, including automatic
`Locale` extensions. Collection and download links follow this registry
automatically. The composer checks the downloaded refs against it, including
their branches, so a missing app or extension stops publication. Update the
README's app list and install examples as well.

Run repository assembly tests with:

```sh
python3 -m unittest discover -s tests -v
```

These tests export small synthetic Flatpak refs and exercise the actual
OSTree merge and catalog generation without executing an app sandbox.
Application behavior tests remain in the individual app repositories.
