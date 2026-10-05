"""Exercise composition with real exported refs, without a runtime sandbox."""

import argparse
import base64
import configparser
from functools import partial
import gzip
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("repository", ROOT / "scripts/repository.py")
repository = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repository)


def command(*args, **kwargs):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"Command failed: {args}\n{result.stderr}")
    return result


class RepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / ".build").mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=ROOT / ".build")
        cls.work = Path(cls.temp.name)
        cls.apps = repository.load_apps()
        cls.inputs = cls.work / "inputs"
        cls.inputs.mkdir()
        cls.arch = "x86_64"
        for app in cls.apps:
            repo = cls.inputs / app["name"]
            for ref in repository.expected_refs(app, cls.arch):
                kind, app_id, arch, branch = ref.split("/")
                build = cls.work / "builds" / app_id
                files = build / "files"
                files.mkdir(parents=True)
                (files / "payload.txt").write_text(app_id)
                section = "Application" if kind == "app" else "Runtime"
                (build / "metadata").write_text(
                    f"[{section}]\nname={app_id}\nruntime=org.freedesktop.Platform/{arch}/26.08\n"
                    f"sdk=org.freedesktop.Sdk/{arch}/26.08\n"
                )
                catalog = files / "share/app-info/xmls"
                catalog.mkdir(parents=True)
                component = (
                    f'<components version="1.0"><component type="desktop-application">'
                    f'<id>{app_id}</id><name>{app_id}</name><summary>Test package</summary>'
                    f'<launchable type="desktop-id">{app_id}.desktop</launchable>'
                    '</component></components>'
                )
                (catalog / f"{app_id}.xml.gz").write_bytes(gzip.compress(component.encode()))
                if kind == "app":
                    (build / "export").mkdir()
                command("flatpak", "build-export", "--disable-sandbox", "--no-update-summary",
                        *(["--runtime", "--files=files"] if kind == "runtime" else []), repo, build, branch)
            # Inputs already have unsigned catalogs. Composition must regenerate
            # these rather than copying them into a signed destination.
            command("flatpak", "build-update-repo", repo)
            (repo / "source.json").write_text(json.dumps({"commit": "test-commit", "source": app["source"]}))
        cls.destination = cls.work / "public"
        cls.compose(cls.destination)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def compose(cls, destination):
        repository.compose(argparse.Namespace(
            output=destination, inputs=cls.inputs, arch=cls.arch,
            url="https://example.org/flatpaks/", unsigned=True,
        ), cls.apps)

    def test_all_apps_and_extensions_keep_their_refs_and_payloads(self):
        actual = set(command("ostree", f"--repo={self.destination}", "refs", text=True).stdout.splitlines())
        expected = {ref for app in self.apps for ref in repository.expected_refs(app, self.arch)}
        self.assertEqual({ref for ref in actual if ref.startswith(("app/", "runtime/"))}, expected)
        self.assertIn("appstream/x86_64", actual)
        self.assertTrue((self.destination / "summary").is_file())
        for ref in expected:
            payload = command("ostree", f"--repo={self.destination}", "cat", ref, "/files/payload.txt", text=True).stdout
            self.assertEqual(payload, ref.split("/")[1])

    def test_catalog_contains_every_component(self):
        data = command("ostree", f"--repo={self.destination}", "cat",
                       "appstream/x86_64", "/appstream.xml.gz").stdout
        catalog = ET.fromstring(gzip.decompress(data))
        ids = {component.findtext("id") for component in catalog.findall("component")}
        expected = {ref.split("/")[1] for app in self.apps for ref in repository.expected_refs(app, self.arch)}
        self.assertEqual(ids, expected)

    def test_install_descriptors_use_shared_remote_and_original_branches(self):
        for app in self.apps:
            config = configparser.ConfigParser()
            config.read(self.destination / f"{app['name']}.flatpakref")
            ref = config["Flatpak Ref"]
            self.assertEqual(ref["Name"], app["id"])
            self.assertEqual(ref["Branch"], app["branch"])
            self.assertEqual(ref["SuggestRemoteName"], "nosini")
            self.assertEqual(ref["Url"], "https://example.org/flatpaks/")
            self.assertNotIn("GPGKey", ref)
        self.assertIn("test-commit", (self.destination / "sources.json").read_text())

    def test_remote_can_list_every_ref(self):
        env = dict(os.environ, FLATPAK_USER_DIR=str(self.work / "installation"))
        command("flatpak", "--user", "remote-add", "--no-gpg-verify", "test", self.destination, env=env)
        result = command("flatpak", "--user", "remote-ls", "--all", "--columns=ref", "test", env=env, text=True)
        refs = {line.strip() for line in result.stdout.splitlines()}
        for app in self.apps:
            for ref in repository.expected_refs(app, self.arch):
                self.assertIn(ref, refs)

    def test_missing_extension_stops_publication_before_creating_output(self):
        app = self.apps[0]
        ref = repository.expected_refs(app, self.arch)[1]
        repo = self.inputs / app["name"]
        commit = command("ostree", f"--repo={repo}", "rev-parse", ref, text=True).stdout.strip()
        command("ostree", f"--repo={repo}", "refs", "--delete", ref)
        destination = self.work / "incomplete"
        try:
            with self.assertRaisesRegex(ValueError, "missing refs"):
                self.compose(destination)
            self.assertFalse(destination.exists())
        finally:
            command("ostree", f"--repo={repo}", "refs", f"--create={ref}", commit)

    def test_existing_publication_is_preserved(self):
        with self.assertRaisesRegex(ValueError, "Output already exists"):
            self.compose(self.destination)
        self.assertTrue((self.destination / "summary").is_file())

    def test_fetch_rejects_a_descriptor_that_redirects_to_another_repository(self):
        app = self.apps[0]
        descriptor = b"[Flatpak Repo]\nUrl=https://example.org/other/\nGPGKey=AA==\n"
        destination = self.work / "unexpected-source"
        with patch.object(repository, "urlopen", return_value=io.BytesIO(descriptor)):
            with self.assertRaisesRegex(ValueError, "unexpected repository"):
                repository.fetch_app(app, destination, self.arch)
        self.assertFalse(destination.exists())

    def test_fetch_requires_a_signed_summary(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        app = dict(self.apps[0])
        handler = partial(QuietHandler, directory=str(self.inputs / app["name"]))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            app["source"] = f"http://127.0.0.1:{server.server_port}/"
            # This is a public key from the published gallery-dl descriptor;
            # tests do not generate or import a private signing key.
            public_key = (ROOT / "tests/fixtures/upstream-public-key.gpg").read_bytes()
            descriptor = (f"[Flatpak Repo]\nUrl={app['source']}\n"
                          f"GPGKey={base64.b64encode(public_key).decode()}\n").encode()
            destination = self.work / "unsigned-source"
            # Permit the local test HTTP server; production validation requires
            # HTTPS. Actual OSTree import and signature checks remain enabled.
            with patch.object(repository, "validate_url"), \
                 patch.object(repository, "urlopen", return_value=io.BytesIO(descriptor)), \
                 patch.object(repository, "run", side_effect=lambda *args, **kwargs: subprocess.run(
                     [str(arg) for arg in args], check=True, capture_output=True, **kwargs)):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    repository.fetch_app(app, destination, self.arch)
            self.assertIn(b"GPG verification enabled, but no summary", caught.exception.stderr)
            refs = command("ostree", f"--repo={destination}", "refs", text=True).stdout
            self.assertEqual(refs.strip(), "")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
