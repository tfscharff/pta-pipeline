# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Self-host Project MUSE **post-termination access (PTA)** journal PDFs for Wheaton College's Madeleine Clark Wallace Library (Norton, MA) and expose them through OCLC WorldShare Management Services (WMS) / WorldCat Discovery, with EZproxy for off-campus users. Portico is not used.

**License constraint:** the content may be hosted locally for authorized users only. Nothing may ever be publicly reachable: no public bucket, no S3 website endpoint, no public ACLs.

## Environment

- Windows 11, PowerShell 7. AWS CLI is available.
- Scripts must run on Windows: Python or PowerShell, never bash-only.

## Inputs (supplied by the user; do not invent values)

1. `muse_listing.txt`: tab-separated `Key  StorageClass  Size`, produced by
   `aws s3api list-objects-v2 --bucket BUCKET --query "Contents[].[Key,StorageClass,Size]" --output text > muse_listing.txt`
2. The MUSE KBART file for the cancelled journal collection.
3. Campus CIDRs and EZproxy outbound IP(s).

## Known facts (from `analysis/01_listing_analysis.md`)

- The bucket is `project.muse` in us-east-1. It holds 2,807,460 objects (754.6 GB), all **DEEP_ARCHIVE**.
- Key layout: `<journal_slug>/v<volume>/<muse_issue_id>/<muse_article_id>.{pdf,html}`. Assets sit under `{full,thumb,inline,file}/`. `language/` adds an extra article folder.
- The issue folder is MUSE's internal issue ID, **not** the issue number. Issue number and date must come from the HTML `citation_*` meta tags, the PDFs, or muse.jhu.edu.
- `muse_listing.txt` is about 200 MB. Process it with streaming tools (awk or Python), never by reading the whole file.
- At this object count, use **S3 Batch Operations** with CSV manifests for the restore and copy. Don't loop over objects with the CLI.
- **The bucket policy denies** object and version deletes, bucket deletion, `PutLifecycleConfiguration` and `PutBucketVersioning`. Never remove those statements. Old DEEP_ARCHIVE versions left by in-place copies are kept **until about 2027-03-23** (written 2026-09-21 to 09-24; the 180-day minimum is billed whether or not they are deleted, so earlier deletion saves nothing). The user will then delete them, which requires temporarily lifting the `DenyDeletingArchiveContent` statement for `s3:DeleteObjectVersion`. That deletion is irreversible, so confirm with the user first.
- **Live access layer:** distribution `EWK48TT92QAN` (`d2cfus91tk2xx.cloudfront.net`), WAF `muse-pta-acl` / IP set `muse-pta-allow`, OAC `ETMS8EHK4MXVR`. IPv6 stays off. Details are in README.md.
- **Hosted EZproxy** is `wheatonma.idm.oclc.org` (`EZPROXY_IP`). It is OCLC-hosted, so stanza changes go through OCLC's hosted config.
- **WMS target collection** is `Muse.MuseAll` (Project Muse All Journals, global link scheme `muse`). We produce a KBART with `covoverlay` coverage rows and `overlay` rows that set `title_url`, `oclc_linkscheme=aphapublications.org` and `title_id=<host>/<slug>/index.html?x=`. With those, OCLC's linker sends article links to our journal page with the article title in `Title=`, and the page script opens the article (details in README.md). WMS rejects blank or unlisted link schemes, and only title-level values take effect in the resolver. KBART uploads produce a preview that must be approved in Collection Manager. Discovery's search pages sit behind a Cloudflare bot check, so test links through the resolver (`/atoztitles/link?genre=article&issn=...`) and with curl against `linker2.worldcat.org`. Titles can't be added to a global collection by KBART, so extras need a local collection.
- **Pilot first:** prove the whole chain with `ab_autobiography_studies` before confirming the full restore job (`6b10cdae-fc86-46d0-85b0-d6ab8581ebd6`, parked in Suspended).

## Settled architecture decisions (don't re-litigate)

- **Storage:** PDFs go to `GLACIER_IR` (expected use of a few thousand downloads a year is far below the break-even point against Standard). HTML index pages stay in `STANDARD`.
- **Access path:** private bucket (Block Public Access ON) → CloudFront with **Origin Access Control** on the **REST** endpoint → **AWS WAF** web ACL (CloudFront scope, us-east-1) that allows an IP set of campus CIDRs plus EZproxy outbound IPs, default action Block → EZproxy stanza for the CloudFront hostname → WMS local collection with proxying on.
- **CloudFront:** HTTPS redirect, GET/HEAD only, CachingOptimized policy, PriceClass_100. Optional standard logging to `wheaton-muse-pta-ops`.
- **Default hostname.** There is no custom hostname, ACM cert or DNS record. Use the distribution's default `*.cloudfront.net` hostname and default certificate. Never delete or recreate the distribution, because that changes the hostname and breaks every KBART link and the EZproxy stanza. (Fallback if a readable name is ever wanted: a domain registered in Route 53.)
- **One page per journal:** each journal gets a generated `index.html` listing its volumes, issues and articles, with relative PDF links. Article links from Discovery land on this page with the article title, and its script opens the article. KBART `title_url` must point explicitly at `.../index.html`, because S3 REST origins don't resolve directory indexes.

## Pipeline order

1. Analyze the listing: storage-class counts, total size, the inferred journal → volume → issue → article structure, and any keys that don't map cleanly.
2. Restore, only if objects are GLACIER or DEEP_ARCHIVE: Bulk tier, Days=7. The script must be idempotent and resumable and must log progress. Check status with head-object `Restore: ongoing-request`. For large object counts, use an S3 Batch Operations manifest CSV.
3. Convert in place to GLACIER_IR with `--force-glacier-transfer`. For PDFs, also pass `--metadata-directive REPLACE --content-type application/pdf`. Give non-PDFs their correct content types. Afterward, verify that every object is GLACIER_IR.
4. Check the lifecycle (`get-bucket-lifecycle-configuration`) and change any rule that would move objects back to Glacier.
5. Generate the index pages: static, accessible HTML with no external assets, one per journal (title, ISSNs, publisher, volumes/issues/articles) plus a top-level index. Upload them with `text/html; charset=utf-8`, STANDARD class.
6. Infrastructure, in this order: WAF → CloudFront → bucket policy.
7. EZproxy stanza, using the distribution's hostname:
   ```
   Title Project MUSE Post-Termination Access (Wallace Library)
   URL https://<dist>.cloudfront.net/
   Host <dist>.cloudfront.net
   ```
8. Build the WMS KBART for a Collection Manager local collection by joining the MUSE KBART to the actual S3 holdings:
   - Fields: standard journal KBART fields plus `oclc_number` where available, with `coverage_depth=fulltext` and `publication_type=serial`.
   - Coverage must reflect what is actually in S3. Split a title into multiple rows where its run has gaps.
   - Report titles in S3 that are missing from the MUSE KBART, and the reverse.
9. Test:
   - on-campus access loads;
   - off-campus direct access returns 403;
   - a direct S3 URL returns 403;
   - the EZproxy login URL works;
   - PDFs open inline;
   - Discovery links are proxied and show the correct coverage.

## Operating rules

- Work autonomously. Stop and confirm only before costly or hard-to-undo AWS mutations:
  - Glacier restore requests (they incur cost);
  - in-place storage-class copies (GLACIER_IR has a 90-day minimum storage charge);
  - lifecycle or bucket-policy changes;
  - Block Public Access changes;
  - creating or deleting CloudFront or WAF resources.
- Read-only AWS calls (`list-*`, `get-*`, `head-object`) need no confirmation.
- Before running a mutating script against the real bucket, offer a dry-run mode or a small test prefix first.
