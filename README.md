# pta-pipeline

Tooling for self-hosting Wheaton College's Project MUSE post-termination access (PTA) journal content from S3 behind CloudFront, AWS WAF and EZproxy, and loading it into OCLC WMS as a local collection. The full brief and architecture are in [CLAUDE.md](CLAUDE.md).

## Status

| Step | State |
|---|---|
| 1. Listing analysis | Done: [analysis/01_listing_analysis.md](analysis/01_listing_analysis.md) |
| 2. Restore from DEEP_ARCHIVE | **Pilot** (`ab_autobiography_studies/`, 364 objects) restored 2026-11-13. **Full job** `6b10cdae…` started 2026-11-13 (Bulk, 7 days). The 364 pilot tasks fail because those objects are already GLACIER_IR. |
| 3. Convert to GLACIER_IR | **Pilot** converted by Batch job `dc5b04f5…` (364/364 succeeded, all verified GLACIER_IR). **Full** copy job `91f2bccc-a627-4f59-a28b-beed900ef6f8` completed 2026-11-14: 2,807,089 succeeded and 7 failed, because those 7 restores hadn't finished yet. Retry job `948b2a65…` (`manifests/copy_retry1.csv`) converted all 7 on 2026-11-15. **Verified 2026-11-15:** a fresh listing shows all 2,807,460 objects in GLACIER_IR, plus the STANDARD `index.html` pages. |
| 4. Lifecycle | Nothing to do. The bucket policy denies `PutLifecycleConfiguration`, so nothing can transition objects back to Glacier. Old DEEP_ARCHIVE versions are kept as a backup (about $0.75/month). They were written 2026-09-21 to 2026-09-24 and DEEP_ARCHIVE bills a 180-day minimum, so deleting them before about 2027-03-23 saves nothing: the remaining days are charged as an early-deletion fee. Delete them after that date. |
| 5. Index pages | **All 727 journal pages** and the top-level index were built by `scripts/build_pages.py` and uploaded 2026-11-15 (STANDARD, `max-age=300`). **PDF pass** (`--pdf`, 2026-11-16) read the MUSE cover page of 137,954 PDF-only articles and re-uploaded every page. 373 of those PDFs are filed by MUSE under a second article ID (the same file is listed under its own ID), and 9 are empty, corrupt or have unreadable metadata. **HTML re-parse** (`--reparse-html`, 2026-11-16) re-read 55,451 articles in MUSE's newer page template (about 2022 on), which the parser had missed: issues lacking a volume/issue/date designation fell from 3,154 to 135. **Untitled clean-up** (2026-11-16): the first parser stored the placeholder "Untitled" for 56,883 HTML articles and did not count them. Re-reading them recovered almost all: older pages (roughly pre-2005) carry the title only in `citation_*` meta tags; 4,395 `.html` objects are really the article PDF (most early-release articles and much of Language; listed in `manifests/pdf_as_html.csv`; Batch job `952472ac…` re-copied them in place with `Content-Type: application/pdf` on 2026-11-17, 4,395 succeeded, sample verified, and one opened as a PDF through EZproxy); reviews with no title on their own page take it from the PDF cover or from the previous/next article named on neighbouring pages (323 titles). 1,680 articles are MUSE error pages with nothing behind them (968 in Missouri Review); they are left off the pages, with a count in the page header, and no volume's coverage depended on them. **Now 671,809 articles are listed and 648 have no title** (406 of them PDFs with the problems above). Only the top index and the pilot page are kept in git; S3 holds the rest. |
| 6. Infrastructure | WAF, OAC, CloudFront and bucket policy are live. On-campus access loads the placeholder (verified 2026-11-12). Off-campus access returns 403 from WAF. |
| 7. EZproxy / KBART / WMS / tests | EZproxy stanza is in OCLC's hosted config. Off-campus access through EZproxy works, and direct off-campus access returns 403 (both verified 2026-11-16). **KBART built 2026-11-16** from the WMS export of Muse.MuseAll (865 titles, 1,134 entries): 726 of 727 S3 journals matched by ISSN, 804 `covoverlay` rows (49 titles split at gaps), every row dated (35 rows with years inferred from a one-volume-a-year pattern). Leaving every other entry out of the file deselects it, so Discovery offers only what we host. Newman Studies Journal is not in Muse.MuseAll; OCLC was asked to add it (email sent 2026-11-17), with a local collection (`local_kbart.txt`) as the fallback. **Uploaded 2026-11-16** with "Replace all titles" (408 titles deselected, 710 updated). A second pass fixed JGSS and Restoration, which had been matched to entries carrying another title's OCN (OCLC was told about the 18 shared OCNs). `covoverlay` changes coverage only, so `muse_pta_kbart_overlay.txt` (the same rows with `ACTION=overlay`) was uploaded with "Update changes only" to set `title_url` (726 titles updated). Split coverage survived the overlay pass: Language shows 8 rows and Minnesota Review 7 (verified 2026-11-16). The collection's Local Collection URL Formula was set to `[[localstem]]https://d2cfus91tk2xx.cloudfront.net/index.html` (2026-11-16); **Article links (2026-11-16):** article results in Discovery go through `linker2.worldcat.org`, and the collection's `muse` link scheme sent them to a muse.jhu.edu title search. WMS won't take a blank or unlisted link scheme, and a collection-level Local Link Scheme is ignored by the resolver, so the override is set per title by overlay KBART. All 650 values in OCLC's link scheme spreadsheet were tested against the linker with a real article link. A few build the URL from the title's `title_id` (the linker's `jKey`). With `oclc_linkscheme=aphapublications.org` and `title_id=d2cfus91tk2xx.cloudfront.net/<slug>/index.html?x=`, the linker redirects to `https://d2cfus91tk2xx.cloudfront.net/<slug>/index.html?x=/action/doSearch?SeriesKey=ajph&Title=<article title>`, and to `...?x=/action/quickLink?...&quickLinkYear=&quickLinkVolume=&quickLinkIssue=` when it has no article title. The journal page reads those parameters: one exact title match opens the article, several are listed, a near match is highlighted, and an issue-only link jumps to the issue. Uploaded for all 726 titles (preview: 0 invalid); the resolver picks up title-level changes within about five minutes. Verified in Discovery 2026-11-16: an article result in Journal of Early Christian Studies opened its PDF. The link depends on OCLC keeping that scheme's URL pattern; if it changes, links still land on the journal page. `atoz` is the fallback scheme: it always redirects to the plain `title_url`. |

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

After the restore completes, create the in-place GLACIER_IR copy job. It also waits in Suspended until confirmed, and it copies metadata (Content-Type) unchanged unless `-ContentType` is given:

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

Build the WMS KBART from a Collection Manager export of Muse.MuseAll (UTF-8 tab-delimited). It writes `work/kbart/muse_pta_kbart.txt` (one `covoverlay` row per unbroken run of volumes, `title_url` pointing at the journal's CloudFront page), `muse_pta_kbart_overlay.txt` (the same rows as `overlay` with the article-linking `oclc_linkscheme` and `title_id`, because `covoverlay` doesn't change `title_url` or linking), `pilot_kbart.txt`, `match_report.tsv` and `unmatched_s3.tsv`. Upload the `covoverlay` file first with "Replace all titles", then the `overlay` file with "Update changes only":

```
python scripts/build_kbart.py --kbart work/6569_Muse.MuseAll_kbart.txt
```

Journal pages accept `?volume=24&issue=1`, `?year=2012`, `?article=<muse id>` (jump to and highlight) and `?q=<words>` (filter). `?Title=<article title>` opens the article with that title (this is what Discovery article links send).

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
