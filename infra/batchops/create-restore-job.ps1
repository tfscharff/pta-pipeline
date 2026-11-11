# Create the S3 Batch Operations job that restores every object from DEEP_ARCHIVE (Bulk tier).
# The job is created in "Suspended" state awaiting confirmation; start it with:
#   aws s3control update-job-status --account-id <acct> --job-id <id> --requested-job-status Ready
param(
    [string]$ManifestKey = 'manifests/restore_all.csv',
    [int]$Days = 7,
    [string]$Label = 'all'
)
$ErrorActionPreference = 'Stop'
$Account = aws sts get-caller-identity --query Account --output text
$OpsBucket = 'wheaton-muse-pta-ops'
$ETag = (aws s3api head-object --bucket $OpsBucket --key $ManifestKey --query ETag --output text).Trim('"')

$operation = @{ S3InitiateRestoreObject = @{ ExpirationInDays = $Days; GlacierJobTier = 'BULK' } } | ConvertTo-Json -Compress -Depth 5
$manifest = @{
    Spec     = @{ Format = 'S3BatchOperations_CSV_20180820'; Fields = @('Bucket', 'Key') }
    Location = @{ ObjectArn = "arn:aws:s3:::$OpsBucket/$ManifestKey"; ETag = $ETag }
} | ConvertTo-Json -Compress -Depth 5
$report = @{
    Bucket = "arn:aws:s3:::$OpsBucket"; Prefix = 'reports/restore'; Format = 'Report_CSV_20180820'
    Enabled = $true; ReportScope = 'FailedTasksOnly'
} | ConvertTo-Json -Compress -Depth 5

$tmp = New-Item -ItemType Directory -Force (Join-Path $env:TEMP 'muse-batchops')
$operation | Set-Content "$tmp/op.json"; $manifest | Set-Content "$tmp/manifest.json"; $report | Set-Content "$tmp/report.json"

aws s3control create-job --account-id $Account --confirmation-required `
    --operation "file://$tmp/op.json" --manifest "file://$tmp/manifest.json" --report "file://$tmp/report.json" `
    --priority 10 --role-arn "arn:aws:iam::${Account}:role/muse-pta-batchops" `
    --description "MUSE PTA: restore $Label from DEEP_ARCHIVE (Bulk, $Days days)" `
    --client-request-token ([guid]::NewGuid().ToString())
