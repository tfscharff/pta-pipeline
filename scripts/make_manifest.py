"""Build S3 Batch Operations CSV manifests from the bucket listing.

Listing format (tab-separated): Key, StorageClass, Size
Manifest format: bucket,url-encoded-key  (no header, as Batch Operations requires)

Examples:
  python scripts/make_manifest.py --out manifests/restore_all.csv --storage-class DEEP_ARCHIVE
  python scripts/make_manifest.py --out manifests/pdf.csv --ext pdf
"""
import argparse
import os
import sys
from urllib.parse import quote

from common import load_config, need


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="configuration file (default: config.json in the repository root)")
    ap.add_argument("--listing", help="bucket listing (default: \"listing\" in the config)")
    ap.add_argument("--bucket", help="content bucket (default: \"bucket\" in the config)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--storage-class", help="only include objects in this storage class")
    ap.add_argument("--ext", action="append", help="only include keys with this extension (repeatable)")
    ap.add_argument("--exclude-ext", action="append", help="exclude keys with this extension (repeatable)")
    ap.add_argument("--prefix", help="only include keys starting with this prefix")
    args = ap.parse_args()
    cfg = load_config(args.config)
    args.listing = args.listing or cfg["listing"]
    args.bucket = args.bucket or need(cfg, "bucket")

    exts = {e.lower().lstrip(".") for e in args.ext or []}
    excl = {e.lower().lstrip(".") for e in args.exclude_ext or []}

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    n = total = 0
    with open(args.listing, encoding="utf-8") as src, open(args.out, "w", encoding="utf-8", newline="\n") as dst:
        for line_no, line in enumerate(src, 1):
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) != 3:
                print(f"skip malformed line {line_no}: {line!r}", file=sys.stderr)
                continue
            key, sc, size = parts
            if args.storage_class and sc != args.storage_class:
                continue
            if args.prefix and not key.startswith(args.prefix):
                continue
            ext = key.rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else ""
            if exts and ext not in exts:
                continue
            if ext in excl:
                continue
            dst.write(f"{args.bucket},{quote(key, safe='/')}\n")
            n += 1
            total += int(size)
    print(f"{n} objects, {total / 1e9:.2f} GB -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
