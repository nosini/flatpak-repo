#!/usr/bin/env python3
"""Collect the apps' published Flatpak repositories into one signed remote.

The apps are built, tested and published by their own repositories. This
script never builds anything: it downloads the signed refs those
repositories publish and signs them again for the shared remote.

  fetch WORK            download every app in apps.json into WORK
  unchanged WORK URL    succeed if URL already publishes what WORK holds
  publish WORK          add WORK's apps to signed-repo/ and sign them
  verify REPO KEY       check REPO as a client that only has the public KEY
"""

import argparse
import base64
import configparser
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REMOTE_NAME = "nosini"
TITLE = "Nosini Flatpaks"
HOMEPAGE = "https://github.com/nosini/flatpak-repo"
FLATHUB = "https://dl.flathub.org/repo/flathub.flatpakrepo"
DEFAULT_BRANCH = "stable"

APP_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+(-[A-Za-z0-9_]+)?")
BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
ARCH = r"[A-Za-z0-9_]+"
NAME = re.compile(r"[a-z0-9][a-z0-9.-]*")
FINGERPRINT = re.compile(r"[0-9A-F]{40}")


class Error(Exception):
    pass


def run(*args, input=None, capture=False):
    # Standard error is kept for the error message; progress goes to stdout.
    return subprocess.run([str(arg) for arg in args], check=True, input=input,
                          stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE)


def output(*args):
    return run(*args, capture=True).stdout.decode()


def failure(error):
    detail = error.stderr.decode(errors="replace").strip() if isinstance(error, subprocess.CalledProcessError) \
        and error.stderr else ""
    return f"{error}\n{detail}" if detail else str(error)


def retry(action):
    # GitHub Pages sometimes answers with 503 when many files are requested.
    # Downloads and OSTree pulls can be repeated, so try a few times.
    for attempt in range(1, 6):
        try:
            return action()
        except (URLError, subprocess.CalledProcessError) as error:
            if attempt == 5 or (isinstance(error, HTTPError) and error.code == 404):
                raise
            print(f"Retrying after: {failure(error)}", file=sys.stderr, flush=True)
            time.sleep(attempt * float(os.environ.get("RETRY_DELAY", 30)))


def download(url):
    def get():
        with urlopen(url, timeout=60) as response:
            return response.read()
    return retry(get)


def check_url(url, what):
    parts = urlsplit(url)
    # Plain HTTP is only accepted from this machine, for the tests.
    local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
    if (parts.scheme != "https" and not local) or not parts.hostname or parts.username \
            or parts.password or parts.query or parts.fragment or re.search(r"\s", url):
        raise Error(f"{what} must be a plain HTTPS URL: {url!r}")
    return url


def load_registry(path=ROOT / "apps.json"):
    """Return {name: entry} for the apps in apps.json.

    Each entry names the app's published .flatpakref and the fingerprint of
    the key its repository is signed with. The name is the .flatpakref's
    file name, which the shared remote uses for its own copy.
    """
    entries = json.loads(Path(path).read_text())
    if not isinstance(entries, list) or not entries:
        raise Error("apps.json must list at least one app")
    apps = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"flatpakref", "key"}:
            raise Error(f"apps.json entries need exactly flatpakref and key: {entry!r}")
        url = check_url(entry["flatpakref"], "flatpakref")
        filename = urlsplit(url).path.rsplit("/", 1)[-1]
        name = filename.removesuffix(".flatpakref")
        if not filename.endswith(".flatpakref") or not NAME.fullmatch(name) or name == REMOTE_NAME:
            raise Error(f"Unsuitable .flatpakref file name: {url}")
        if name in apps:
            raise Error(f"Two apps publish {filename}")
        key = entry["key"].replace(" ", "").upper()
        if not FINGERPRINT.fullmatch(key):
            raise Error(f"{name}: key must be a full 40-digit fingerprint")
        apps[name] = {"flatpakref": url, "key": key}
    return apps


def key_fingerprints(key):
    with tempfile.TemporaryDirectory() as home:
        listing = run("gpg", "--homedir", home, "--batch", "--with-colons", "--show-keys",
                      input=key, capture=True).stdout.decode()
    fingerprints, primary = [], False
    for line in listing.splitlines():
        fields = line.split(":")
        if fields[0] in ("pub", "sub"):
            primary = fields[0] == "pub"
        elif fields[0] == "fpr" and primary:
            fingerprints.append(fields[9])
            primary = False
    return fingerprints


def read_flatpakref(name, entry):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    try:
        config.read_string(download(entry["flatpakref"]).decode())
        ref = dict(config["Flatpak Ref"])
        missing = {"Name", "Branch", "Url", "GPGKey", "Title"} - ref.keys()
        if missing:
            raise Error(f"{name}: .flatpakref lacks {', '.join(sorted(missing))}")
        key = base64.b64decode(ref["GPGKey"], validate=True)
    except (configparser.Error, KeyError, ValueError) as error:
        raise Error(f"{name}: unreadable .flatpakref: {error}") from error
    if not APP_ID.fullmatch(ref["Name"]):
        raise Error(f"{name}: invalid app ID {ref['Name']!r}")
    if not BRANCH.fullmatch(ref["Branch"]):
        raise Error(f"{name}: invalid branch {ref['Branch']!r}")
    if ref.get("IsRuntime", "false") != "false":
        raise Error(f"{name}: the .flatpakref describes a runtime, not an app")
    check_url(ref["Url"], f"{name}: Url")
    # The key is pinned in apps.json, so a key replaced at the source, by
    # mistake or by someone else, stops collection instead of being trusted.
    if key_fingerprints(key) != [entry["key"]]:
        raise Error(f"{name}: the .flatpakref's key is not {entry['key']} from apps.json")
    return ref, key


def own_refs(refs, app_id, branch):
    """Refs of the app and its extensions on its branch, for every arch.

    Anything else a source repository contains, such as another app or a
    runtime, isn't the app's to publish and is left out.
    """
    pattern = re.compile(rf"(app/{re.escape(app_id)}|runtime/{re.escape(app_id)}\.[A-Za-z0-9_.]+)"
                         rf"/{ARCH}/{re.escape(branch)}")
    return sorted(ref for ref in refs if pattern.fullmatch(ref))


def cache_options(cache, name):
    """Use cached file objects without reusing refs, commits or signatures."""
    repo = Path(cache) / name if cache is not None else None
    return [f"--localcache-repo={repo.resolve()}"] if repo is not None and repo.exists() else []


def refresh_cache(cache, name, repos):
    """Keep only the compressed file objects used by the current repositories.

    Commit metadata and signatures always come from the live remote. Separate
    source and published caches let unchanged runs refresh the sources without
    losing the shared history cached by the last publication.
    """
    if cache is None:
        return
    repo = Path(cache) / name
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)
    run("ostree", f"--repo={repo}", "init", "--mode=archive-z2")
    for source in repos:
        for obj in (Path(source) / "objects").glob("*/*.filez"):
            destination = repo / "objects" / obj.parent.name / obj.name
            if not destination.exists():
                destination.parent.mkdir(exist_ok=True)
                shutil.copyfile(obj, destination)


def fetch(work, apps, cache=None):
    work = Path(work)
    if work.exists():
        raise Error(f"{work} already exists; remove it first")
    ids = {}
    state = {}
    for name, entry in apps.items():
        ref, key = read_flatpakref(name, entry)
        app_id, branch = ref["Name"], ref["Branch"]
        for other_name, other_id in ids.items():
            if app_id == other_id or app_id.startswith(other_id + ".") or other_id.startswith(app_id + "."):
                raise Error(f"{name} and {other_name} overlap: {app_id} and {other_id}")
        ids[name] = app_id

        repo = work / "sources" / name
        repo.mkdir(parents=True)
        key_file = work / "sources" / f"{name}.gpg"
        key_file.write_bytes(key)
        run("ostree", f"--repo={repo}", "init", "--mode=archive-z2")
        # Both the summary, which lists the refs, and every commit must be
        # signed with the source's key.
        run("ostree", f"--repo={repo}", "remote", "add", f"--gpg-import={key_file}",
            "--set=gpg-verify=true", "--set=gpg-verify-summary=true", "source", ref["Url"], capture=True)
        listed = retry(lambda: output("ostree", f"--repo={repo}", "remote", "refs", "source"))
        refs = own_refs((line.split(":", 1)[1] for line in listed.split()), app_id, branch)
        if not any(r.startswith("app/") for r in refs):
            raise Error(f"{name}: {ref['Url']} has no {app_id} on branch {branch}")
        retry(lambda: run("ostree", f"--repo={repo}", "pull", "--mirror", "--depth=0",
                          *cache_options(cache, "sources"), "source", *refs))
        state[name] = {
            "flatpakref": entry["flatpakref"],
            "key": entry["key"],
            "id": app_id,
            "branch": branch,
            "url": ref["Url"],
            "title": ref["Title"],
            "comment": ref.get("Comment", ""),
            "description": ref.get("Description", ""),
            "homepage": ref.get("Homepage", ""),
            "refs": {r: output("ostree", f"--repo={repo}", "rev-parse", r).strip() for r in refs},
        }
        print(f"{name}: {len(refs)} refs from {ref['Url']}", flush=True)
    (work / "sources.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    refresh_cache(cache, "sources", [work / "sources" / name for name in state])


def unchanged(work, url):
    """Whether the published sources.json records exactly what WORK holds."""
    try:
        published = json.loads(download(url.rstrip("/") + "/sources.json"))
    except HTTPError as error:
        if error.code == 404:
            return False
        raise
    return published == json.loads((Path(work) / "sources.json").read_text())


def write_keyfile(path, section, values):
    with open(path, "w") as stream:
        stream.write(f"[{section}]\n")
        for key, value in values.items():
            if value:
                stream.write(f"{key}={' '.join(value.split())}\n")


def publish(work, output_dir="signed-repo", cache=None):
    work, repo = Path(work), Path(output_dir)
    gnupg, key, url = (os.environ.get(name) for name in ("GNUPGHOME", "GPG_KEY", "REPO_URL"))
    if not (gnupg and key and url):
        raise Error("Set GNUPGHOME, GPG_KEY and REPO_URL")
    url = url.rstrip("/") + "/"
    # Earlier versions kept for each app and extension, so users can roll
    # back with `flatpak update --commit`. KEEP_HISTORY=false starts over.
    depth = int(os.environ.get("HISTORY_DEPTH", 5))
    keep_history = os.environ.get("KEEP_HISTORY", "true") == "true"
    if repo.exists():
        raise Error(f"{repo} already exists; remove it first")
    state = json.loads((work / "sources.json").read_text())
    sign = [f"--gpg-sign={key}", f"--gpg-homedir={gnupg}"]
    public_key = run("gpg", "--batch", "--export", "--export-options", "export-minimal", key,
                     capture=True).stdout
    if not public_key:
        raise Error(f"No public key for {key}")
    key_file = work / "signing-key.gpg"
    key_file.write_bytes(public_key)

    run("ostree", f"--repo={repo}", "init", "--mode=archive-z2")
    # Start from the published repository, checked with the signing key, so
    # new versions become children of the published ones.
    if keep_history:
        try:
            download(url + "summary")
        except HTTPError as error:
            if error.code != 404:
                raise
            print(f"Nothing published at {url} yet; starting a new repository.", flush=True)
        else:
            run("ostree", f"--repo={repo}", "remote", "add", f"--gpg-import={key_file}",
                "--set=gpg-verify=true", "--set=gpg-verify-summary=true", "published", url, capture=True)
            retry(lambda: run("ostree", f"--repo={repo}", "pull", "--mirror", f"--depth={depth}",
                              *cache_options(cache, "published"), "published"))
            run("ostree", f"--repo={repo}", "remote", "delete", "published")

    # A ref whose files didn't change keeps its published commit.
    wanted = set()
    for name, app in state.items():
        for ref in app["refs"]:
            wanted.add(ref)
            run("flatpak", "build-commit-from", "--untrusted", "--no-update-summary", *sign,
                f"--src-repo={work / 'sources' / name}", repo, ref)
    # Apps removed from apps.json and architectures an app no longer builds
    # disappear instead of being served with stale content.
    for ref in output("ostree", f"--repo={repo}", "refs").split():
        if ref.startswith(("app/", "runtime/")) and ref not in wanted:
            run("ostree", f"--repo={repo}", "refs", "--delete", ref)

    # Generate the catalogs again, so they describe the new versions and are
    # signed together with the summary. Static deltas let clients download
    # an install or update as a few large files.
    run("ostree", f"--repo={repo}", "refs", "--delete", "appstream", "appstream2")
    run("flatpak", "build-update-repo", *sign, "--generate-static-deltas", "--prune",
        f"--prune-depth={depth}", f"--title={TITLE}", f"--default-branch={DEFAULT_BRANCH}", repo)

    gpg_key = base64.b64encode(public_key).decode()
    write_keyfile(repo / f"{REMOTE_NAME}.flatpakrepo", "Flatpak Repo", {
        "Title": TITLE,
        "Url": url,
        "Homepage": HOMEPAGE,
        "Comment": "Flatpak packages of " + ", ".join(app["title"] for app in state.values()),
        "Description": "Apps packaged by Nosini, collected from their own repositories",
        "DefaultBranch": DEFAULT_BRANCH,
        "GPGKey": gpg_key,
    })
    items = []
    for name, app in state.items():
        icon = copy_icon(repo, app, repo / f"{name}.png")
        write_keyfile(repo / f"{name}.flatpakref", "Flatpak Ref", {
            "Name": app["id"],
            "Branch": app["branch"],
            "Title": app["title"],
            "Url": url,
            "Homepage": app["homepage"],
            "Comment": app["comment"],
            "Description": app["description"],
            "Icon": url + icon.name if icon else "",
            "RuntimeRepo": FLATHUB,
            "IsRuntime": "false",
            "SuggestRemoteName": REMOTE_NAME,
            "GPGKey": gpg_key,
        })
        arches = sorted({ref.split("/")[2] for ref in app["refs"] if ref.startswith("app/")})
        items.append(
            f'<li><a href="{html.escape(name)}.flatpakref">{html.escape(app["title"])}</a>'
            f' ({html.escape(", ".join(arches))})'
            + (f'<br>{html.escape(app["comment"])}' if app["comment"] else "")
            + (f'<br><a href="{html.escape(app["homepage"])}">Packaging and instructions</a>'
               if app["homepage"] else "")
            + "</li>")
    (repo / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{TITLE}</title>\n<h1>{TITLE}</h1>\n"
        f'<p><a href="{REMOTE_NAME}.flatpakrepo">Add the {REMOTE_NAME} remote</a>, or open an '
        "app's link to install it. Runtimes come from Flathub.</p>\n"
        "<ul>\n" + "\n".join(items) + "\n</ul>\n"
        f'<p><a href="{HOMEPAGE}">Instructions and source</a></p>\n</html>\n')
    shutil.copy(work / "sources.json", repo / "sources.json")
    (repo / ".nojekyll").touch()
    # OSTree scratch files must not be published.
    shutil.rmtree(repo / "tmp", ignore_errors=True)
    refresh_cache(cache, "published", [repo])


def copy_icon(repo, app, destination):
    """Copy the app's catalog icon, which software centers show for the .flatpakref."""
    for ref in sorted(app["refs"], key=lambda ref: "/x86_64/" not in ref):
        if ref.startswith("app/"):
            try:
                icon = run("ostree", f"--repo={repo}", "cat", ref,
                           f"/files/share/app-info/icons/flatpak/128x128/{app['id']}.png",
                           capture=True).stdout
            except subprocess.CalledProcessError:
                continue
            destination.write_bytes(icon)
            return destination
    return None


def verify(repo, key_file):
    """Pull every ref's commit and each arch's catalog as a client would."""
    repo, key_file = Path(repo).resolve(), Path(key_file).resolve()
    scratch = repo / "tmp"
    had_scratch = scratch.exists()
    with tempfile.TemporaryDirectory() as client:
        run("ostree", f"--repo={client}", "init", "--mode=bare-user-only")
        run("ostree", f"--repo={client}", "remote", "add", f"--gpg-import={key_file}",
            "--set=gpg-verify=true", "--set=gpg-verify-summary=true", "test", f"file://{repo}", capture=True)
        refs = output("ostree", f"--repo={client}", "remote", "refs", "test").split()
        refs = [ref.split(":", 1)[1] for ref in refs]
        packages = [ref for ref in refs if ref.startswith(("app/", "runtime/"))]
        if not packages:
            raise Error("The repository is empty")
        run("ostree", f"--repo={client}", "pull", "--commit-metadata-only", "test", *packages, capture=True)
        for arch in sorted({ref.split("/")[2] for ref in packages}):
            # Flatpak only reads the catalog of the machine's own arch, so
            # the others are checked with OSTree, which rejects them unless
            # they are signed.
            run("ostree", f"--repo={client}", "pull", "test", f"appstream2/{arch}", capture=True)
            catalog = output("ostree", f"--repo={client}", "cat", f"test:appstream2/{arch}", "/appstream.xml")
            for ref in packages:
                kind, name, ref_arch, _ = ref.split("/")
                if kind == "app" and ref_arch == arch and f"<id>{name}</id>" not in catalog:
                    raise Error(f"{name} is missing from the {arch} catalog")
    # Reading the repository creates OSTree scratch files, which must not be
    # published.
    if not had_scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"{len(packages)} refs verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("fetch")
    command.add_argument("work", type=Path)
    command.add_argument("--cache", type=Path, help="reuse downloaded file objects in this directory")
    command = commands.add_parser("unchanged")
    command.add_argument("work", type=Path)
    command.add_argument("url")
    command = commands.add_parser("publish")
    command.add_argument("work", type=Path)
    command.add_argument("--output", type=Path, default=Path("signed-repo"))
    command.add_argument("--cache", type=Path, help="reuse downloaded history file objects in this directory")
    command = commands.add_parser("verify")
    command.add_argument("repo", type=Path)
    command.add_argument("key", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            fetch(args.work, load_registry(), args.cache)
        elif args.command == "unchanged":
            if not unchanged(args.work, args.url):
                print("The sources changed since the last publication.")
                sys.exit(1)
            print("Every app is published as it is.")
        elif args.command == "publish":
            publish(args.work, args.output, args.cache)
        else:
            verify(args.repo, args.key)
    except (Error, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"error: {failure(error)}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
