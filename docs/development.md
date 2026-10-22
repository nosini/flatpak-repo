# Development

## How it works

The apps are packaged in their own repositories, which start from
[flatpak-template](https://github.com/nosini/flatpak-template). Each one
builds its app once per architecture, tests it, signs it with its own key
and publishes it to its own remote on GitHub Pages. This repository runs no
builds. `scripts/compose.py` collects the published packages into the
shared `nosini` remote:

1. **fetch** reads `apps.json`. For each app it downloads the app's
   `.flatpakref` and checks that its key has the fingerprint pinned in
   `apps.json`. With that key, it reads the signed summary of the app's
   remote and pulls the app and every extension on the app's branch, for
   every architecture: add-ons, `Locale` and `Debug` included. Each commit
   must be signed with the app's key. Refs outside the app's ID, such as
   another app or a runtime, are left out. The result, with the commit of
   every ref, is recorded in `sources.json`.
2. **unchanged** compares that record with the `sources.json` of the live
   shared remote, so runs without news don't publish.
3. **publish** downloads the live shared remote with its five previous
   versions, checked against the shared key, and adds the fetched commits
   on top with `flatpak build-commit-from`, signed with the shared key. A
   ref whose files didn't change keeps its commit. Refs that are no longer
   fetched (a removed app or architecture) are dropped. It then generates
   and signs the software catalogs and the summary, generates static
   deltas, and writes `nosini.flatpakrepo`, a `.flatpakref` and an icon for
   each app, `index.html` and `sources.json`.
4. **verify** checks the result as a client that only has the shared
   public key: every app and extension commit, the summary, and the
   catalog of every architecture must be signed, and each catalog must
   list the apps of its architecture.

Files the shared remote serves:

| File | Content |
| --- | --- |
| `nosini.flatpakrepo` | Adds the remote, with the public key. |
| `<name>.flatpakref`, `<name>.png` | Installs one app from this remote. The title and descriptions come from the app's own `.flatpakref`, the icon from its catalog. `<name>` is the app's own `.flatpakref` file name. |
| `index.html` | The download page. |
| `sources.json` | What was collected: each app's source, pinned key and the source commit of every ref. |

## GitHub Actions

`.github/workflows/publish.yml` runs `fetch`, then `publish` and `verify`,
and deploys the result to GitHub Pages. It runs:

- on manual runs, which always publish when publishing is enabled on `main`;
- when an app repository announces a publication (see below), and every
  six hours, which only publish if an app changed;
- when `apps.json` changes on `main`, so app registrations and removals
  take effect immediately;
- on pull requests, which collect and sign with a throwaway key without
  deploying, to test changes to `apps.json` and the script.

Publication needs every registered app. If one can't be downloaded or
fails a check, nothing is published and the live remote stays as it was.

The workflow caches downloaded OSTree file objects with `actions/cache`,
using the template's separate restore and save steps and a new key for
each run. Both app downloads and shared history pulls reuse these files
through OSTree's `--localcache-repo`. Refs, commit metadata, signatures,
summaries and keys are fetched fresh; every run still checks the live
remotes against the pinned keys. Signing keys are outside the cache.
Source files are refreshed after collection, including unchanged runs;
history files are refreshed after publication. Each refresh keeps only
the files in the current sources or retained shared history. Only
successful runs on `main` save the cache, after verification when there
is a new publication. A cache miss downloads everything as before.

Version tags and GitHub Releases belong to the app repositories. An app's
tag workflow builds, tests and publishes the app, then can announce its
publication here. This collector needs no tags or GitHub Releases: it
consumes the signed packages already served by the registered remotes.
The apps control which versions reach their stable remotes; collection
doesn't rebuild them or publish prerelease bundles.

Pushes to `main` run the checks and collection tests. Changes to `apps.json`
also collect and publish the live app remotes. Collection-script changes take
effect on the next registration change, announcement, scheduled run or manual
publication.

`.github/workflows/checks.yml` runs `actionlint` and the tests in `tests/`.
These publish small hand-made apps, signed with their own throwaway keys,
to a local web server and compose the shared remote from them. They need
flatpak, ostree and gpg, but no SDK:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

GitHub disables scheduled workflows in public repositories after 60 days
without a commit. Announcements from the app repositories keep the
remote up to date regardless; re-enable the schedule under **Actions** if
it was disabled.

### Setting up publishing

1. In **Settings → Pages**, select **GitHub Actions** as the source.
2. Store the shared signing key as the secret `FLATPAK_GPG_PRIVATE_KEY`
   (see below).
3. Set the variable:
   `gh variable set PUBLISH_FLATPAK --body true --repo nosini/flatpak-repo`.
4. If **Settings → Environments → github-pages** restricts deployment
   branches and tags, allow Branch `main`. This collector needs no tag
   rule; publication runs from `main`.
5. Run the workflow:
   `gh workflow run publish.yml --ref main --repo nosini/flatpak-repo`.

For a custom domain, set the variable `FLATPAK_REPO_URL` to the remote's
URL, with a trailing slash. GitHub Pages sites are limited to 1 GB; the
workflow stops before deploying a larger repository.

### Signing key

The shared remote has its own key. It must be an ASCII-armored GnuPG
private key without a passphrase. Generate it on your own machine, outside
any source checkout, in a separate GnuPG directory:

```sh
mkdir -p -m 700 "$HOME/.gnupg-flatpak-repo/private-keys-v1.d"
gpgconf --homedir "$HOME/.gnupg-flatpak-repo" --create-socketdir
gpg --homedir "$HOME/.gnupg-flatpak-repo" --batch --pinentry-mode loopback \
  --passphrase '' --quick-generate-key 'Nosini Flatpaks' ed25519 sign 0
```

Upload it through a private temporary file, so a failed export can't
upload an empty secret:

```sh
(
  set -eu
  umask 077
  key_file=$(mktemp "$HOME/.gnupg-flatpak-repo/export.XXXXXX")
  trap 'rm -f "$key_file"' EXIT
  gpg --homedir "$HOME/.gnupg-flatpak-repo" --armor \
    --export-secret-keys 'Nosini Flatpaks' > "$key_file"
  test -s "$key_file"
  gh secret set FLATPAK_GPG_PRIVATE_KEY --repo nosini/flatpak-repo < "$key_file"
)
```

Keep a backup of the key. Installed apps trust the key they were installed
with, so replacing it breaks their updates. If you replace it anyway, the
published history can't be checked with the new key and publication fails.
Set the variable `FLATPAK_KEEP_HISTORY` to `false` for one run to start a
new repository, then delete the variable.

## Adding an app

1. Package and publish the app from its own repository, starting from
   flatpak-template.
2. Add its `.flatpakref` and the fingerprint of its signing key to
   `apps.json`. The fingerprint is printed by

   ```sh
   curl -fsS https://nosini.github.io/example-flatpak/example.flatpakref \
     | sed -n 's/^GPGKey=//p' | base64 -d | gpg --show-keys
   ```

   Compare it with the key in the app repository's secret, which only you
   have, before trusting it.
3. Add the app to the README.
4. Set up the announcement below in the app's repository.

When an app's signing key is replaced, update its fingerprint here;
until then, collection stops with an error naming the app.

To remove an app, delete its entry. The next publication drops its refs;
installed copies stop receiving updates from this remote.

## Announcing new versions

An app repository can start this workflow right after it deploys, so new
versions don't wait for the schedule. Create a
[fine-grained personal access token](https://github.com/settings/personal-access-tokens/new)
with access to `nosini/flatpak-repo` only and the repository permission
**Contents: Read and write**, which `repository_dispatch` requires. Store it
in each app repository as the secret `FLATPAK_REPO_DISPATCH_TOKEN`, and add
this job to its `flatpak.yml`:

```yaml
  announce:
    needs: deploy
    runs-on: ubuntu-24.04
    steps:
      - name: Announce the publication to the shared remote
        env:
          GH_TOKEN: ${{ secrets.FLATPAK_REPO_DISPATCH_TOKEN }}
        run: |
          if [[ -n "$GH_TOKEN" ]]; then
            gh api repos/nosini/flatpak-repo/dispatches -f event_type=app-published
          fi
```

The token never reaches a job that builds code. Without it, the job does
nothing and the schedule picks the new version up.

## Running it locally

`compose.py` needs Python 3.11 or later, flatpak, ostree and gpg, and no
SDK. To collect everything and publish to a local directory with a test key:

```sh
python3 scripts/compose.py fetch work
export GNUPGHOME=$(mktemp -d) REPO_URL="file://$PWD/signed-repo/" KEEP_HISTORY=false
gpg --batch --pinentry-mode loopback --passphrase '' --quick-generate-key Test ed25519 sign 1d
export GPG_KEY=$(gpg --list-secret-keys --with-colons | awk -F: '$1 == "fpr" { print $10; exit }')
python3 scripts/compose.py publish work
gpg --export "$GPG_KEY" > test-key.gpg
python3 scripts/compose.py verify signed-repo test-key.gpg
flatpak remote-add --user --gpg-import=test-key.gpg nosini-test signed-repo
```

Remove `work/` and `signed-repo/` before running `fetch` or `publish`
again; the script doesn't overwrite them.

To reuse downloads locally, pass `--cache /path/to/cache` to both `fetch`
and `publish`. Keep that directory separate from `work/` and
`signed-repo/`, and keep it between runs. Deleting it forces fresh
downloads.
