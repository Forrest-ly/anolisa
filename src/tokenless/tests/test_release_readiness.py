#!/usr/bin/env python3
"""Offline tests for the packaging gate in scripts/check-release-readiness.py.

The gate decides whether a Tokenless version may be packaged, so the tests
drive it end to end through its CLI against a stub GitHub API: a released
version passes, a version whose tag exists but whose wheels were never
uploaded fails (the GH-3390 state), and an unreachable registry stays
advisory unless the caller asks for `--strict`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-release-readiness.py"
REAL_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "adapters"
    / "tokenless"
    / "qwenpaw"
    / "requirements.txt.in"
)
PIN_RE = re.compile(r"/releases/download/tokenless/v@VERSION@/([^\s;]+)")
REPO = "alibaba/anolisa"


def pinned_names(version: str) -> List[str]:
    """Asset names the shipped QwenPaw template pins for `version`."""
    return [match.replace("@VERSION@", version) for match in PIN_RE.findall(REAL_TEMPLATE.read_text())]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server hook
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        route = self.server.routes.get(self.path.split("?")[0])
        if route is None:
            body = json.dumps({"message": f"no stub route for {self.path}"}).encode()
            self.send_response(404)
        else:
            status, payload = route
            body = json.dumps(payload if payload is not None else {}).encode()
            self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep the test output readable
        pass


class StubGitHub:
    """Serve a fixed route table on 127.0.0.1 and report its base URL."""

    def __init__(self, routes: Dict[str, Tuple[int, object]]) -> None:
        self._routes = routes
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.seen: List[Tuple[str, Optional[str]]] = []

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.routes = self._routes  # type: ignore[attr-defined]
        self._server.seen = self.seen  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __exit__(self, *exc_info) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


def release_routes(
    version: str,
    *,
    tag: bool = True,
    release: bool = True,
    assets: Optional[List[str]] = None,
    tag_status: int = 200,
    release_status: int = 200,
) -> Dict[str, Tuple[int, object]]:
    names = pinned_names(version) if assets is None else assets
    return {
        f"/repos/{REPO}/git/refs/tags/tokenless/v{version}": (
            tag_status if tag else 404,
            {"ref": f"refs/tags/tokenless/v{version}"} if tag else None,
        ),
        f"/repos/{REPO}/releases/tags/tokenless/v{version}": (
            release_status if release else 404,
            {"tag_name": f"tokenless/v{version}", "assets": [{"name": n} for n in names]}
            if release
            else None,
        ),
    }


def free_port_url() -> str:
    """A URL nothing listens on -- stands in for an unreachable registry."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


class ReleaseReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="tokenless-readiness-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.source = self._tmp / "src" / "tokenless"
        (self.source / "adapters" / "tokenless" / "qwenpaw").mkdir(parents=True)

    def write_source(self, version: str = "0.8.3", template: Optional[str] = None) -> Path:
        (self.source / "Cargo.toml").write_text(
            '[workspace]\nmembers = []\n\n[workspace.package]\nversion = "%s"\n' % version
        )
        body = REAL_TEMPLATE.read_text() if template is None else template
        (self.source / "adapters" / "tokenless" / "qwenpaw" / "requirements.txt.in").write_text(body)
        return self.source

    def run_gate(
        self,
        api_base: str,
        *args: str,
        source: Optional[Path] = None,
        env_overrides: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, dict, str, str]:
        env = os.environ.copy()
        env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1,localhost"
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_REPOSITORY", None)
        env.update(env_overrides or {})
        command = [
            sys.executable,
            str(SCRIPT),
            "--source-dir",
            str(source or self.source),
            "--api-base",
            api_base,
            "--timeout",
            "10",
            *args,
        ]
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False
        )
        stdout = completed.stdout.decode("utf-8", "replace")
        stderr = completed.stderr.decode("utf-8", "replace")
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = {}
        return completed.returncode, payload, stdout, stderr

    # -- verdicts -----------------------------------------------------------

    def test_published_release_with_every_pinned_wheel_is_ready(self) -> None:
        version = "0.8.3"
        self.write_source(version)
        self.assertEqual(len(pinned_names(version)), 3, "expected one wheel per supported platform")
        with StubGitHub(release_routes(version)) as base:
            code, report, stdout, _ = self.run_gate(base, "--json")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(report["verdict"], "ready")
        self.assertEqual(report["expected_assets"], pinned_names(version))
        self.assertEqual(report["missing_assets"], [])
        self.assertTrue(report["release_exists"])

    def test_tag_without_uploaded_wheels_blocks_packaging(self) -> None:
        """The GH-3390 state: the bump is tagged, the publish never ran."""
        version = "0.8.3"
        self.write_source(version)
        with StubGitHub(release_routes(version, release=False)) as base:
            code, report, _, _ = self.run_gate(base, "--json")
            message_code, _, _, message = self.run_gate(base)
        self.assertEqual(code, 1)
        self.assertEqual(message_code, 1)
        self.assertEqual(report["verdict"], "not_ready")
        self.assertTrue(report["tag_exists"])
        self.assertFalse(report["release_exists"])
        self.assertEqual(report["missing_assets"], pinned_names(version))
        self.assertIn("approve the pending `release` environment", message)

    def test_release_missing_one_platform_wheel_blocks_packaging(self) -> None:
        version = "0.8.3"
        self.write_source(version)
        published = pinned_names(version)[:2]
        with StubGitHub(release_routes(version, assets=published)) as base:
            code, report, _, _ = self.run_gate(base, "--json")
            _, _, _, message = self.run_gate(base)
        self.assertEqual(code, 1)
        self.assertEqual(report["missing_assets"], [pinned_names(version)[2]])
        self.assertIn(pinned_names(version)[2], message)

    def test_untagged_version_points_at_the_missing_tag(self) -> None:
        version = "0.9.9"
        self.write_source(version)
        with StubGitHub(release_routes(version, tag=False, release=False)) as base:
            code, report, _, _ = self.run_gate(base, "--json")
            _, _, _, message = self.run_gate(base)
        self.assertEqual(code, 1)
        self.assertFalse(report["tag_exists"])
        self.assertIn("git tag tokenless/v0.9.9", message)

    def test_require_levels_accept_the_evidence_they_ask_for(self) -> None:
        version = "0.8.3"
        self.write_source(version)
        with StubGitHub(release_routes(version, release=False)) as base:
            for require, expected in (("tag", 0), ("release", 1), ("assets", 1)):
                with self.subTest(require=require):
                    code, report, _, _ = self.run_gate(base, "--json", "--require", require)
                    self.assertEqual(code, expected)
                    self.assertEqual(report["require"], require)

    # -- the pinned asset set follows the templates -------------------------

    def test_asset_set_follows_the_adapter_template(self) -> None:
        version = "0.8.3"
        extra = (
            "\n"
            "anolisa-tokenless @ https://github.com/alibaba/anolisa/releases/download/"
            "tokenless/v@VERSION@/anolisa_tokenless-@VERSION@-cp311-abi3-win_amd64.whl "
            '; sys_platform == "win32"\n'
        )
        self.write_source(version, template=REAL_TEMPLATE.read_text() + extra)
        with StubGitHub(release_routes(version, assets=pinned_names(version))) as base:
            code, report, _, _ = self.run_gate(base, "--json")
            _, _, _, message = self.run_gate(base)
        self.assertEqual(code, 1)
        self.assertEqual(
            report["missing_assets"], [f"anolisa_tokenless-{version}-cp311-abi3-win_amd64.whl"]
        )
        self.assertIn("requirements.txt.in", message)

    def test_component_without_pinned_templates_has_nothing_to_wait_for(self) -> None:
        (self.source / "Cargo.toml").write_text('[workspace.package]\nversion = "0.8.3"\n')
        with StubGitHub({}) as base:
            code, report, stdout, _ = self.run_gate(base, "--json")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(report["verdict"], "ready")
        self.assertEqual(report["expected_assets"], [])

    def test_version_defaults_to_the_workspace_manifest(self) -> None:
        self.write_source("1.2.3")
        with StubGitHub(release_routes("1.2.3")) as base:
            code, report, _, _ = self.run_gate(base, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(report["version"], "1.2.3")

    def test_tag_prefix_match_does_not_read_as_published(self) -> None:
        """`v0.8.3` must not be satisfied by a `v0.8.30` prefix match (HTTP 300)."""
        version = "0.8.3"
        self.write_source(version)
        routes = release_routes(version, tag=False, release=False)
        routes[f"/repos/{REPO}/git/refs/tags/tokenless/v{version}"] = (
            300,
            [{"ref": "refs/tags/tokenless/v0.8.30"}],
        )
        with StubGitHub(routes) as base:
            code, report, _, _ = self.run_gate(base, "--json", "--require", "tag")
        self.assertEqual(code, 1)
        self.assertFalse(report["tag_exists"])

    def test_tag_prefix_match_containing_the_exact_ref_is_published(self) -> None:
        version = "0.8.3"
        self.write_source(version)
        routes = release_routes(version, release=False)
        routes[f"/repos/{REPO}/git/refs/tags/tokenless/v{version}"] = (
            300,
            [
                {"ref": "refs/tags/tokenless/v0.8.3"},
                {"ref": "refs/tags/tokenless/v0.8.30"},
            ],
        )
        with StubGitHub(routes) as base:
            code, report, _, _ = self.run_gate(base, "--json", "--require", "tag")
        self.assertEqual(code, 0)
        self.assertTrue(report["tag_exists"])

    # -- inconclusive lookups ----------------------------------------------

    def test_unreachable_registry_is_advisory_unless_strict(self) -> None:
        version = "0.8.3"
        self.write_source(version)
        dead = free_port_url()
        code, report, stdout, stderr = self.run_gate(dead, "--json")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(report["verdict"], "unknown")

        code, report, _, _ = self.run_gate(dead, "--json", "--strict")
        self.assertEqual(code, 1)
        self.assertEqual(report["verdict"], "not_ready")
        self.assertIn("cannot verify the release", report["problems"][0])

    def test_rate_limited_registry_is_advisory(self) -> None:
        version = "0.8.3"
        self.write_source(version)
        routes = release_routes(version, tag_status=403, release_status=403)
        with StubGitHub(routes) as base:
            code, report, _, _ = self.run_gate(base, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(report["verdict"], "unknown")

    # -- credentials --------------------------------------------------------

    def test_ambient_token_is_only_presented_to_its_own_repository(self) -> None:
        version = "0.8.3"
        self.write_source(version)
        token = "ghs_test-token"
        for scoped_to, expected in ((REPO, token), ("somewhere/else", None)):
            with self.subTest(scoped_to=scoped_to):
                stub = StubGitHub(release_routes(version))
                with stub as base:
                    code, _, _, stderr = self.run_gate(
                        base,
                        env_overrides={
                            "GITHUB_TOKEN": token,
                            "GITHUB_REPOSITORY": scoped_to,
                        },
                    )
                self.assertEqual(code, 0, stderr)
                self.assertTrue(stub.seen)
                for _, authorization in stub.seen:
                    if expected is None:
                        self.assertIsNone(authorization)
                    else:
                        self.assertEqual(authorization, f"Bearer {expected}")

    def test_explicit_token_is_presented_whatever_the_running_repository(self) -> None:
        version = "0.8.3"
        self.write_source(version)
        stub = StubGitHub(release_routes(version))
        with stub as base:
            code, _, _, stderr = self.run_gate(
                base,
                "--token",
                "ghs_explicit",
                env_overrides={"GITHUB_REPOSITORY": "somewhere/else"},
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stub.seen[0][1], "Bearer ghs_explicit")

    # -- bump scoping -------------------------------------------------------

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=gate-test", "-c", "user.email=gate@test.invalid", *args],
            cwd=str(self._tmp),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

    def commit_tree(self, version: str) -> None:
        self.write_source(version)
        self.git("add", "-A")
        self.git("commit", "-qm", f"chore(tokenless): stage {version}")

    def test_unchanged_version_skips_the_lookup(self) -> None:
        self.git("init", "-q", str(self._tmp))
        self.commit_tree("0.8.3")
        (self.source / "README.md").write_text("docs only\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "docs(tokenless): touch an unrelated file")
        # A dead API plus --strict proves the skip happens before any lookup.
        code, _, stdout, stderr = self.run_gate(free_port_url(), "--changed-since", "HEAD~1", "--strict")
        self.assertEqual(code, 0, stderr)
        self.assertIn("skipped", stdout)

    def test_bumped_version_is_checked(self) -> None:
        self.git("init", "-q", str(self._tmp))
        self.commit_tree("0.8.2")
        self.commit_tree("0.8.3")
        with StubGitHub(release_routes("0.8.3", release=False)) as base:
            code, report, _, _ = self.run_gate(base, "--json", "--changed-since", "HEAD~1")
        self.assertEqual(code, 1)
        self.assertEqual(report["version"], "0.8.3")

    def test_changed_pinned_template_is_checked_at_the_same_version(self) -> None:
        self.git("init", "-q", str(self._tmp))
        self.commit_tree("0.8.3")
        template = self.source / "adapters" / "tokenless" / "qwenpaw" / "requirements.txt.in"
        template.write_text(
            template.read_text()
            + "anolisa-tokenless @ https://github.com/alibaba/anolisa/releases/download/"
            "tokenless/v@VERSION@/anolisa_tokenless-@VERSION@-cp311-abi3-win_amd64.whl\n"
        )
        self.git("add", "-A")
        self.git("commit", "-qm", "feat(tokenless): pin a windows wheel")
        with StubGitHub(release_routes("0.8.3")) as base:
            code, report, _, stderr = self.run_gate(base, "--json", "--changed-since", "HEAD~1")
        self.assertEqual(code, 1, stderr)
        self.assertEqual(
            report["missing_assets"], [f"anolisa_tokenless-0.8.3-cp311-abi3-win_amd64.whl"]
        )

    # -- CLI surface --------------------------------------------------------

    def test_quiet_reports_only_a_blocking_verdict(self) -> None:
        version = "0.8.3"
        self.write_source(version)
        with StubGitHub(release_routes(version)) as base:
            code, _, stdout, stderr = self.run_gate(base, "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "")
        self.assertEqual(stderr.strip(), "")

        with StubGitHub(release_routes(version, release=False)) as base:
            code, _, stdout, stderr = self.run_gate(base, "--quiet")
        self.assertEqual(code, 1)
        self.assertEqual(stdout.strip(), "")
        self.assertIn("cannot be packaged yet", stderr)

    def test_source_dir_without_a_manifest_is_a_usage_error(self) -> None:
        (self.source / "adapters").mkdir(parents=True, exist_ok=True)
        code, _, _, stderr = self.run_gate(free_port_url())
        self.assertEqual(code, 2)
        self.assertIn("cannot read the workspace manifest", stderr)

    def test_missing_source_dir_is_a_usage_error(self) -> None:
        code, _, _, stderr = self.run_gate(
            free_port_url(), source=self._tmp / "absent"
        )
        self.assertEqual(code, 2)
        self.assertIn("not a directory", stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
