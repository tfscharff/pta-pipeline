"""Spot-check Glacier restore progress by sampling objects with head-object.

Reports how many sampled objects are not yet requested, in progress
(ongoing-request="true"), or restored (ongoing-request="false", with an expiry date).
Uses the AWS CLI so no extra Python packages are needed.

  python scripts/check_restore.py --sample 200
"""
import argparse
import json
import random
import subprocess
import sys
from collections import Counter

from common import load_config, need


def head(bucket: str, key: str) -> dict:
    out = subprocess.run(
        ["aws", "s3api", "head-object", "--bucket", bucket, "--key", key, "--output", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return {"_error": out.stderr.strip()}
    return json.loads(out.stdout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="configuration file (default: config.json in the repository root)")
    ap.add_argument("--listing", help="bucket listing (default: \"listing\" in the config)")
    ap.add_argument("--bucket", help="content bucket (default: \"bucket\" in the config)")
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()
    cfg = load_config(args.config)
    args.listing = args.listing or cfg["listing"]
    args.bucket = args.bucket or need(cfg, "bucket")

    # Reservoir sample so a large listing is streamed, not loaded.
    rng = random.Random(args.seed)
    sample: list[str] = []
    with open(args.listing, encoding="utf-8") as f:
        for i, line in enumerate(f):
            key = line.split("\t", 1)[0]
            if i < args.sample:
                sample.append(key)
            else:
                j = rng.randint(0, i)
                if j < args.sample:
                    sample[j] = key

    states: Counter[str] = Counter()
    expiries: Counter[str] = Counter()
    for key in sample:
        h = head(args.bucket, key)
        if "_error" in h:
            states["error"] += 1
            print(f"ERROR {key}: {h['_error']}", file=sys.stderr)
            continue
        restore = h.get("Restore")
        if restore is None:
            states["not requested"] += 1
        elif 'ongoing-request="true"' in restore:
            states["in progress"] += 1
        else:
            states["restored"] += 1
            if "expiry-date=" in restore:
                expiries[restore.split('expiry-date="', 1)[1].split('"', 1)[0][:16]] += 1

    n = len(sample)
    for state in ("not requested", "in progress", "restored", "error"):
        print(f"{state:>14}: {states[state]:5d}  ({100 * states[state] / n:.0f}%)")
    if expiries:
        print("earliest expiry:", min(expiries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
