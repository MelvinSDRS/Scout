"""Check source/wheel artifacts for private files, missing notices and secrets."""

import argparse
import tarfile
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

from check_secrets import private_path, scan_paths


def validate_members(members, wheel):
    for name in members:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or private_path(name):
            raise ValueError(f"Unsafe or private release path: {name}")
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ValueError(f"Reference/screenshot images must not be published: {name}")
    metadata_names = [n for n in members if n.endswith("/METADATA") or n == "PKG-INFO"]
    if len(metadata_names) != 1:
        raise ValueError("Expected one package metadata file")
    metadata = Parser().parsestr(members[metadata_names[0]].decode())
    if metadata["Name"] != "scout" or metadata["License-Expression"] != "MIT":
        raise ValueError("Package identity or SPDX license metadata is missing")
    if metadata["Description-Content-Type"] != "text/markdown":
        raise ValueError("README metadata is missing")
    prefix = metadata_names[0].rsplit("/", 1)[0] + "/licenses/" if wheel else ""
    for notice in ("LICENSE", "docs/nationwide-LICENSE.txt"):
        name = prefix + notice
        if name not in members or b"MIT License" not in members[name]:
            raise ValueError(f"Missing license notice: {name}")
    resource_prefix = "scout/" if wheel else "src/scout/"
    for resource in ("app.js", "index.html", "style.css", "regions.json"):
        if resource_prefix + resource not in members:
            raise ValueError(f"Missing runtime resource: {resource}")


def check_artifact(path):
    wheel = path.suffix == ".whl"
    if wheel:
        with zipfile.ZipFile(path) as archive:
            members = {n: archive.read(n) for n in archive.namelist() if not n.endswith("/")}
    else:
        with tarfile.open(path) as archive:
            members = {}
            for entry in archive:
                if entry.isdir():
                    continue
                if not entry.isfile():
                    raise ValueError(f"Release contains a link or special file: {entry.name}")
                name = str(PurePosixPath(*PurePosixPath(entry.name).parts[1:]))
                members[name] = archive.extractfile(entry).read()
    validate_members(members, wheel)
    with tempfile.TemporaryDirectory(prefix="scout-release-check-") as folder:
        paths = []
        for name, data in members.items():
            dest = Path(folder) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            paths.append(dest)
        if not scan_paths(paths):
            raise ValueError("Release contains potential secrets")
    print(f"Release passed: {path.name} ({len(members)} files)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    wheels = list(args.directory.glob("*.whl"))
    sources = list(args.directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        parser.error("Use a clean output directory containing one wheel and one source archive")
    for path in wheels + sources:
        check_artifact(path)


if __name__ == "__main__":
    main()
