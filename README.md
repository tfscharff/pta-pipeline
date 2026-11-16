# pta-pipeline

Tooling for self-hosting Wheaton College's Project MUSE post-termination access (PTA) journal content from S3 behind CloudFront, AWS WAF and EZproxy, and loading it into OCLC WMS as a local collection. The full brief and architecture are in [CLAUDE.md](CLAUDE.md).

## Status

| Step | State |
|---|---|
| 1. Listing analysis | Done: [analysis/01_listing_analysis.md](analysis/01_listing_analysis.md) |
| 2. Restore from DEEP_ARCHIVE | **Pilot** (`ab_autobiography_studies/`, 364 objects) restored 2026-11-13. **Full job** `6b10cdae…` started 2026-11-13 (Bulk, 7 days). The 364 pilot tasks fail because those objects are already GLACIER_IR. |
| 3. Convert to GLACIER_IR | **Pilot** converted by Batch job `dc5b04f5…` (364/364 succeeded, all verified GLACIER_IR). **Full** copy job `91f2bccc-a627-4f59-a28b-beed900ef6f8` completed 2026-11-14: 2,807,089 succeeded and 7 failed, because those 7 restores hadn't finished yet. Retry job `948b2a65…` (`manifests/copy_retry1.csv`) converted all 7 on 2026-11-15. **Verified 2026-11-15:** a fresh listing shows all 2,807,460 objects in GLACIER_IR, plus the STANDARD `index.html` pages. |
| 4. Lifecycle | Nothing to do. The bucket policy denies `PutLifecycleConfiguration`, so nothing can transition objects back to Glacier. Old DEEP_ARCHIVE versions are kept as a backup (about $0.75/month). |
| 5. Index pages | **All 727 journal pages** and the top-level index were built by `scripts/build_pages.py` and uploaded 2026-11-15 (STANDARD, `max-age=300`). **PDF pass** (`--pdf`, 2026-11-16) read the MUSE cover page of 137,954 PDF-only articles and re-uploaded every page. 673,489 articles in all. Only 382 remain untitled: 373 are PDFs MUSE filed under a second article ID (the same file is listed under its own ID), and 9 PDFs are empty, corrupt or have unreadable metadata. 3,154 issues still lack a volume/issue/date designation (down from 10,571). Only the top index and the pilot page are kept in git; S3 holds the rest. |
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

After the restore completes, create the in-place GLACIER_IR copy job. It also waits in Suspended until confirmed, and it copies metadata (Content-Type) unchanged:

```
./infra/batchops/create-copy-job.ps1 -ManifestKey manifests/restore_all.csv
```

Build journal pages from the article HTML (the issue folder is MUSE's internal ID, so volume, issue, year, title, authors and pages come from each article's HTML), then upload them:

```
aws s3 sync s3://project.muse/<slug>/ work/<slug>/ --exclude "*" --include "*.html"
python scripts/build_pages.py --slug <slug>
aws s3 cp site/<slug>/index.html s3://project.muse/<slug>/index.html --content-type "text/html; charset=utf-8" --cache-control "max-age=300"
```

Fill in PDF-only articles from each PDF's cover page (`pip install pypdf` first). Results are cached in `work/meta/<slug>.pdf.json`, so a re-run only fetches journals without a cache:

```
python scripts/build_pages.py --all --pdf --upload --tmp <scratch dir>
```

Journal pages accept `?volume=24&issue=1`, `?year=2012`, `?article=<muse id>` (jump to and highlight) and `?q=<words>` (filter).

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
