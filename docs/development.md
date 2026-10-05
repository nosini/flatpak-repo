# Building and publishing

`apps.json` lists the source repository, Git ref, manifest, Flatpak branch,
build image, and expected bundled extensions for each app, including
Pulsemeeter's automatically exported `Locale` translations. The application
projects continue to maintain their own packaging; this repository builds
and combines it into one remote. Reddit Migration keeps its existing `master`
Flatpak branch; gallery-dl and Pulsemeeter use `stable`.

The GitHub workflow builds all apps on pushes to `main`, pull requests, manual
runs, and daily at 07:41 UTC. Each build reads the app's configured Git ref
and records the resolved commit in the published `sources.json`. The daily
run picks up packaging commits from the app repositories, including
Pulsemeeter's upstream updates once its own workflow commits them.

Only successful builds of every registered app can be published. Each
deployment contains the current app and extension refs, with a new signed
AppStream catalog and summary. Runtime and SDK refs stay on Flathub. Older
commits and static deltas are omitted to keep the repository small; OSTree
still reuses unchanged files when clients update.

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

Build jobs never receive the signing secret. A separate job imports it,
signs every app and extension, then regenerates and signs the catalog and
summary. It verifies signatures using a fresh repository with only the
public key before deploying. Pull requests assemble an unsigned repository
for validation and cannot deploy.

[GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
limits published sites to 1 GB. The workflow checks that limit. For a larger
repository, publish the generated directory to your own HTTPS server and set
`FLATPAK_REPO_URL` accordingly; replace the Pages upload and deploy steps with
your hosting provider's upload. A Flatpak remote is an
[OSTree archive repository served over HTTP](https://docs.flatpak.org/en/latest/hosting-a-repository.html).

## Local builds

Install Git, Python 3, Flatpak, flatpak-builder, OSTree, and GnuPG. Builds need
a Linux host where Flatpak's bubblewrap sandbox works. Existing local app
checkouts can be used without modifying them:

```sh
python3 scripts/repository.py build all --sources-root "$HOME/Git"
python3 scripts/repository.py compose --unsigned --url "file://$PWD/public/"
flatpak remote-add --user --no-gpg-verify nosini-local "$PWD/public"
flatpak install --user nosini-local eu.nosini.GalleryDl//stable
```

Omit `--sources-root` to clone the configured source refs instead. Use an app
name such as `pulsemeeter` instead of `all` to build one app. Composing the
shared remote always requires repositories for every registered app.
Builds, sources, and state live under `.build/`; the output lives in `public/`.
Remove an app's `.build/repos/NAME` before rebuilding it, and `public/` before
composing again. Remove `.build/sources/NAME` before fetching a fresh checkout;
the builder's `.build/state/NAME` cache can be retained.

For a signed local publication, set `GNUPGHOME` to your GnuPG home and
`GPG_KEY` to the signing fingerprint, omit `--unsigned`, and supply the
public HTTPS URL. Upload the entire output directory, including `summary`,
`summary.sig`, `config`, `objects`, and `refs`, to that URL. Keep the previous
publication available until the replacement is completely uploaded.

## Adding an app

Add an entry to `apps.json` with a unique name and app ID. Use an HTTPS source
URL and a Git branch or tag that `git clone --branch` accepts. Choose a build
image with the matching SDK and list every bundled extension suffix. The
workflow matrix and download links follow this registry automatically.
The composer checks the exported refs against it, including their branches,
so a missing app or extension stops publication. Update the README's app list
and install examples as well.

Run repository assembly tests with:

```sh
python3 -m unittest discover -s tests -v
```

These tests export small synthetic Flatpak refs and exercise the actual
OSTree merge and catalog generation without executing an app sandbox.
Application behavior tests remain in the individual app repositories.
