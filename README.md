# pta-pipeline

Tooling for self-hosting Wheaton College's Project MUSE post-termination access (PTA) journal content from S3 behind CloudFront, AWS WAF and EZproxy, and loading it into OCLC WMS as a local collection. The full brief and architecture are in [CLAUDE.md](CLAUDE.md).

## Status

| Step | State |
|---|---|
| 1. Listing analysis | Done: [analysis/01_listing_analysis.md](analysis/01_listing_analysis.md) |
| 2. Restore from DEEP_ARCHIVE | **Pilot** (`ab_autobiography_studies/`, 364 objects) restoring. The full job (`6b10cdae…`) is parked in Suspended until the pilot proves out. |
| 3. Convert to GLACIER_IR | Not started |
| 4. Lifecycle | Nothing to do. The bucket policy denies `PutLifecycleConfiguration`, so nothing can transition objects back to Glacier. Old DEEP_ARCHIVE versions are kept as a backup (about $0.75/month). |
| 5. Index pages | Placeholder root `index.html` uploaded |
| 6. Infrastructure | WAF, OAC, CloudFront and bucket policy are live. On-campus access loads the placeholder (verified 2026-11-12). Off-campus access returns 403 from WAF. |
| 7. EZproxy / KBART / WMS / tests | EZproxy stanza being requested from OCLC. |

## Access

- **CloudFront hostname:** `https://d2cfus91tk2xx.cloudfront.net/` (distribution `EWK48TT92QAN`). **Never delete or recreate it**, because the hostname is baked into the KBART and the EZproxy stanza.
- **WAF allow list** (`muse-pta-allow`): `CAMPUS_CIDR` (campus), `VPN_IP/32` (VPN), `EZPROXY_IP/32` (hosted EZproxy `wheatonma.idm.oclc.org`). Everything else is blocked. IPv6 is disabled on the distribution because the allow list is IPv4 only.
- **EZproxy stanza:**
  ```
  Title Project MUSE Post-Termination Access (Wallace Library)
  URL https://d2cfus91tk2xx.cloudfront.net/
  Host d2cfus91tk2xx.cloudfront.net
  ```

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
| WAF IP set `muse-pta-allow`, web ACL `muse-pta-acl` | CloudFront scope (us-east-1). Default action is Block. `infra/waf/` |
| OAC `ETMS8EHK4MXVR`, distribution `EWK48TT92QAN` | `infra/cloudfront/` |
| Bucket policy | Original delete and lifecycle denies kept, plus CloudFront read-only access. `infra/s3/bucket-policy.json` (the original is saved alongside it). |
