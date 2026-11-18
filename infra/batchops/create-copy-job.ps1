# Create the S3 Batch Operations job that copies restored objects in place to another storage class.
# Reads bucket names and the role from config.json in the repository root.
# Objects must already be restored (Restore: ongoing-request="false"). Metadata, including
# Content-Type, is copied unchanged unless -ContentType is given (for example for PDFs that were
# saved under .html names). The old version stays as a non-current version.
# The job is created in "Suspended" state awaiting confirmation; start it with:
#   aws s3control update-job-status --account-id <acct> --job-id <id> --requested-job-status Ready
param(
    [Parameter(Mandatory)] [string]$ManifestKey,
    [string]$Label = 'all',
    [string]$StorageClass = 'GLACIER_IR',
    [string]$ContentType
)
$ErrorActionPreference = 'Stop'
$Config = Get-Content (Join-Path $PSScriptRoot '..\..\config.json') -Raw | ConvertFrom-Json
$Account = aws sts get-caller-identity --query Account --output text
$OpsBucket = $Config.ops_bucket
$ETag = (aws s3api head-object --bucket $OpsBucket --key $ManifestKey --query ETag --output text).Trim('"')

$copy = @{ TargetResource = "arn:aws:s3:::$($Config.bucket)"; StorageClass = $StorageClass; MetadataDirective = 'COPY' }
if ($ContentType) { $copy.MetadataDirective = 'REPLACE'; $copy.NewObjectMetadata = @{ ContentType = $ContentType } }
$operation = @{ S3PutObjectCopy = $copy } | ConvertTo-Json -Compress -Depth 5
$manifest = @{
    Spec     = @{ Format = 'S3BatchOperations_CSV_20180820'; Fields = @('Bucket', 'Key') }
    Location = @{ ObjectArn = "arn:aws:s3:::$OpsBucket/$ManifestKey"; ETag = $ETag }
} | ConvertTo-Json -Compress -Depth 5
$report = @{
    Bucket = "arn:aws:s3:::$OpsBucket"; Prefix = 'reports/copy'; Format = 'Report_CSV_20180820'
    Enabled = $true; ReportScope = 'FailedTasksOnly'
} | ConvertTo-Json -Compress -Depth 5

$tmp = New-Item -ItemType Directory -Force (Join-Path $env:TEMP 'pta-batchops')
$operation | Set-Content "$tmp/copy-op.json"; $manifest | Set-Content "$tmp/copy-manifest.json"; $report | Set-Content "$tmp/copy-report.json"

aws s3control create-job --account-id $Account --confirmation-required `
    --operation "file://$tmp/copy-op.json" --manifest "file://$tmp/copy-manifest.json" --report "file://$tmp/copy-report.json" `
    --priority 10 --role-arn "arn:aws:iam::${Account}:role/$($Config.batch_role)" `
    --description "PTA: copy $Label in place to $StorageClass" `
    --client-request-token ([guid]::NewGuid().ToString())
