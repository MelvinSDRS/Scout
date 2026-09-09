"""Bounded, isolated image comparison. No neural model or GPU required."""

import io
import json
import os
import sys


def compare(data, reference_dir):
    from pathlib import Path

    import cv2
    import numpy as np
    from PIL import Image

    cv2.setNumThreads(1)
    cv2.setRNGSeed(0)
    Image.MAX_IMAGE_PIXELS = 4_000_000
    detector = cv2.SIFT_create(nfeatures=400)

    def features(raw):
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 4_000_000:
                raise ValueError("Image exceeds pixel limit")
            image.thumbnail((320, 320))
            image = image.convert("RGB").resize((320, 320))
            grey = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
        return detector.detectAndCompute(grey, None)

    points, descriptors = features(data)
    best = {"inliers": 0, "coverage": 0.0, "good": 0}
    refs = sorted(Path(reference_dir).glob("*.jpg"))
    if not refs or len(refs) > 5:
        raise ValueError("Expected 1–5 reference images")
    if descriptors is not None:
        for path in refs:
            rp, rd = features(path.read_bytes())
            if rd is None:
                continue
            pairs = cv2.BFMatcher().knnMatch(rd, descriptors, k=2)
            good = [
                m
                for pair in pairs
                if len(pair) == 2
                for m, n in [pair]
                if m.distance < 0.7 * n.distance
            ]
            if len(good) < 4:
                continue
            a = np.float32([rp[m.queryIdx].pt for m in good])
            b = np.float32([points[m.trainIdx].pt for m in good])
            _, mask = cv2.findHomography(a, b, cv2.RANSAC, 4, maxIters=500)
            if mask is None:
                continue
            positions = b[mask.ravel().astype(bool)]
            coverage = float(np.prod(np.ptp(positions, axis=0)) / 102400) if len(positions) else 0
            if len(positions) > best["inliers"]:
                best = {"inliers": len(positions), "coverage": coverage, "good": len(good)}
    best["decision"] = "match" if best["inliers"] >= 12 and best["coverage"] >= 0.08 else "review"
    return best


def main():
    import resource

    os.nice(10)
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (2, 3))
    data = sys.stdin.buffer.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Image exceeds byte limit")
    print(json.dumps(compare(data, sys.argv[1])))


if __name__ == "__main__":
    main()
