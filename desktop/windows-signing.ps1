function Get-CCWorksSigningArguments {
    $timestamp = $env:CCWORKS_TIMESTAMP_URL
    $dlib = $env:CCWORKS_ARTIFACT_SIGNING_DLIB
    $metadata = $env:CCWORKS_ARTIFACT_SIGNING_METADATA

    if ($dlib -or $metadata) {
        if (-not $dlib -or -not $metadata -or $env:CCWORKS_SIGN_THUMBPRINT -or
            -not (Test-Path -LiteralPath $dlib -PathType Leaf) -or
            -not (Test-Path -LiteralPath $metadata -PathType Leaf)) {
            throw 'Set both Azure Artifact Signing paths and clear CCWORKS_SIGN_THUMBPRINT.'
        }
        if (-not $timestamp) {
            $timestamp = 'http://timestamp.acs.microsoft.com'
        }
        $selection = @('/dlib', (Resolve-Path -LiteralPath $dlib).Path,
            '/dmdf', (Resolve-Path -LiteralPath $metadata).Path)
    } else {
        if ($env:CCWORKS_SIGN_THUMBPRINT -notmatch '^[A-Fa-f0-9]{40}$') {
            throw 'Set CCWORKS_SIGN_THUMBPRINT to the approved Windows signing certificate.'
        }
        $selection = @('/sha1', $env:CCWORKS_SIGN_THUMBPRINT)
    }
    if ($timestamp -notmatch '^https?://[A-Za-z0-9.\-/:]+$') {
        throw 'Set CCWORKS_TIMESTAMP_URL to the approved time-stamping service.'
    }
    return @('sign', '/fd', 'SHA256', '/tr', $timestamp, '/td', 'SHA256') + $selection
}

function Format-CCWorksInnoSignTool([string] $Program, [string[]] $Arguments) {
    # ISCC expands $q, $$ and the final $f inside this argument.
    $quoted = (@($Program) + $Arguments) | ForEach-Object {
        '$q' + $_.Replace('$', '$$') + '$q'
    }
    return '/Sccworks=' + ($quoted -join ' ') + ' $f'
}
