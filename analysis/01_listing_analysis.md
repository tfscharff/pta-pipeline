# Listing analysis: `project.muse` (us-east-1)

Source: `muse_listing.txt`, 2026-11-11 (2,807,460 objects, 204 MB listing).

## Totals

| | Objects | Size |
|---|---:|---:|
| All objects | 2,807,460 | 754.6 GB |
| **Storage class**: `DEEP_ARCHIVE` (100%) | 2,807,460 | 754.6 GB |

Because everything is DEEP_ARCHIVE, every object must be restored (Bulk tier, up to 48 h) before it can be copied to GLACIER_IR.

### By file type

| Ext | Objects | GB | Notes |
|---|---:|---:|---|
| pdf | 646,510 | 403.1 | article PDFs |
| html | 961,374 | 32.1 | article full-text HTML, plus figure/table popup pages |
| jpg | 839,104 | 94.8 | figures (`full/`, `thumb/`) |
| gif | 351,272 | 1.9 | inline images (`inline/`) |
| mp4 / mpeg / quicktime / x-m4v / x-msvideo | 3,093 | 198.7 | video supplements |
| wav / ogg | 142 | 8.7 | audio supplements |
| x-download | 606 | 7.8 | supplements saved with a MIME-derived suffix, e.g. `*.docx.x-download` |
| x-zip-compressed | 117 | 6.9 | zip supplements |
| txt | 5,058 | <0.01 | 24-byte stubs (figure placeholders) |
| png, tif, tiff, gz, rtf, excel | 184 | 0.8 | miscellaneous |

Several extensions are MIME subtypes rather than real file extensions (`excel`, `quicktime`, `x-download`, `x-m4v`, `x-zip-compressed`, `x-msvideo`). They need explicit content types and, ideally, a `Content-Disposition` filename at conversion time.

## Structure

```
<journal_slug>/v<volume>/<muse_issue_id>/<muse_article_id>.pdf|.html
<journal_slug>/v<volume>/<muse_issue_id>/{full,thumb,inline,file}/<article_id>_<asset>.<ext>
```

- **727 journals**, **14,419 volumes**, **43,230 issues**.
- The issue folder is MUSE's internal **issue ID**, not the issue number. For example, `21192` is `muse.jhu.edu/issue/21192`. Article filenames are MUSE article IDs.
- Article availability at the article level:
  - 502,991 articles have both a PDF and HTML.
  - 138,344 are PDF only.
  - **27,104 are HTML only.** These have no PDF, so their HTML and image assets must be served too, or they're unreadable.

## Things that don't map cleanly

1. **The issue number and date aren't in the path.** Coverage (`num_first_issue_online`, dates) and readable index pages need issue metadata such as number, season and year. The KBART doesn't provide it at issue level. Options:
   - (a) Parse `citation_*` meta tags from the article HTML once it's restored. This covers about 80% of articles.
   - (b) Extract it from the PDF cover pages.
   - (c) Look it up on muse.jhu.edu by issue ID.

   Recommended: (a), with (b) or (c) for gaps.
2. **Year-numbered volumes.** Folders like `v1995` or `v2003` (41,453 objects) appear in Brookings Papers, *Ab Imperio*, *American Literary Scholarship* and others. The volume is the year, which is fine for KBART.
3. **Five-digit volume folders** (137 objects, e.g. `essays_in_medieval_studies`) need a manual look.
4. **`preprint/` folders** (4,352 objects, about 35 journals) hold articles not yet assigned to an issue. They belong on the index pages under "Preprints" but stay out of KBART coverage.
5. **`language/` adds an extra level** (`issue/<article_id>/full/...`). That accounts for the depth-6 paths and 113 PDFs one level deeper.
6. **Supplements** (`file/`, video, audio, zip) need correct content types. They make up about 220 GB, of which 190 GB is video.

## Rough cost (approximate us-east-1 list prices; confirm in the AWS calculator)

| Step | Basis | Approx. |
|---|---|---:|
| Deep Archive Bulk restore requests | 2.81M × $0.025/1k | ~$70 |
| Bulk retrieval data | 755 GB × $0.0025 | ~$2 |
| Restored temp copies, 7 days (billed as Standard) | 755 GB × $0.023 × 7/30 | ~$4 |
| S3 Batch Operations (restore + copy jobs) | $0.25/job + $1/M objects | ~$7 |
| Copy into GLACIER_IR (PUT) | 2.81M × $0.02/1k | ~$56 |
| GLACIER_IR storage, ongoing | 755 GB × $0.004/mo | ~$3/mo |

At 2.8M objects, request fees are the main cost, so per-object CLI loops would be slow and fragile. **S3 Batch Operations with CSV manifests is the right tool for both the restore and the copy.** The bucket was created 2026-05-14, so the 180-day Deep Archive minimum ends about 2026-11-10. In-place copies should no longer incur early-deletion fees, but check each object's upload date before converting.
