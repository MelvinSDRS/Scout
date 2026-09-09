"""Local reference folders, validated before exposing a photo-check profile."""

import hashlib
import io
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image

PROFILE_ID = r"[a-z0-9][a-z0-9_-]{0,63}"


@dataclass(frozen=True)
class ImageProfile:
    id: str
    directory: Path
    fingerprint: str


@lru_cache(maxsize=64)
def _fingerprint(files):
    digest = hashlib.sha256()
    for name, _, _ in files:
        data = Path(name).read_bytes()
        if len(data) > 2_000_000:
            raise ValueError("Reference exceeds byte limit")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or image.width * image.height > 4_000_000:
                raise ValueError("References must be JPEGs of at most four million pixels")
            image.verify()
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def get_profile(root, ident):
    if not ident or not re.fullmatch(PROFILE_ID, ident):
        return None
    directory = root / ident
    try:
        files = sorted(directory.glob("*.jpg"))
        if not 1 <= len(files) <= 5:
            return None
        stamps = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
        if any(size > 2_000_000 for _, _, size in stamps):
            return None
        fingerprint = _fingerprint(stamps)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return ImageProfile(ident, directory, f"sift-v1:{ident}:{fingerprint}")


def available_profiles(root):
    try:
        directories = sorted(root.iterdir())
    except OSError:
        return []
    return [
        {"id": p.name, "label": p.name.replace("-", " ").replace("_", " ").title()}
        for p in directories
        if p.is_dir() and get_profile(root, p.name) is not None
    ]
