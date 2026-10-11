"""Tests for scripts/compose.py.

Hand-made app builds are exported and published, signed with their own
keys, to a local web server, the way the app repositories publish them.
The shared repository is then composed from them. Needs flatpak, ostree,
gpg and python3, but no SDK and no sandbox.
"""

import base64
from contextlib import contextmanager
from functools import partial
import gzip
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("compose", ROOT / "scripts/compose.py")
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, capture_output=True, text=True, **kwargs)


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + b"\xff\0\0\xff" * 128 for _ in range(128))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.site = self.tmp / "site"
        self.site.mkdir()
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=self.site))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}/"
        self.keys = {}
        os.environ["RETRY_DELAY"] = "0"
        self.shared_key = self.make_key("shared")
        self.use_shared_key(self.shared_key)
        self.icon = png()

    def make_key(self, name):
        home = self.tmp / f"gnupg-{name}"
        home.mkdir(mode=0o700)
        self.addCleanup(subprocess.run, ["gpgconf", "--homedir", home, "--kill", "all"])
        run("gpg", "--homedir", home, "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
            "--quick-generate-key", f"Test {name}", "ed25519", "sign", "0")
        listing = run("gpg", "--homedir", home, "--batch", "--with-colons", "--list-secret-keys").stdout
        fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr"))
        public = subprocess.run(["gpg", "--homedir", home, "--batch", "--export", fingerprint],
                                check=True, capture_output=True).stdout
        return {"home": home, "fingerprint": fingerprint, "public": public}

    def use_shared_key(self, key):
        os.environ.update(GNUPGHOME=str(key["home"]), GPG_KEY=key["fingerprint"], REPO_URL=self.url + "shared/")
        (self.tmp / "shared.gpg").write_bytes(key["public"])

    def export(self, repo, app_id, version, arches, extensions=(), branch="stable", extra_runtime=None):
        """Export an app build, with extensions, as an app's build job does."""
        for arch in arches:
            build = Path(tempfile.mkdtemp(dir=self.tmp))
            files = build / "app/files"
            for directory in ("bin", "share/metainfo", "share/app-info/icons/flatpak/128x128", "share/app-info/xmls"):
                (files / directory).mkdir(parents=True)
            (build / "app/export").mkdir()
            (build / "app/metadata").write_text(
                f"[Application]\nname={app_id}\nruntime=org.freedesktop.Platform/{arch}/26.08\n"
                f"sdk=org.freedesktop.Sdk/{arch}/26.08\ncommand=test\n")
            (files / "bin/test").write_text(f"#!/bin/sh\necho {version}\n")
            (files / f"share/app-info/icons/flatpak/128x128/{app_id}.png").write_bytes(self.icon)
            (files / f"share/app-info/xmls/{app_id}.xml.gz").write_bytes(gzip.compress(
                f'<components version="0.14" origin="{app_id}"><component type="desktop-application">'
                f"<id>{app_id}</id><name>Test</name><summary>A test</summary></component></components>".encode(),
                mtime=0))
            run("flatpak", "build-export", "--no-update-summary", f"--arch={arch}", repo, build / "app", branch)
            for suffix in [*extensions, *([extra_runtime] if extra_runtime else [])]:
                name = suffix if suffix == extra_runtime else f"{app_id}.{suffix}"
                (build / suffix / "files").mkdir(parents=True)
                (build / suffix / "files/data").write_text(version)
                (build / suffix / "metadata").write_text(
                    f"[Runtime]\nname={name}\n\n[ExtensionOf]\nref=app/{app_id}/{arch}/{branch}\n")
                run("flatpak", "build-export", "--no-update-summary", "--runtime", f"--arch={arch}",
                    repo, build / suffix, branch)
            shutil.rmtree(build)

    def publish_app(self, name, app_id, version, arches=("x86_64",), extensions=(), key=None,
                    branch="stable", extra_runtime=None, sign=True):
        """Publish a new build of an app at self.url/NAME/, as its repository does."""
        if key is None:
            key = self.keys.get(name) or self.keys.setdefault(name, self.make_key(name))
        build = self.tmp / f"build-{name}"
        shutil.rmtree(build, ignore_errors=True)
        self.export(build, app_id, version, arches, extensions, branch, extra_runtime)
        site = self.site / name
        shutil.rmtree(site, ignore_errors=True)
        sign_args = [f"--gpg-sign={key['fingerprint']}", f"--gpg-homedir={key['home']}"] if sign else []
        run("ostree", f"--repo={site}", "init", "--mode=archive-z2")
        for ref in run("ostree", f"--repo={build}", "refs").stdout.split():
            run("flatpak", "build-commit-from", "--no-update-summary", *sign_args, f"--src-repo={build}", site, ref)
        run("flatpak", "build-update-repo", *sign_args, site)
        (site / f"{name}.flatpakref").write_text(
            f"[Flatpak Ref]\nName={app_id}\nBranch={branch}\nTitle={name.title()}\nUrl={self.url}{name}/\n"
            f"Homepage=https://example.invalid/{name}\nComment=The {name} app\nIsRuntime=false\n"
            f"GPGKey={base64.b64encode(key['public']).decode()}\n")
        return {"flatpakref": f"{self.url}{name}/{name}.flatpakref", "key": key["fingerprint"]}

    def registry(self, *entries):
        path = self.tmp / "apps.json"
        path.write_text(json.dumps(list(entries)))
        return compose.load_registry(path)

    def fetch(self, *entries):
        work = self.tmp / "work"
        shutil.rmtree(work, ignore_errors=True)
        compose.fetch(work, self.registry(*entries))
        return work

    def publish(self, work, **env):
        """Compose the shared repository from WORK and deploy it."""
        output = self.tmp / "signed-repo"
        shutil.rmtree(output, ignore_errors=True)
        saved = {name: os.environ.get(name) for name in env}
        os.environ.update(env)
        try:
            compose.publish(work, output)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        compose.verify(output, self.tmp / "shared.gpg")
        shutil.rmtree(self.site / "shared", ignore_errors=True)
        shutil.copytree(output, self.site / "shared")
        return self.site / "shared"

    def refs(self, repo):
        return {ref for ref in run("ostree", f"--repo={repo}", "refs").stdout.split()
                if ref.startswith(("app/", "runtime/"))}

    def versions(self, repo, ref):
        return run("ostree", f"--repo={repo}", "log", ref).stdout.count("\ncommit ") + 1

    @contextmanager
    def fails(self, message):
        with self.assertRaises((compose.Error, OSError, subprocess.CalledProcessError)) as context:
            yield
        self.assertRegex(compose.failure(context.exception), message)

    def test_collects_every_arch_and_extension(self):
        one = self.publish_app("one", "org.example.One", "1", ("x86_64", "aarch64"), ("Locale", "Addon"))
        two = self.publish_app("two", "org.example.Two", "1")
        site = self.publish(self.fetch(one, two))
        self.assertEqual(self.refs(site), {
            "app/org.example.One/x86_64/stable", "app/org.example.One/aarch64/stable",
            "runtime/org.example.One.Locale/x86_64/stable", "runtime/org.example.One.Locale/aarch64/stable",
            "runtime/org.example.One.Addon/x86_64/stable", "runtime/org.example.One.Addon/aarch64/stable",
            "app/org.example.Two/x86_64/stable",
        })
        ref = (site / "one.flatpakref").read_text()
        self.assertIn("Name=org.example.One\n", ref)
        self.assertIn(f"Url={self.url}shared/\n", ref)
        self.assertIn("SuggestRemoteName=nosini\n", ref)
        self.assertIn(f"Icon={self.url}shared/one.png\n", ref)
        self.assertIn("Comment=The one app\n", ref)
        self.assertIn(f"GPGKey={base64.b64encode(self.shared_key['public']).decode()}\n", ref)
        self.assertEqual((site / "one.png").read_bytes(), self.icon)
        repo_file = (site / "nosini.flatpakrepo").read_text()
        self.assertIn(f"Url={self.url}shared/\n", repo_file)
        self.assertIn("Comment=Flatpak packages of One, Two\n", repo_file)
        self.assertIn('href="two.flatpakref"', (site / "index.html").read_text())
        self.assertTrue(any((site / "deltas").rglob("superblock")))
        self.assertNotIn("[remote", (site / "config").read_text())
        # Inspecting the deployed copy creates scratch files; the output must not have them.
        self.assertFalse((self.tmp / "signed-repo/tmp").exists())
        sources = json.loads((site / "sources.json").read_text())
        self.assertEqual(sources["one"]["key"], self.keys["one"]["fingerprint"])
        self.assertEqual(len(sources["one"]["refs"]), 6)

    def test_new_versions_keep_history(self):
        one = self.publish_app("one", "org.example.One", "1")
        ref = "app/org.example.One/x86_64/stable"
        work = self.fetch(one)
        site = self.publish(work)
        self.assertTrue(compose.unchanged(work, self.url + "shared/"))
        first = run("ostree", f"--repo={site}", "rev-parse", ref).stdout.strip()

        self.publish_app("one", "org.example.One", "2")
        work = self.fetch(one)
        self.assertFalse(compose.unchanged(work, self.url + "shared/"))
        site = self.publish(work)
        self.assertEqual(self.versions(site, ref), 2)
        self.assertEqual(run("ostree", f"--repo={site}", "rev-parse", f"{ref}^").stdout.strip(), first)
        second = run("ostree", f"--repo={site}", "rev-parse", ref).stdout.strip()

        # A new build with the same files adds no version.
        self.publish_app("one", "org.example.One", "2")
        site = self.publish(self.fetch(one))
        self.assertEqual(run("ostree", f"--repo={site}", "rev-parse", ref).stdout.strip(), second)

        self.publish_app("one", "org.example.One", "3")
        site = self.publish(self.fetch(one), HISTORY_DEPTH="1")
        self.assertEqual(self.versions(site, ref), 2)

        site = self.publish(self.fetch(one), KEEP_HISTORY="false")
        self.assertEqual(self.versions(site, ref), 1)

    def test_nothing_published_counts_as_changed(self):
        work = self.fetch(self.publish_app("one", "org.example.One", "1"))
        self.assertFalse(compose.unchanged(work, self.url + "shared/"))

    def test_removed_apps_and_arches_disappear(self):
        one = self.publish_app("one", "org.example.One", "1", ("x86_64", "aarch64"))
        two = self.publish_app("two", "org.example.Two", "1")
        self.publish(self.fetch(one, two))
        self.publish_app("one", "org.example.One", "2", ("x86_64",))
        site = self.publish(self.fetch(one))
        self.assertEqual(self.refs(site), {"app/org.example.One/x86_64/stable"})
        self.assertFalse((site / "two.flatpakref").exists())

    def test_only_the_apps_own_refs_are_collected(self):
        one = self.publish_app("one", "org.example.One", "1", extra_runtime="org.example.Platform")
        site = self.publish(self.fetch(one))
        self.assertEqual(self.refs(site), {"app/org.example.One/x86_64/stable"})

    def test_other_branches_are_left_out(self):
        one = self.publish_app("one", "org.example.One", "1", branch="beta")
        with self.fails("no org.example.One on branch stable"):
            # The .flatpakref names beta, so rewrite it to name stable.
            path = self.site / "one/one.flatpakref"
            path.write_text(path.read_text().replace("Branch=beta", "Branch=stable"))
            self.fetch(one)

    def test_pinned_key_must_match(self):
        one = self.publish_app("one", "org.example.One", "1")
        other = self.make_key("other")
        with self.fails("is not"):
            self.fetch({**one, "key": other["fingerprint"]})

    def test_source_must_be_signed_with_its_key(self):
        one = self.publish_app("one", "org.example.One", "1")
        # Republish with another key while the .flatpakref still has the old one.
        flatpakref = (self.site / "one/one.flatpakref").read_text()
        self.publish_app("one", "org.example.One", "2", key=self.make_key("intruder"))
        (self.site / "one/one.flatpakref").write_text(flatpakref)
        with self.fails("(?i)gpg|signature"):
            self.fetch(one)

    def test_unsigned_source_is_rejected(self):
        one = self.publish_app("one", "org.example.One", "1", sign=False)
        with self.fails("(?i)gpg|signature"):
            self.fetch(one)

    def test_missing_source_stops_collection(self):
        one = self.publish_app("one", "org.example.One", "1")
        shutil.rmtree(self.site / "one")
        with self.fails("404"):
            self.fetch(one)

    def test_published_repository_must_match_the_key(self):
        one = self.publish_app("one", "org.example.One", "1")
        self.publish(self.fetch(one))
        self.use_shared_key(self.make_key("new-shared"))
        with self.fails("(?i)gpg|signature"):
            compose.publish(self.fetch(one), self.tmp / "other-repo")

    def test_overlapping_app_ids_are_rejected(self):
        one = self.publish_app("one", "org.example.One", "1")
        two = self.publish_app("two", "org.example.One.Two", "1")
        with self.fails("overlap"):
            self.fetch(one, two)

    def test_registry_is_checked(self):
        key = "A" * 40
        good = {"flatpakref": "https://example.org/app/app.flatpakref", "key": key}
        self.assertEqual(self.registry(good), {"app": {"flatpakref": good["flatpakref"], "key": key}})
        for entries, message in [
            ([], "at least one"),
            ([{**good, "extra": 1}], "exactly"),
            ([{**good, "flatpakref": "http://example.org/app.flatpakref"}], "HTTPS"),
            ([{**good, "flatpakref": "https://example.org/app.flatpakrepo"}], "file name"),
            ([{**good, "flatpakref": "https://example.org/nosini.flatpakref"}], "file name"),
            ([{**good, "key": "ABCD"}], "fingerprint"),
            ([good, {**good, "flatpakref": "https://example.net/app.flatpakref"}], "Two apps"),
        ]:
            with self.subTest(message), self.fails(message):
                self.registry(*entries)

    def test_repository_registry_is_valid(self):
        self.assertTrue(compose.load_registry())


if __name__ == "__main__":
    unittest.main()
