#!/usr/bin/env python3
"""Verify the tokenless release wheel inventory and versions.

Release wheels are built from ``src/tokenless/Cargo.toml`` while the GitHub
Release version comes from the pushed ``tokenless/v*`` tag (or the preview
workflow input). If a tag is created on a commit whose Cargo.toml still
declares an older version, an inventory-only gate would publish stale wheels
on the new Release. This script therefore fails closed unless both wheels
carry exactly the expected release version, in their file names and in their
embedded METADATA.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path


def die(message: str) -> None:
    """Exit with one actionable diagnostic."""
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def classify(directory: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """Split the wheel directory into runtime, AgentScope, and stray wheels."""
    runtime: list[Path] = []
    agentscope: list[Path] = []
    unexpected: list[Path] = []
    for wheel in sorted(directory.glob("*.whl")):
        name = wheel.name
        if name.startswith("anolisa_tokenless_agentscope-"):
            agentscope.append(wheel)
        elif name.startswith("anolisa_tokenless-"):
            runtime.append(wheel)
        else:
            unexpected.append(wheel)
    return runtime, agentscope, unexpected


def filename_version(wheel: Path) -> str:
    """Read the version field from a wheel file name."""
    parts = wheel.name.split("-")
    if len(parts) < 5:
        die(f"wheel file name does not follow PEP 427: {wheel.name}")
    return parts[1]


def metadata_version(wheel: Path) -> str:
    """Read the Version header from the wheel's dist-info METADATA."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                die(f"{wheel.name} contains {len(metadata_names)} METADATA files")
            text = archive.read(metadata_names[0]).decode("utf-8")
    except (OSError, zipfile.BadZipFile) as error:
        die(f"cannot read wheel {wheel.name}: {error}")
    for line in text.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    die(f"{wheel.name} METADATA has no Version header")


def check_version(wheel: Path, expected: str) -> None:
    """Fail unless both embedded versions match the release version."""
    for source, actual in (
        ("file name", filename_version(wheel)),
        ("METADATA", metadata_version(wheel)),
    ):
        if actual != expected:
            die(
                f"wheel {wheel.name} {source} version {actual} does not match "
                f"release version {expected}; the tag was likely created on a "
                "commit whose src/tokenless/Cargo.toml is out of sync"
            )
    print(f"Verified {wheel.name} matches release version {expected}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the tokenless release wheel inventory and versions."
    )
    parser.add_argument(
        "--directory",
        required=True,
        type=Path,
        help="Directory containing the built wheels",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Release version the wheels must carry (tag version or preview input)",
    )
    args = parser.parse_args()

    if not args.directory.is_dir():
        die(f"wheel directory does not exist: {args.directory}")

    runtime, agentscope, unexpected = classify(args.directory)
    for wheel in unexpected:
        die(f"unexpected wheel in release directory: {wheel.name}")
    if len(runtime) != 1:
        die(
            f"expected exactly one anolisa_tokenless wheel, found {len(runtime)}"
        )
    if len(agentscope) != 1:
        die(
            "expected exactly one anolisa_tokenless_agentscope wheel, found "
            f"{len(agentscope)}"
        )
    if "-cp311-abi3-" not in runtime[0].name:
        die(
            "native wheel is not tagged for the CPython 3.11 stable ABI: "
            f"{runtime[0].name}"
        )

    for wheel in (runtime[0], agentscope[0]):
        check_version(wheel, args.version)


if __name__ == "__main__":
    main()
