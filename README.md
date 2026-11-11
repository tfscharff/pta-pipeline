# pta-pipeline

Tooling for self-hosting Wheaton College's Project MUSE post-termination access (PTA) journal content from S3 behind CloudFront, AWS WAF and EZproxy, and loading it into OCLC WMS as a local collection. The full brief and architecture are in [CLAUDE.md](CLAUDE.md).

## Status

| Step | State |
|---|---|
| 1. Listing analysis | Done: [analysis/01_listing_analysis.md](analysis/01_listing_analysis.md) |
| 2. Restore from DEEP_ARCHIVE | Batch Operations job created |
| 3. Convert to GLACIER_IR | Not started |
| 4. Lifecycle | Not started (bucket has no lifecycle; versioning is **enabled**) |
| 5. Index pages | Not started |
| 6. Infrastructure | Not started |
| 7. EZproxy / KBART / WMS / tests | Not started |

## Usage

Regenerate the listing (git ignores it because it's about 200 MB):

```
aws s3api list-objects-v2 --bucket project.muse --query "Contents[].[Key,StorageClass,Size]" --output text > muse_listing.txt
```

Build a Batch Operations manifest (`bucket,url-encoded-key`):

```
python scripts/make_manifest.py --out manifests/restore_all.csv --storage-class DEEP_ARCHIVE
```

Create the restore job. It waits in Suspended until confirmed:

```
./infra/batchops/create-restore-job.ps1
```

Check job status and restore progress:

```
aws s3control describe-job --account-id <acct> --job-id <id> --query "Job.[Status,ProgressSummary]"
python scripts/check_restore.py --sample 200
```

## AWS resources

| Resource | Purpose |
|---|---|
| `s3://project.muse` | Content. Private, versioned, SSE-S3. |
| `s3://wheaton-muse-pta-ops` | Private bucket for Batch Operations manifests and reports (later also CloudFront logs). |
| IAM role `muse-pta-batchops` | Role assumed by S3 Batch Operations. Inline policy in `infra/batchops/policy.json`. |
