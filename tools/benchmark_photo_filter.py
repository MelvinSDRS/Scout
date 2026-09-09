"""Replay locally archived alerts through the exact resource-limited photo process."""

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

from scout.models import Listing, Watch, matches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "archive",
        type=Path,
        help="Private archive with updated-watch.json, alerts.json and <alert>.jpg files",
    )
    parser.add_argument("references", type=Path, help="Folder with 1–5 reference JPEGs")
    args = parser.parse_args()
    root, references = args.archive, args.references
    watch = Watch.model_validate_json((root / "updated-watch.json").read_text())
    rows = json.loads((root / "alerts.json").read_text())
    results = []
    started = time.monotonic()
    env = {
        **os.environ,
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    for row in rows:
        item = Listing(**{k: row[k] for k in Listing.__dataclass_fields__ if k in row})
        run = subprocess.run(
            [sys.executable, "-m", "scout.image_match", str(references)],
            input=(root / f"{row['alert']}.jpg").read_bytes(),
            capture_output=True,
            env=env,
            timeout=4,
            check=True,
        )
        score = json.loads(run.stdout)
        decision = score["decision"] if matches(watch, item) else "keyword-excluded"
        results.append(
            {
                "alert": row["alert"],
                "id": item.id,
                "title": item.title,
                "decision": decision,
                **{"photo_score": score},
            }
        )
    summary = {
        "images": len(rows),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "child_peak_rss_mb": round(
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1
        ),
        "decisions": {
            d: sum(r["decision"] == d for r in results)
            for d in ["match", "review", "keyword-excluded"]
        },
    }
    (root / "replay.json").write_text(
        json.dumps({"summary": summary, "results": results}, indent=2)
    )
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
