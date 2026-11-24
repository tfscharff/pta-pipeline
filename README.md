# pta-pipeline

Tooling for a library to host its own **post-termination access (PTA)** journal content, meaning the back issues it keeps rights to after a subscription ends, and to make that content work in OCLC WorldShare Management Services (WMS) and WorldCat Discovery.

The content sits in a private S3 bucket and is served through CloudFront, AWS WAF and EZproxy. Instead of building a new collection, the pipeline points an existing collection in the WorldCat knowledge base at the hosted copies: it sets coverage to what the bucket actually holds and sends each title's links to a generated journal page. It works for any knowledge base collection whose files you hold.

## How it fits together

```
private S3 bucket  ->  CloudFront (Origin Access Control)  ->  AWS WAF allow list  ->  users
                                                               campus network, VPN, EZproxy
```

- **Nothing is public.** The bucket keeps Block Public Access on. CloudFront is the only reader, and WAF blocks every address that is not on the allow list. Off-campus users come through EZproxy.
- **One page per journal.** Each journal folder gets a generated `index.html` listing its volumes, issues and articles, plus a top-level index of all journals. The pages are static, have no external assets, and follow the reader's light or dark setting.
- **The knowledge base collection links to those pages.** A KBART file sets each title's coverage and URL. An optional link scheme setting makes article links from Discovery open the article itself.

## What you need

- AWS CLI with credentials for the account that owns the bucket
- Python 3.9 or later (`pip install pypdf` only if you use `--pdf`)
- PowerShell 7 for the two Batch Operations scripts
- The content in S3, laid out as `journal/v<volume>/<issue>/<article id>.pdf` and `.html` (see [Parsing profiles](#parsing-profiles) for other layouts)
- A Collection Manager export of the knowledge base collection (UTF-8, tab-delimited)
- Your campus network ranges and the outbound address of your EZproxy server

## Configure

Copy `config.example.json` to `config.json` and fill it in. `config.json` is git-ignored, so deployment details stay out of the repository.

| Key | Meaning |
|---|---|
| `bucket` | S3 bucket that holds the content |
| `ops_bucket` | Private bucket for Batch Operations manifests and reports |
| `batch_role` | IAM role that S3 Batch Operations assumes (`infra/batchops/`) |
| `base_url` | The CloudFront address, `https://<distribution>.cloudfront.net` |
| `listing` | Path of the bucket listing file |
| `profile` | Parsing profile that reads article metadata (default `profiles/citation_meta.py`) |
| `site.*` | Wording on the generated pages: headings, page titles, the footer, and the provider's name |
| `kbart.link_scheme`, `kbart.title_id_template` | Optional article linking (see [Link scheme](#link-scheme)) |
| `kbart.avoid_title_ids` | `title_id` values that mark odd entries in the export to avoid when a title has several |
| `kbart.pilot` | Journal folder written to `pilot_kbart.txt` for a first test upload |

## Steps

Prove the whole chain with one journal before running anything on the full bucket.

### 1. List the bucket

```
aws s3api list-objects-v2 --bucket <bucket> --query "Contents[].[Key,StorageClass,Size]" --output text > listing.txt
```

The listing is the input to everything else. For a large bucket it runs to hundreds of megabytes, so the scripts stream it.

### 2. Restore archived objects

Skip this if nothing is in `GLACIER` or `DEEP_ARCHIVE`. Otherwise build a manifest, upload it to the ops bucket and create the restore job. The job waits in Suspended until you confirm it.

```
python scripts/make_manifest.py --out manifests/restore_all.csv --storage-class DEEP_ARCHIVE
aws s3 cp manifests/restore_all.csv s3://<ops bucket>/manifests/restore_all.csv
./infra/batchops/create-restore-job.ps1
python scripts/check_restore.py --sample 200
```

### 3. Copy to an instant-retrieval class

`GLACIER_IR` suits content that is read rarely but must open immediately. The copy is in place, keeps each object's metadata, and also waits in Suspended.

```
./infra/batchops/create-copy-job.ps1 -ManifestKey manifests/restore_all.csv
```

Pass `-ContentType application/pdf` with a narrower manifest to correct objects stored with the wrong type, for example PDFs saved under `.html` names.

### 4. Build the journal pages

```
python scripts/build_pages.py --all --fetch --upload --tmp <scratch dir>
python scripts/build_pages.py --all --pdf --upload --tmp <scratch dir>
```

The first command reads every article's HTML and builds and uploads the pages. The second fills in articles that have only a PDF. Results are cached under `work/meta/`, so an interrupted run resumes and later rebuilds need no downloads. Use `--slug <journal folder>` instead of `--all` for one journal, and `--reparse-html` to re-read only the articles that still lack a title after you improve a profile.

Pages are uploaded as `STANDARD`, `text/html`, with a five-minute cache lifetime.

### 5. Infrastructure

Create these in order, from the templates in `infra/`. The capitalised placeholders (`BUCKET`, `ACCOUNT_ID`, `OAC_ID` and so on) must be replaced first.

1. **WAF** (CloudFront scope, `us-east-1`): an IP set with the campus ranges, the VPN and the EZproxy address, and a web ACL whose default action is Block (`infra/waf/`).
2. **CloudFront**: an Origin Access Control and a distribution on the bucket's REST endpoint, HTTPS only, GET and HEAD only, IPv6 off if the allow list is IPv4 only (`infra/cloudfront/`).
3. **Bucket policy**: read access for that one distribution, alongside any deny statements you keep to protect the archive (`infra/s3/`).

Use the distribution's default `*.cloudfront.net` hostname unless you control DNS. **Never delete or recreate the distribution** once the KBART is loaded: its hostname is written into every title URL and the EZproxy stanza.

### 6. EZproxy

```
Title Journal archive
URL https://<distribution>.cloudfront.net/
Host <distribution>.cloudfront.net
```

### 7. KBART

Export the collection from Collection Manager, then:

```
python scripts/build_kbart.py --kbart work/<collection export>.txt
```

It matches each journal in the bucket to a title in the export by ISSN, then by title, and writes to `work/kbart/`:

| File | Use |
|---|---|
| `kbart_coverage.txt` | `covoverlay` rows: one per unbroken run of volumes, so gaps show as separate ranges |
| `kbart_overlay.txt` | The same rows as `overlay`, which is what changes `title_url` and linking |
| `pilot_kbart.txt` | The pilot journal only, for a first test |
| `unmatched_s3.tsv`, `local_kbart.txt` | Journals in the bucket with no entry in the collection, and a KBART to load them as a local collection |
| `match_report.tsv` | How every journal matched |

Upload `kbart_coverage.txt` to the collection with **Replace all titles**, then `kbart_overlay.txt` with **Update changes only**. Titles left out of the first file are deselected, so Discovery offers only what you host.

### 8. Test

- On campus, a journal page loads directly.
- Off campus, the direct address returns 403, and the EZproxy address works.
- An S3 address returns 403.
- PDFs open in the browser.
- In Discovery, journal and article links are proxied, coverage is right, and split coverage shows as separate ranges.

## Link scheme

Article links in Discovery do not use `title_url`. They go through OCLC's linker, which builds the address from the collection's link scheme, and that normally points at the provider's site. To send them to your pages:

- Set the scheme **per title**, in the overlay file (`oclc_linkscheme`). Collection Manager accepts a collection-level Local Link Scheme, but the resolver ignored it in our testing. It also rejects a blank scheme and any value missing from OCLC's link scheme list.
- A few listed schemes build their address from the title's `title_id`. With `kbart.link_scheme` set to `aphapublications.org` and `kbart.title_id_template` set to `{host}/{slug}/index.html?x=`, the linker sends article links to
  `https://<host>/<slug>/index.html?x=/action/doSearch?SeriesKey=ajph&Title=<article title>`,
  and to `...?x=/action/quickLink?...&quickLinkYear=&quickLinkVolume=&quickLinkIssue=` when it has no title.
- The journal page reads those parameters. One exact title match opens the article, several are listed, a near match is highlighted, and an issue-only link jumps to the issue.

This depends on OCLC keeping that scheme's address pattern, so test it before relying on it. If it changes, links still land on the journal page. The scheme `atoz` is the plain fallback: it always sends article links to `title_url`.

To test without Discovery's search page, use the resolver directly:

```
https://<institution>.on.worldcat.org/atoztitles/link?genre=article&issn=<issn>&date=<year>&volume=<v>&issue=<n>&spage=<page>&atitle=<title>
```

## Article search

An article-level search box for research guides. Its results come from the WorldCat Discovery central index, filtered to the journals and years in the archive, and each result links to the archived copy.

- `search.html` sits at the root of the archive. It calls `api/search?q=…&offset=…` on the same CloudFront host.
- CloudFront sends `/api/*` to a small AWS Lambda function, `search/relay.py`, so the same WAF allow list and EZproxy apply. The function holds the OCLC key, searches the Discovery API with the limits in `config.json` (`search.db_ids`, `search.item_sub_type`, `search.held_by_institution`), and keeps only articles whose journal (by ISSN, then title) and year are in the archive.
- Each result links to `<slug>/index.html?Title=…&volume=…&issue=…`, which the journal page opens as described above.

Setup:

1. Request a WSKey for the **WorldCat Discovery API**, type machine-to-machine, at https://platform.worldcat.org/wskey/. It needs a WMS or Discovery Premium subscription.
2. Store it as `<key>:<secret>` in an SSM SecureString named by `search.wskey_param` (default `/pta-search/wskey`). Type this in your own terminal, not in a shared session:
   `aws ssm put-parameter --name /pta-search/wskey --type SecureString --value "<key>:<secret>"`
3. Fill in the `search` section of `config.json` and run `python scripts/build_search.py`. It writes `site/search.html`, the Lambda package `work/search/relay.zip` and the research-guide box `work/search/widget.html`.
4. Create the Lambda function `pta-search-relay`: Python 3.13, handler `relay.handler`, timeout 30 seconds, environment `WSKEY_PARAM`, and a role from `infra/search/relay-trust.json` and `relay-policy.json`. Give it a function URL with auth type `AWS_IAM`.
5. In CloudFront, add an Origin Access Control of type `lambda`, the function URL as an origin and an `/api/*` behavior (`infra/search/cloudfront-api.json`). Then allow the distribution to call the function: `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`, principal `cloudfront.amazonaws.com`, source ARN the distribution.
6. Run `python scripts/build_search.py --upload`, set `search.enabled` to true and rebuild the index, which then opens with an article search box (labelled with `search.index_link`) above the journal list.
7. Paste `work/search/widget.html` into a LibGuides Media / Widget box. It opens the search through `search.proxy_login`, so it works on and off campus.

Rebuild and redeploy `relay.zip` when journals are added, because the function carries the archive's journal list.

## Journal page parameters

| Parameter | Effect |
|---|---|
| `?volume=24&issue=1`, `?year=2012` | Jump to the issue |
| `?article=<article id>` | Jump to and highlight one article |
| `?q=<words>` | Filter by title or author |
| `?Title=<article title>` | Open the article with that title |

## Parsing profiles

A profile is a small Python module that turns one article file into metadata. The default, `profiles/citation_meta.py`, reads the `citation_*` meta tags most publishing platforms put in each article page, and the title and author stored in a PDF. Its docstring describes the full contract.

Write your own profile when a provider's files need more: a different folder layout, a cover page on each PDF, titles that are only in the page body, or error pages saved in place of articles. Copy the default, adapt it, and set `profile` in `config.json`. Keeping it under `local/` keeps it out of the repository.

The page builder then handles the rest, including three things that turned out to matter in practice:

- `.html` objects that are really PDFs are detected and read as PDFs.
- Articles a profile marks as `missing` are left off the page and counted in a note.
- An article with no title of its own is titled from the previous and next articles, when a profile supplies those.

## Things worth knowing about WMS

- `covoverlay` changes coverage only. `title_url`, `title_id` and `oclc_linkscheme` need `overlay` rows.
- A KBART upload produces a preview that must be approved in Collection Manager before anything changes.
- Title-level changes reach the resolver within a few minutes.
- Titles cannot be added to a global collection by KBART. Extras need a local collection.
- Exports can contain several entries for one title, and entries that carry another title's OCLC number. The builder prefers an entry whose OCLC number no other title uses, then the one whose coverage overlaps yours most. Report shared numbers to OCLC.
- Discovery's search pages sit behind a bot check, so automated tests should use the resolver address above.

## Tests

```
python tests/smoke_test.py
```

It builds pages and a KBART from made-up articles with the default profile, using only the standard library.

## License

The scripts and templates are under the [MIT License](LICENSE). Journal content is licensed from its publisher and is not part of this repository.
