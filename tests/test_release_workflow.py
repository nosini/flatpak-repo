"""Exercise the workflow's release policy and shell commands offline.

Read the specific indented blocks under test without adding a YAML package
requirement. actionlint checks the complete workflow's syntax separately.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import textwrap
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/publish.yml").read_text()


def step_script(name):
    step = WORKFLOW.split(f"      - name: {name}\n", 1)[1]
    script = re.search(r"^        run: \|\n((?:^          .*\n|^\n)+)", step, re.M)
    return textwrap.dedent(script[1])


def evaluate(expression, **context):
    """Evaluate the workflow gates' boolean subset with supplied job results."""
    expression = expression.strip().removeprefix("${{").removesuffix("}}")
    expression = re.sub(r"!(?!=)", "not ", expression.replace("&&", "and").replace("||", "or"))
    return eval(f"({expression})", {"__builtins__": {}},
                dict(context, startsWith=lambda value, prefix: value.startswith(prefix)))


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.env = dict(os.environ, TAG="v1.0.0")

    def run_step(self, name, **env):
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", step_script(name)],
            cwd=self.directory, env=dict(self.env, **env),
            capture_output=True, text=True,
        )

    def test_tag_classification(self):
        for tag, stable in (
            ("v1.0.0", True), ("v1.0.0-1", True), ("v1.0.0-12", True),
            ("v1.1.0-rc1", False), ("v1.1.0-beta.2", False),
            ("v1.1.0rc1", False), ("v1.0.0-", False), ("v1.0.0-1rc1", False),
            ("v1.0", False), ("v1.2.3/rc1", False),
        ):
            with self.subTest(tag=tag):
                output = self.directory / "output"
                result = self.run_step("Classify the version tag", TAG=tag, GITHUB_OUTPUT=str(output))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(), f"stable={str(stable).lower()}\n")
                output.unlink()

    def test_publication_requires_stable_tag_or_manual_main(self):
        expression = re.search(r"PUBLISH: >-\n(.*?)^        run:", WORKFLOW, re.M | re.S)[1]
        for event, ref, stable, enabled, expected in (
            ("push", "refs/tags/v1.0.0", "true", "true", True),
            ("push", "refs/tags/v1.0.0-1", "true", "true", True),
            ("push", "refs/tags/v1.1.0-rc1", "false", "true", False),
            ("push", "refs/tags/v1.0.0", "true", "false", False),
            ("push", "refs/heads/main", "", "true", False),
            ("workflow_dispatch", "refs/heads/main", "", "true", True),
            ("workflow_dispatch", "refs/heads/feature", "", "true", False),
            ("workflow_dispatch", "refs/tags/v1.1.0-rc1", "false", "true", False),
            ("pull_request", "refs/pull/1/merge", "", "true", False),
        ):
            with self.subTest(event=event, ref=ref, enabled=enabled):
                self.assertEqual(evaluate(
                    expression, github=NS(event_name=event, ref=ref),
                    steps=NS(version=NS(outputs=NS(stable=stable))),
                    vars=NS(PUBLISH_FLATPAK=enabled),
                ), expected)

    def test_release_requires_verification_and_required_deployment(self):
        job = WORKFLOW.split("  release:\n", 1)[1]
        expression = re.search(r"    if: >-\n(.*?)^    runs-on:", job, re.M | re.S)[1]
        for publish, result, deploy, cancelled, ref, expected in (
            ("true", "success", "success", False, "refs/tags/v1.0.0", True),
            ("false", "success", "skipped", False, "refs/tags/v1.1.0-rc1", True),
            ("true", "failure", "skipped", False, "refs/tags/v1.0.0", False),
            ("true", "success", "failure", False, "refs/tags/v1.0.0", False),
            ("true", "success", "skipped", False, "refs/tags/v1.0.0", False),
            ("true", "success", "cancelled", False, "refs/tags/v1.0.0", False),
            ("false", "failure", "skipped", False, "refs/tags/v1.1.0-rc1", False),
            ("true", "success", "success", True, "refs/tags/v1.0.0", False),
            ("true", "success", "success", False, "refs/heads/main", False),
        ):
            with self.subTest(publish=publish, result=result, deploy=deploy, ref=ref):
                self.assertEqual(evaluate(
                    expression, github=NS(ref=ref), cancelled=lambda: cancelled,
                    needs=NS(publish=NS(result=result, outputs=NS(publish=publish)),
                             deploy=NS(result=deploy)),
                ), expected)

    def test_artifacts_preserve_verified_files_and_checksums(self):
        repo = self.directory / "signed-repo"
        repo.mkdir()
        files = {"nosini.flatpakrepo": "remote", "one.flatpakref": "installer",
                 "sources.json": "{}", "summary": "signed summary", ".nojekyll": ""}
        for name, content in files.items():
            (repo / name).write_text(content)
        (self.directory / "signing-key.gpg").write_text("public key")
        result = self.run_step("Package the verified repository and installer files",
                               RUNNER_TEMP=str(self.directory))
        self.assertEqual(result.returncode, 0, result.stderr)
        assets = self.directory / "release-assets"
        with tarfile.open(assets / "nosini-repository.tar.gz") as archive:
            for name, content in files.items():
                self.assertEqual(archive.extractfile(f"./{name}").read().decode(), content)
        result = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=assets, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((assets / "one.flatpakref").read_text(), files["one.flatpakref"])
        self.assertEqual((assets / "signing-key.gpg").read_text(), "public key")

    def release_commands(self, *, stable="true", exists="false", fail="", assets=True):
        if assets:
            directory = self.directory / "release-assets"
            directory.mkdir()
            for name in ("nosini-repository.tar.gz", "nosini.flatpakrepo", "one.flatpakref", "sources.json"):
                (directory / name).touch()
        binary = self.directory / "gh"
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['release', 'view']:\n"
            "    sys.exit(0 if os.environ['RELEASE_EXISTS'] == 'true' else 1)\n"
            "with open(os.environ['GH_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "sys.exit(1 if args[1] == os.environ['RELEASE_FAIL'] else 0)\n"
        )
        binary.chmod(0o755)
        log = self.directory / "gh.jsonl"
        result = self.run_step(
            "Create the release and upload the verified artifacts",
            PATH=f"{self.directory}:{os.environ['PATH']}", STABLE=stable,
            RELEASE_EXISTS=exists, RELEASE_FAIL=fail, GH_LOG=str(log),
        )
        commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, commands

    def test_stable_release_attaches_artifacts_and_requires_existing_tag(self):
        result, commands = self.release_commands()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(commands), 1)
        args = commands[0]
        self.assertEqual(args[:3], ["release", "create", "v1.0.0"])
        for arg in ("--verify-tag", "--generate-notes", "release-assets/nosini-repository.tar.gz",
                    "release-assets/nosini.flatpakrepo", "release-assets/one.flatpakref"):
            self.assertIn(arg, args)
        self.assertNotIn("--prerelease", args)

    def test_prerelease_cannot_become_latest(self):
        result, commands = self.release_commands(stable="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--prerelease", commands[0])
        self.assertIn("--latest=false", commands[0])

    def test_rerun_replaces_assets_and_completes_existing_draft(self):
        result, commands = self.release_commands(exists="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([args[1] for args in commands], ["upload", "edit"])
        self.assertIn("--clobber", commands[0])
        self.assertIn("--draft=false", commands[1])

    def test_rerun_enforces_prerelease_status_before_upload(self):
        result, commands = self.release_commands(exists="true", stable="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([args[1] for args in commands], ["edit", "upload", "edit"])
        self.assertIn("--prerelease", commands[0])
        self.assertIn("--latest=false", commands[0])

    def test_upload_failure_does_not_publish_existing_draft(self):
        result, commands = self.release_commands(exists="true", fail="upload")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([args[1] for args in commands], ["upload"])

    def test_failed_prerelease_edit_stops_before_upload(self):
        result, commands = self.release_commands(exists="true", stable="false", fail="edit")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([args[1] for args in commands], ["edit"])

    def test_creation_failure_fails_the_step(self):
        result, _ = self.release_commands(fail="create")
        self.assertNotEqual(result.returncode, 0)

    def test_missing_artifacts_prevents_release_creation(self):
        result, commands = self.release_commands(assets=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(commands, [])


if __name__ == "__main__":
    unittest.main()
