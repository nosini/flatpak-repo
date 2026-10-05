#!/usr/bin/env python3
"""Fetch already-built apps and assemble one signed Flatpak repository."""

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
from urllib.parse import urlsplit
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
FLATHUB = "https://dl.flathub.org/repo/flathub.flatpakrepo"


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def output(*args):
    return run(*args, capture_output=True, text=True).stdout.strip()


def load_apps():
    apps = json.loads((ROOT / "apps.json").read_text())
    if not apps:
        raise ValueError("apps.json is empty")
    for field in ("name", "id"):
        values = [app[field] for app in apps]
        if len(set(values)) != len(values):
            raise ValueError(f"Duplicate {field} in apps.json")
    for app in apps:
        for field in ("name", "branch"):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", app[field]) or app[field] in (".", ".."):
                raise ValueError(f"Invalid {field}: {app[field]}")
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2,}", app["id"]):
            raise ValueError(f"Invalid app ID: {app['id']}")
        for field in ("source", "descriptor"):
            validate_url(app[field])
        if any(not re.fullmatch(r"[A-Za-z0-9_]+", suffix) for suffix in app["extensions"]):
            raise ValueError("Invalid extension suffix")
    return apps


def expected_refs(app, arch):
    return [f"app/{app['id']}/{arch}/{app['branch']}"] + [
        f"runtime/{app['id']}.{suffix}/{arch}/{app['branch']}"
        for suffix in app["extensions"]
    ]


def checked_refs(repo, app, arch):
    refs = output("ostree", f"--repo={repo}", "refs").splitlines()
    actual = {ref for ref in refs if ref.startswith(("app/", "runtime/"))}
    expected = set(expected_refs(app, arch))
    if actual != expected:
        raise ValueError(f"{app['name']}: missing refs {expected - actual}; unexpected refs {actual - expected}")
    return sorted(actual)


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Source repositories and descriptors must use HTTPS")
    if any(char in url for char in "\r\n"):
        raise ValueError("Invalid source URL")


def fetch_app(app, repo, arch):
    if repo.exists():
        raise ValueError(f"Input repository already exists: {repo}; remove it before fetching again")
    config = configparser.ConfigParser(interpolation=None)
    with urlopen(app["descriptor"], timeout=30) as response:
        config.read_string(response.read().decode("utf-8"))
    description = config["Flatpak Repo"]
    url = description["Url"]
    validate_url(url)
    if url.rstrip("/") != app["source"].rstrip("/"):
        raise ValueError(f"{app['name']}: descriptor points to an unexpected repository: {url}")
    public_key = base64.b64decode(description["GPGKey"], validate=True)
    if not public_key:
        raise ValueError(f"{app['name']}: source descriptor has an empty public key")
    repo.mkdir(parents=True)
    key_file = repo / "upstream-key.gpg"
    key_file.write_bytes(public_key)
    run("ostree", f"--repo={repo}", "init", "--mode=archive-z2")
    # The signed summary binds every ref to its commit checksum, and HTTP
    # pulls verify the complete object tree against those checksums. Some
    # producers leave automatically exported Locale commits unsigned, so
    # authenticate the whole snapshot through its signed summary instead.
    run("ostree", f"--repo={repo}", "remote", "add",
        f"--gpg-import={key_file}", "--set=gpg-verify=false",
        "--set=gpg-verify-summary=true", "upstream", url)
    refs = expected_refs(app, arch)
    run("ostree", f"--repo={repo}", "pull", "--mirror", "upstream", *refs)
    checked_refs(repo, app, arch)
    (repo / "source.json").write_text(json.dumps({
        "source": url,
        "descriptor": app["descriptor"],
        "refs": {ref: output("ostree", f"--repo={repo}", "rev-parse", ref) for ref in refs},
    }, indent=2) + "\n")


def fetch(args, apps):
    selected = [app for app in apps if args.app in ("all", app["name"])]
    if not selected:
        raise ValueError(f"Unknown app: {args.app}")
    for app in selected:
        fetch_app(app, args.inputs.resolve() / app["name"], args.arch)


def write_keyfile(path, section, values):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config[section] = values
    with path.open("w") as stream:
        config.write(stream, space_around_delimiters=False)


def write_metadata(repo, apps, url, public_key, arch):
    url = url.rstrip("/") + "/"
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "file") or (parsed.scheme == "https" and not parsed.netloc):
        raise ValueError("Repository URL must use https:// or file://")
    if any(char in url for char in "\r\n"):
        raise ValueError("Invalid repository URL")
    common = {"Url": url}
    if public_key:
        common["GPGKey"] = base64.b64encode(public_key).decode("ascii")
    write_keyfile(repo / "nosini.flatpakrepo", "Flatpak Repo", {
        "Title": "Nosini Flatpaks", "Comment": "Flatpak applications packaged by Nosini",
        "Homepage": "https://github.com/nosini/flatpak-repo", "DefaultBranch": "stable", **common,
    })
    links = []
    for app in apps:
        filename = f"{app['name']}.flatpakref"
        write_keyfile(repo / filename, "Flatpak Ref", {
            "Name": app["id"], "Branch": app["branch"], "Title": app["title"],
            "IsRuntime": "false", "SuggestRemoteName": "nosini", "RuntimeRepo": FLATHUB, **common,
        })
        links.append(f'<li><a href="{filename}">{html.escape(app["title"])}</a></li>')
    (repo / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Nosini Flatpaks</title><h1>Nosini Flatpaks</h1>'
        '<p>Install these applications with Flatpak. '
        '<a href="nosini.flatpakrepo">Add the remote</a> to receive updates.</p>'
        f'<p>Available for {html.escape(arch)}. Runtimes come from Flathub.</p>'
        '<ul>' + "".join(links) + '</ul>'
        '<p><a href="https://github.com/nosini/flatpak-repo">Source and instructions</a></p></html>\n'
    )
    (repo / ".nojekyll").touch()


def compose(args, apps):
    destination = args.output.resolve()
    if destination.exists():
        raise ValueError(f"Output already exists: {destination}; choose a new directory")
    signing = []
    public_key = b""
    if not args.unsigned:
        key = os.environ.get("GPG_KEY")
        homedir = os.environ.get("GNUPGHOME")
        if not key or not homedir:
            raise ValueError("Set GPG_KEY and GNUPGHOME, or use --unsigned for local testing")
        signing = [f"--gpg-sign={key}", f"--gpg-homedir={homedir}"]
        public_key = run("gpg", "--batch", "--homedir", homedir, "--export",
                         "--export-options", "export-minimal", key, capture_output=True).stdout
        if not public_key:
            raise ValueError("Signing public key export is empty")
    # Validate every input before creating the output. A missing app must never
    # result in a deployment that silently removes it from the shared remote.
    inputs = [(app, args.inputs.resolve() / app["name"]) for app in apps]
    refs = [(app, repo, checked_refs(repo, app, args.arch)) for app, repo in inputs]
    destination.mkdir(parents=True)
    run("ostree", f"--repo={destination}", "init", "--mode=archive-z2")
    sources = {}
    for app, repo, app_refs in refs:
        for ref in app_refs:
            # Recommit refs individually, including bundled extensions. Exporting
            # build directories would fold extension files back into the app.
            run("flatpak", "build-commit-from", f"--src-repo={repo}", "--no-update-summary",
                *signing, destination, ref)
        source_file = repo / "source.json"
        if source_file.exists():
            sources[app["name"]] = json.loads(source_file.read_text())
    # Fresh AppStream commits and summary are signed together; no unsigned
    # catalog commits from input repositories are copied.
    run("flatpak", "build-update-repo", *signing, "--title=Nosini Flatpaks",
        "--default-branch=stable", destination)
    write_metadata(destination, apps, args.url, public_key, args.arch)
    (destination / "sources.json").write_text(json.dumps(sources, indent=2) + "\n")
    shutil.rmtree(destination / "tmp", ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_parser = sub.add_parser("fetch", help="Pull already-built apps from their published repositories")
    fetch_parser.add_argument("app", help="Name from apps.json, or all")
    fetch_parser.add_argument("--inputs", type=Path, default=ROOT / ".build/repos")
    fetch_parser.add_argument("--arch", default="x86_64")
    compose_parser = sub.add_parser("compose", help="Merge and sign all app repositories")
    compose_parser.add_argument("--inputs", type=Path, default=ROOT / ".build/repos")
    compose_parser.add_argument("--output", type=Path, default=ROOT / "public")
    compose_parser.add_argument("--url", required=True)
    compose_parser.add_argument("--arch", default="x86_64")
    compose_parser.add_argument("--unsigned", action="store_true", help="Local testing only")
    args = parser.parse_args()
    try:
        apps = load_apps()
        if args.command == "fetch":
            fetch(args, apps)
        else:
            compose(args, apps)
    except (ValueError, KeyError, configparser.Error, URLError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
