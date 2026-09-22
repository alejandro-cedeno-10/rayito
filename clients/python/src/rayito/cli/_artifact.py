"""The image artifact: a deterministic zip of the image directory and the
filtered copy of the kernel sidecar. Standard library only, by contract.

``create-microvm-image`` expects the code artifact to be a zip whose root
holds the Dockerfile and everything it ``COPY``s. Development leftovers
inside any subtree (``__pycache__``, ``tests``, virtualenvs, tool caches,
lock files, bytecode, other zips) never enter the artifact, so the image
hash depends on shipped files only; dates and modes are fixed so the archive
bytes depend on content only.

``--variant slim`` adds one synthetic entry to the archive,
``kernel-sidecar/ipython/startup/warmup_variant`` holding ``slim``, which
``0004_warmup.py`` reads to skip the kernel warm-up; ``--variant poly`` adds
``kernel-sidecar/kernels_variant`` holding ``poly``, which the conditional
layer of ``image/Dockerfile`` reads to install the bash kernel (javascript
stays a reserved name: no image ships that kernel, AWS_API_NOTES.md Q57).
Nothing is written under the image
directory, and the variants of one tree differ only by their marker entry
(and so by their content hash). ``full`` (the default) adds nothing.

``scripts/image_zip.py`` and ``scripts/copy_sidecar.py`` load this module by
file path and run ``zip_main`` / ``copy_main``: the CI ``build`` job and
``release.yml`` zip the image on a bare ``python3`` without installing the
SDK, so nothing here may import a third-party package (``tests/unit/cli/
test_artifact.py`` parses the file and checks every import against
``sys.stdlib_module_names``).
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

EXCLUDED_SUFFIXES = (".zip", ".pyc")
EXCLUDED_DIRECTORIES = frozenset(
    {"__pycache__", "tests", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)
EXCLUDED_FILES = frozenset({"uv.lock", ".gitignore"})
VARIANTS = ("full", "slim", "poly")
WARMUP_MARKER_ENTRY = "kernel-sidecar/ipython/startup/warmup_variant"
KERNELS_MARKER_ENTRY = "kernel-sidecar/kernels_variant"
SLIM_MARKER_CONTENT = "slim\n"
POLY_MARKER_CONTENT = "poly\n"
MARKER_ENTRIES = {"slim": WARMUP_MARKER_ENTRY, "poly": KERNELS_MARKER_ENTRY}
MARKER_CONTENTS = {"slim": SLIM_MARKER_CONTENT, "poly": POLY_MARKER_CONTENT}
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
REQUIREMENTS_FILES = ("requirements.txt", "requirements-poly.txt")
ZIP_USAGE = "image_zip.py IMAGE_DIR DESTINATION [--variant full|slim|poly]"
COPY_USAGE = "copy_sidecar.py SOURCE DESTINATION"


def is_excluded(relative: Path) -> bool:
    if relative.suffix in EXCLUDED_SUFFIXES or relative.name in EXCLUDED_FILES:
        return True
    return any(part in EXCLUDED_DIRECTORIES for part in relative.parts[:-1])


def shipped_files(root: Path) -> list[Path]:
    """Files under ``root`` that the zip ships: the exclusion list is checked
    before the filesystem is touched, so an unreadable entry inside an
    excluded directory (a Linux venv symlink seen from Windows) never
    aborts the walk."""
    return [
        path
        for path in sorted(root.rglob("*"))
        if not is_excluded(path.relative_to(root)) and path.is_file()
    ]


def image_files(image_dir: Path) -> list[Path]:
    files = shipped_files(image_dir)
    if not (image_dir / "Dockerfile").is_file():
        raise SystemExit(f"{image_dir}/Dockerfile is missing")
    return files


def normalized(info: zipfile.ZipInfo, *, executable: bool) -> zipfile.ZipInfo:
    """Fixed date and mode so the archive hash depends on content only."""
    info.external_attr = (0o755 if executable else 0o644) << 16
    info.date_time = FIXED_DATE_TIME
    return info


def write_zip(image_dir: Path, destination: Path, variant: str = "full") -> int:
    files = image_files(image_dir)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo.from_file(path, arcname=path.relative_to(image_dir).as_posix())
            with path.open("rb") as source:
                archive.writestr(normalized(info, executable=path.name == "rayd"), source.read())
        if variant in MARKER_ENTRIES:
            marker = zipfile.ZipInfo(MARKER_ENTRIES[variant])
            archive.writestr(normalized(marker, executable=False), MARKER_CONTENTS[variant])
    return len(files) + (1 if variant in MARKER_ENTRIES else 0)


def marker_variant(artifact: Path) -> str:
    """The variant an artifact zip was built for: ``poly`` when it carries
    the kernels marker with ``poly``, ``slim`` when it carries the warm-up
    marker with ``slim``, ``full`` otherwise. Both markers at once name an
    artifact no script produces, so the archive is refused."""
    with zipfile.ZipFile(artifact) as archive:
        names = set(archive.namelist())
        found = [
            variant
            for variant, entry in MARKER_ENTRIES.items()
            if entry in names and archive.read(entry).decode("utf-8").strip() == variant
        ]
    if len(found) > 1:
        raise SystemExit(f"{artifact} carries both the slim and the poly markers")
    return found[0] if found else "full"


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_tree(source: Path, destination: Path) -> int:
    """Replaces ``destination`` wholesale with the files of ``source`` that
    the zip would ship (same exclusions), so the image never carries tests,
    virtualenvs or tool caches."""
    if destination.exists():
        shutil.rmtree(destination)
    count = 0
    for path in shipped_files(source):
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def require_sidecar_requirements(source: Path) -> None:
    """Both pin files travel (``requirements.txt`` for every image,
    ``requirements-poly.txt`` read only by the ``poly`` layer of the
    Dockerfile) and the copy fails when either is missing."""
    for requirements in REQUIREMENTS_FILES:
        if not (source / requirements).is_file():
            raise SystemExit(f"{source}/{requirements} is missing")


def copy_sidecar(source: Path, destination: Path) -> int:
    require_sidecar_requirements(source)
    return copy_tree(source, destination)


def zip_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="image_zip.py",
        description="Zip an image directory with its Dockerfile at the archive root.",
    )
    parser.add_argument("image_dir", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--variant", choices=VARIANTS, default="full")
    args = parser.parse_args(argv)
    count = write_zip(args.image_dir, args.destination, args.variant)
    print(
        f"{args.destination}: {count} files, {args.destination.stat().st_size} bytes "
        f"(variant {args.variant})"
    )
    return 0


def copy_main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {COPY_USAGE}", file=sys.stderr)
        return 2
    source, destination = Path(argv[0]), Path(argv[1])
    count = copy_sidecar(source, destination)
    print(f"{destination}: {count} files")
    return 0
