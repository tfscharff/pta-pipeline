# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A generic pipeline for a library to self-host post-termination access (PTA) journal content from a private S3 bucket behind CloudFront, AWS WAF and EZproxy, and to point an existing collection in the WorldCat knowledge base (OCLC WMS / WorldCat Discovery) at it. README.md is the user-facing guide to the steps.

**License constraint:** hosted content is for authorized users only. Nothing may ever be publicly reachable: no public bucket, no S3 website endpoint, no public ACLs.

## This repository is generic. Deployment details are local.

Tracked files must not name a provider, an institution, a bucket, a hostname, an AWS account number, an IP address or a resource ID. Those live in git-ignored files:

- `config.json`: bucket names, CloudFront address, page wording, KBART options (template: `config.example.json`).
- `local/`: the deployment's own parsing profile, deployment log and infrastructure records.
- `CLAUDE.local.md`: deployment-specific facts and rules. **Read it first if it exists.**

Before committing, check that `git grep -i` finds none of the deployment's names in tracked files, and keep them out of commit messages too.

## Commands

```
python tests/smoke_test.py                                  # end-to-end check with made-up data, standard library only
python scripts/build_pages.py --slug <journal folder>       # rebuild one journal page from the cache
python scripts/build_pages.py --all --fetch --upload --tmp <dir>
python scripts/build_kbart.py --kbart work/<export>.txt
```

On Windows use `python3` from Git Bash. Scripts must run on Windows: Python or PowerShell, never bash-only.

## Architecture

- `scripts/common.py`: config loader (`load_config`, defaults in `DEFAULTS`), profile loader, text helpers. Every script reads `config.json` from the repository root unless `--config` is given.
- `scripts/build_pages.py`: bucket listing → holdings → article metadata → one static page per journal plus a top index. It caches parsed metadata in `work/meta/<slug>.articles.json` (HTML) and `.pdf.json` (PDF) and writes a summary `work/meta/<slug>.json`. All provider-specific parsing is delegated to the **profile** module named in the config; the contract is the docstring of `profiles/citation_meta.py`. The core keeps the provider-neutral logic: PDFs saved as `.html`, `missing` articles, titles filled from neighbouring articles, and the page script that opens the article named by `?Title=`.
- `scripts/build_kbart.py`: joins a Collection Manager export to those summaries by ISSN then title, and writes `covoverlay` rows (coverage, split at volume gaps) and `overlay` rows (`title_url`, optional link scheme and `title_id`).
- `scripts/build_search.py` + `search/relay.py`: article search. The relay is an AWS Lambda function behind CloudFront `/api/*` that holds the OCLC WSKey (from SSM), searches the WorldCat Discovery API (`summary-bibs`) and keeps only articles whose journal and year are in the archive, linking each to `<slug>/index.html?Title=…`. The builder writes `search.html`, the Lambda package (relay + `titles.json` from the summaries) and a research-guide search box that goes through the proxy. Standard library only, apart from boto3 in Lambda.
- `scripts/make_manifest.py`, `scripts/check_restore.py`, `infra/batchops/*.ps1`: S3 Batch Operations restore and in-place copy, driven by CSV manifests. At large object counts never loop over objects with the CLI.
- `infra/`: JSON templates with capitalised placeholders (`BUCKET`, `ACCOUNT_ID`, `OAC_ID`, `WEB_ACL_ARN`, `IP_SET_ARN`, `DISTRIBUTION_ID`).

Generated pages are byte-for-byte reproducible from the caches. After changing `build_pages.py`, rebuild a few journals and compare with the deployed pages before uploading anything.

## Settled decisions (don't re-litigate)

- **Storage:** article files in `GLACIER_IR`; generated HTML pages in `STANDARD`.
- **Access path:** private bucket (Block Public Access on) → CloudFront with Origin Access Control on the REST endpoint → AWS WAF web ACL (CloudFront scope, us-east-1) allowing an IP set, default action Block → EZproxy stanza for the CloudFront hostname.
- **CloudFront:** HTTPS redirect, GET/HEAD only, CachingOptimized policy, PriceClass_100, default `*.cloudfront.net` hostname and certificate. Never delete or recreate a distribution that a loaded KBART points at.
- **One page per journal**, with relative article links. `title_url` points explicitly at `.../index.html`, because S3 REST origins don't resolve directory indexes.
- **Existing knowledge base collection**, not a new one: `covoverlay` for coverage, `overlay` for `title_url` and linking. Titles missing from the collection go in a local collection.

## Operating rules

- Work autonomously. Stop and confirm only before costly or hard-to-undo AWS mutations:
  - Glacier restore requests (they incur cost);
  - in-place storage-class copies (minimum storage charges apply);
  - lifecycle or bucket-policy changes, and never remove a bucket's deny statements;
  - Block Public Access changes;
  - creating or deleting CloudFront or WAF resources.
- Read-only AWS calls (`list-*`, `get-*`, `head-object`) need no confirmation.
- Before running a mutating script against a real bucket, offer a dry-run mode or a small test prefix first.
