$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. "$PSScriptRoot/windows-signing.ps1"

function Assert-Signing([bool] $Condition, [string] $Message) {
    if (-not $Condition) {
        throw $Message
    }
}

$signVariables = @('CCWORKS_TIMESTAMP_URL', 'CCWORKS_SIGN_THUMBPRINT',
    'CCWORKS_ARTIFACT_SIGNING_DLIB', 'CCWORKS_ARTIFACT_SIGNING_METADATA')
$existing = @{}
foreach ($setting in $signVariables) {
    $existing[$setting] = [Environment]::GetEnvironmentVariable($setting, 'Process')
    [Environment]::SetEnvironmentVariable($setting, $null, 'Process')
}
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('ccworks signing ' + [Guid]::NewGuid().ToString())
New-Item -Path $fixture -ItemType Directory | Out-Null
try {
    $env:CCWORKS_SIGN_THUMBPRINT = 'a' * 40
    $env:CCWORKS_TIMESTAMP_URL = 'https://example.test/timestamp'
    $local = Get-CCWorksSigningArguments
    Assert-Signing ($local -contains '/sha1') 'Local signing must select the requested certificate.'
    Assert-Signing ($local -notcontains '/dlib') 'Local signing must use the Windows certificate store.'
    $env:CCWORKS_SIGN_THUMBPRINT = $null
    $env:CCWORKS_TIMESTAMP_URL = $null

    $env:CCWORKS_ARTIFACT_SIGNING_DLIB = Join-Path $fixture 'Signing.dll'
    $env:CCWORKS_ARTIFACT_SIGNING_METADATA = Join-Path $fixture 'Sign $ metadata.json'
    New-Item -Path $env:CCWORKS_ARTIFACT_SIGNING_DLIB -ItemType File | Out-Null
    Set-Content -LiteralPath $env:CCWORKS_ARTIFACT_SIGNING_METADATA -Value '{}'
    $azure = Get-CCWorksSigningArguments
    Assert-Signing ($azure -contains 'http://timestamp.acs.microsoft.com') 'Azure must default to its timestamp service.'
    Assert-Signing ($azure -contains $env:CCWORKS_ARTIFACT_SIGNING_METADATA) 'SignTool must receive the metadata as one argument.'
    $tool = Format-CCWorksInnoSignTool 'C:\Program Files\SDK\signtool.exe' $azure
    Assert-Signing ($tool.StartsWith('/Sccworks=$qC:\Program Files\SDK\signtool.exe$q')) 'The executable path must be quoted.'
    Assert-Signing ($tool.Contains('Sign $$ metadata.json$q')) 'ISCC must receive literal dollar signs and quoted paths.'
    Assert-Signing ($tool.EndsWith(' $f')) 'ISCC must supply its installer or uninstaller path.'

    $env:CCWORKS_ARTIFACT_SIGNING_METADATA = $null
    $rejected = $false
    try { Get-CCWorksSigningArguments | Out-Null } catch { $rejected = $true }
    Assert-Signing $rejected 'Incomplete Azure configuration must fail before building.'
    Write-Output 'PASS: Windows signing arguments for local certificates and Azure Artifact Signing.'
} finally {
    Remove-Item -LiteralPath $fixture -Recurse -Force
    foreach ($setting in $signVariables) {
        [Environment]::SetEnvironmentVariable($setting, $existing[$setting], 'Process')
    }
}
