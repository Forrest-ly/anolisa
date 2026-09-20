#!/usr/bin/env python3
"""Refuse to package a Tokenless version whose pinned release assets are absent.

The QwenPaw bundle pins its Python SDK wheel to a GitHub Release asset for
exactly the version that `make stamp-adapter-templates` writes into
`requirements.txt`. A version bump that reaches a package before the matching
`tokenless/vX.Y.Z` release is published therefore ships an installer that can
only fail with HTTP 404 -- the wheel is not missing, it has not been uploaded
yet (GH-3288, then GH-3390 one version later).

This script answers a single question so that every caller gates on the same
answer instead of probing differently: is the release for this version
published, carrying every asset the adapter templates pin? Its three
`--require` levels match the three moments that can open the window -- a bump
under review can only show its tag, a commit on the main branch has to show
downloadable wheels, and a package build must not emit anything until they are
there.

An unreachable registry stays advisory by default: an offline or mirrored
build host must still be able to package, and the QwenPaw installer keeps its
own probe as the last line of defence. `--strict` turns an inconclusive lookup
into a failure for callers that would rather stop than guess.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, NoReturn, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_DIR = SCRIPT_DIR.parent
DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_REPO = "alibaba/anolisa"

# The same anchor `scripts/rpm-build.sh` and `src/tokenless/Makefile` use: the
# first column-zero `version = "..."` in the workspace manifest is the version
# every adapter template gets stamped with.
CARGO_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)

# Adapter templates pin a release asset as
# `.../releases/download/tokenless/v@VERSION@/<asset>`; the asset name carries
# `@VERSION@` as well, and the line ends at the environment marker.
PINNED_ASSET_RE = re.compile(r"/releases/download/tokenless/v@VERSION@/([^\s;'\"()]+)")
# The host and owner/name the asset is pinned to -- that repository, not
# whichever fork is building, is where the release has to exist.
PINNED_REPO_RE = re.compile(
    r"https://[^/\s]+/([^/\s]+)/([^/\s]+)/releases/download/tokenless/v@VERSION@/"
)

REQUIRE_LEVELS = ("tag", "release", "assets")

EXIT_READY = 0
EXIT_NOT_READY = 1
EXIT_USAGE = 2


class Lookup(NamedTuple):
    """One GitHub API call: a decoded payload, or the reason there is none."""

    status: int
    payload: Optional[object]
    error: Optional[str]

    @property
    def found(self) -> bool:
        return self.status in (200, 300) and self.payload is not None

    @property
    def absent(self) -> bool:
        # Only an explicit 404 proves absence; anything else is inconclusive.
        return self.status == 404

    @property
    def inconclusive(self) -> bool:
        return not self.found and not self.absent


class Report(NamedTuple):
    verdict: str
    version: str
    repo: str
    tag: str
    require: str
    tag_exists: Optional[bool]
    release_exists: Optional[bool]
    expected_assets: Sequence[str]
    missing_assets: Sequence[str]
    problems: Sequence[str]
    detail: Optional[str]

    def as_json(self) -> Dict:
        return {
            "verdict": self.verdict,
            "version": self.version,
            "repo": self.repo,
            "tag": self.tag,
            "require": self.require,
            "tag_exists": self.tag_exists,
            "release_exists": self.release_exists,
            "expected_assets": list(self.expected_assets),
            "missing_assets": list(self.missing_assets),
            "problems": list(self.problems),
            "detail": self.detail,
        }


def usage_error(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def read_cargo_version(source_dir: Path) -> str:
    manifest = source_dir / "Cargo.toml"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as error:
        usage_error(f"{manifest}: cannot read the workspace manifest: {error}")
    match = CARGO_VERSION_RE.search(text)
    if not match:
        usage_error(f"{manifest}: no column-zero version field found")
    return match.group(1)


def pinned_templates(source_dir: Path) -> List[Path]:
    """Adapter templates that can pin a release asset.

    Only `.in` files are stamped at build time, and only the adapter payload
    tree ships pinned downloads, so the scan stays bounded (never the whole
    component directory, which may hold a populated `target/`).
    """
    adapters = source_dir / "adapters"
    if not adapters.is_dir():
        return []
    return sorted(path for path in adapters.rglob("*.in") if path.is_file())


def template_texts(source_dir: Path) -> List[Tuple[Path, str]]:
    texts: List[Tuple[Path, str]] = []
    for template in pinned_templates(source_dir):
        try:
            texts.append((template, template.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return texts


def pinned_assets(source_dir: Path, version: str) -> List[Tuple[str, Path]]:
    """The release assets this tree pins, as (asset name, template) pairs."""
    assets: List[Tuple[str, Path]] = []
    for template, text in template_texts(source_dir):
        for match in PINNED_ASSET_RE.finditer(text):
            name = match.group(1).replace("@VERSION@", version)
            if (name, template) not in assets:
                assets.append((name, template))
    return assets


def pinned_repository(source_dir: Path) -> Optional[str]:
    """The repository the templates download from -- where the release must live."""
    for _, text in template_texts(source_dir):
        match = PINNED_REPO_RE.search(text)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def resolve_token(explicit: Optional[str], repo: str) -> Optional[str]:
    """Present an ambient token only to the repository it was issued for.

    A GitHub Actions token is scoped to one repository, and presenting it to a
    different public repository can answer 404 where anonymous access would
    have returned the public release -- which this gate would misread as "not
    published". Two unauthenticated requests per run are well inside the
    anonymous budget, so dropping the token is the safe side to err on.
    """
    if explicit:
        return explicit
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return None
    scoped_to = os.environ.get("GITHUB_REPOSITORY", "")
    if scoped_to and scoped_to.lower() != repo.lower():
        return None
    return token


def decode_body(body: bytes, status: int, url: str) -> Lookup:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        return Lookup(0, None, f"unparsable response from {url}: {error}")
    if not isinstance(payload, (dict, list)):
        return Lookup(0, None, f"unexpected response shape from {url}")
    return Lookup(status, payload, None)


def api_get(url: str, token: Optional[str], timeout: float) -> Lookup:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "anolisa-tokenless-release-readiness",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return decode_body(response.read(), int(response.status), url)
    except urllib.error.HTTPError as error:
        if error.code != 300:
            return Lookup(error.code, None, f"HTTP {error.code} {error.reason}")
        # 300 carries the prefix matches in its body, which urllib surfaces as
        # an error; lookup_tag needs that list to find the exact ref.
        try:
            body = error.read()
        except OSError as read_error:
            return Lookup(error.code, None, f"HTTP {error.code}: {read_error}")
        return decode_body(body, error.code, url)
    except (urllib.error.URLError, OSError, ValueError) as error:
        return Lookup(0, None, str(error))


def lookup_tag(api_base: str, repo: str, tag: str, token: Optional[str], timeout: float) -> Lookup:
    """Resolve `tag` exactly.

    The refs endpoint answers a prefix match with HTTP 300 and a JSON array, so
    `tokenless/v0.8.3` would otherwise look published once `tokenless/v0.8.30`
    exists. Only an entry naming this ref counts as the tag being there.
    """
    lookup = api_get(f"{api_base.rstrip('/')}/repos/{repo}/git/refs/tags/{tag}", token, timeout)
    if not lookup.found:
        return lookup
    expected = f"refs/tags/{tag}"
    entries = lookup.payload if isinstance(lookup.payload, list) else [lookup.payload]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("ref") == expected:
            return Lookup(200, entry, None)
    return Lookup(404, None, f"no ref {expected} among the prefix matches")


def git_output(source_dir: Path, args: Sequence[str]) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(source_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", "replace")


def version_at_ref(source_dir: Path, ref: str) -> Optional[str]:
    """Read the workspace version as of `ref`, or None when git cannot tell."""
    prefix = git_output(source_dir, ["rev-parse", "--show-prefix"])
    if prefix is None:
        return None
    text = git_output(source_dir, ["show", f"{ref}:{prefix.strip()}Cargo.toml"])
    if text is None:
        return None
    match = CARGO_VERSION_RE.search(text)
    return match.group(1) if match else None


def templates_changed_since(source_dir: Path, ref: str, templates: Sequence[Path]) -> List[Path]:
    """Pinned templates that differ from `ref` -- they change the asset set."""
    # --relative keeps the paths comparable with the component-relative
    # template paths this script works with everywhere else.
    changed = git_output(source_dir, ["diff", "--name-only", "--relative", ref, "--", "adapters"])
    if changed is None:
        return []
    names = {line.strip() for line in changed.splitlines() if line.strip()}
    return [template for template in templates if str(template.relative_to(source_dir)) in names]


def _compare_with_ref(source_dir: Path, version: str, ref: str) -> Tuple[bool, str]:
    """Return (skip, reason): skip when REF already packaged this same release."""
    previous = version_at_ref(source_dir, ref)
    if previous is None:
        return False, f"cannot read the workspace version at {ref}; checking anyway"
    if previous != version:
        return False, f"version changed {previous} -> {version} since {ref}"
    changed = templates_changed_since(source_dir, ref, pinned_templates(source_dir))
    if changed:
        names = ", ".join(str(path.relative_to(source_dir)) for path in changed)
        return False, f"pinned adapter template changed since {ref}: {names}"
    return True, f"version {version} and pinned templates unchanged since {ref}"


def _inconclusive(
    make_report: Callable[..., Report], strict: bool, lookup: Lookup, tag: str, repo: str
) -> Report:
    detail = f"{lookup.error or 'HTTP ' + str(lookup.status)} while looking up {tag} on {repo}"
    if strict:
        return make_report("not_ready", None, [], [f"cannot verify the release: {detail}"], detail)
    return make_report("unknown", None, [], [], detail)


def release_readiness(
    source_dir: Path,
    version: str,
    require: str,
    api_base: str,
    repo: str,
    token: Optional[str],
    timeout: float,
    strict: bool,
) -> Report:
    tag = f"tokenless/v{version}"
    asset_names = [name for name, _ in pinned_assets(source_dir, version)]

    tag_exists: Optional[bool] = None

    def report(
        verdict: str,
        release_exists: Optional[bool],
        missing: Sequence[str],
        problems: Sequence[str],
        detail: Optional[str] = None,
    ) -> Report:
        return Report(
            verdict=verdict,
            version=version,
            repo=repo,
            tag=tag,
            require=require,
            tag_exists=tag_exists,
            release_exists=release_exists,
            expected_assets=asset_names,
            missing_assets=list(missing),
            problems=list(problems),
            detail=detail,
        )

    if not asset_names and require == "assets":
        # Nothing pins a release asset, so there is nothing to wait for.
        return report("ready", None, [], [], "no adapter template pins a release asset")

    ref_lookup = lookup_tag(api_base, repo, tag, token, timeout)
    if ref_lookup.inconclusive:
        return _inconclusive(report, strict, ref_lookup, tag, repo)
    tag_exists = ref_lookup.found

    if require == "tag":
        if tag_exists:
            return report("ready", None, [], [])
        return report(
            "not_ready",
            None,
            [],
            [f"tag `{tag}` does not exist on {repo}"],
        )

    release_lookup = api_get(
        f"{api_base.rstrip('/')}/repos/{repo}/releases/tags/{tag}", token, timeout
    )
    if release_lookup.inconclusive:
        return _inconclusive(report, strict, release_lookup, tag, repo)
    if release_lookup.absent:
        return report(
            "not_ready",
            False,
            asset_names,
            [f"GitHub Release `{tag}` does not exist on {repo}"],
        )

    if require == "release":
        return report("ready", True, [], [])

    published = {
        str(asset.get("name"))
        for asset in (release_lookup.payload or {}).get("assets", [])
        if isinstance(asset, dict)
    }
    missing = [name for name in asset_names if name not in published]
    if missing:
        return report(
            "not_ready",
            True,
            missing,
            [f"release `{tag}` is missing asset `{name}`" for name in missing],
        )
    return report("ready", True, [], [])


def pinned_template_labels(source_dir: Path, version: str) -> List[str]:
    """Component-relative template paths to name in a failure message."""
    return sorted(
        {
            str(template.relative_to(source_dir))
            for _, template in pinned_assets(source_dir, version)
        }
    )


def remediation(report: Report) -> List[str]:
    """The maintainer action that closes this specific gap."""
    workflow = ".github/workflows/release.yaml"
    if report.tag_exists is False:
        return [
            "Publish the release first, then re-run this check:",
            f"  git tag {report.tag} && git push origin {report.tag}",
            f"The tag starts {workflow}; approve its `release` environment",
            "deployment so the wheels are uploaded.",
        ]
    if report.release_exists is False:
        return [
            f"The tag exists but {workflow} has not published the release:",
            f"  approve the pending `release` environment deployment for {report.tag}",
            f"  (or re-run that workflow) so the wheels are uploaded",
        ]
    return [
        "The release exists but its wheel upload is incomplete: delete the release",
        f"and re-run {workflow} for {report.tag}, which refuses to overwrite an",
        "existing release.",
    ]


def render(report: Report, source_dir: Path, strict: bool) -> str:
    if report.verdict == "ready":
        suffix = ""
        if report.require == "assets" and report.expected_assets:
            suffix = f" with {len(report.expected_assets)} pinned asset(s)"
        elif report.require == "tag":
            suffix = f" (tag {report.tag} exists)"
        return f"tokenless v{report.version} is published{suffix}."
    if report.verdict == "unknown":
        return (
            f"warning: assuming tokenless v{report.version} is published -- "
            f"{report.detail}. Re-run with --strict to fail instead."
        )

    lines = [f"tokenless v{report.version} cannot be packaged yet (require={report.require}):"]
    lines.extend(f"  - {problem}" for problem in report.problems)
    templates = pinned_template_labels(source_dir, report.version)
    if templates:
        lines.append("Adapter templates pin the SDK wheel to that release:")
        lines.extend(f"  - {template}" for template in templates)
    lines.append(
        "A package built from this tree hands QwenPaw a bundle whose install.sh "
        "fails with HTTP 404 on the pinned wheel."
    )
    lines.extend(remediation(report))
    if not strict:
        lines.append(
            "Building for an offline mirror anyway? Set "
            "ANOLISA_ALLOW_UNPUBLISHED_WHEEL=1 for scripts/rpm-build.sh."
        )
    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that the Tokenless release for a version is published with its pinned assets.",
    )
    parser.add_argument(
        "--source-dir",
        default=str(DEFAULT_SOURCE_DIR),
        help="Tokenless component directory (default: the one containing this script).",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Version to check (default: the workspace Cargo.toml version).",
    )
    parser.add_argument(
        "--require",
        choices=REQUIRE_LEVELS,
        default="assets",
        help=(
            "What has to exist: the `tokenless/vX.Y.Z` tag, the GitHub Release, "
            "or the release carrying every pinned asset (default)."
        ),
    )
    parser.add_argument(
        "--changed-since",
        default=None,
        metavar="REF",
        help=(
            "Skip the check when neither the workspace version nor a pinned "
            "adapter template differs from REF (a git revision)."
        ),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("ANOLISA_RELEASE_REPO"),
        help=(
            "Repository hosting the release (default: the one the adapter "
            "templates download from, then $GITHUB_REPOSITORY)."
        ),
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("ANOLISA_GITHUB_API_BASE", DEFAULT_API_BASE),
        help=f"GitHub API root (default: {DEFAULT_API_BASE}).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "Bearer token for the API call. Defaults to $GITHUB_TOKEN / $GH_TOKEN "
            "when they were issued for the repository being queried."
        ),
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout in seconds.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat an inconclusive lookup (network, rate limit) as not ready.",
    )
    parser.add_argument("--json", action="store_true", help="Print the verdict as JSON.")
    parser.add_argument("--quiet", action="store_true", help="Only report a not-ready verdict.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    source_dir = Path(args.source_dir).resolve()
    if not source_dir.is_dir():
        usage_error(f"{source_dir}: not a directory")

    version = args.version or read_cargo_version(source_dir)

    if args.changed_since:
        unchanged, reason = _compare_with_ref(source_dir, version, args.changed_since)
        if unchanged:
            if not args.quiet:
                print(f"tokenless v{version}: skipped ({reason}).")
            return EXIT_READY

    repo = (
        args.repo
        or pinned_repository(source_dir)
        or os.environ.get("GITHUB_REPOSITORY")
        or DEFAULT_REPO
    )
    report = release_readiness(
        source_dir=source_dir,
        version=version,
        require=args.require,
        api_base=args.api_base,
        repo=repo,
        token=resolve_token(args.token, repo),
        timeout=args.timeout,
        strict=args.strict,
    )

    if args.json:
        print(json.dumps(report.as_json(), indent=2, sort_keys=True))
    elif report.verdict == "not_ready" or not args.quiet:
        message = render(report, source_dir, args.strict)
        print(message, file=sys.stderr if report.verdict == "not_ready" else sys.stdout)

    return EXIT_READY if report.verdict in ("ready", "unknown") else EXIT_NOT_READY


if __name__ == "__main__":
    raise SystemExit(main())
