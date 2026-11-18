# Run only on an isolated test runner with no existing ccworks installation.
param([switch] $ExpectWinSparkle)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location (Split-Path -Parent $PSScriptRoot)

function Run-Installer([string] $Program, [string[]] $Arguments) {
    $process = Start-Process -FilePath $Program -ArgumentList $Arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Installer operation failed (exit $($process.ExitCode))."
    }
}

$installPath = Join-Path $env:LOCALAPPDATA 'Programs\ccworks'
$statePath = Join-Path $env:LOCALAPPDATA 'ccworks'
$updateKey = 'HKCU:\Software\ccworks\WinSparkle'
$existingState = if (Test-Path $statePath) {
    Get-ChildItem -Force -LiteralPath $statePath | Where-Object {
        $_.Name -ne 'concur_session.json.lock' -or $_.PSIsContainer -or $_.Length -ne 0
    }
}
if ((Test-Path $installPath) -or $existingState -or (Test-Path $updateKey)) {
    throw 'The installer test requires a fresh runner.'
}
$python = '.venv\desktop\Scripts\python.exe'
$version = (& $python -c 'from ccworks import __version__; print(__version__)').Trim()
if ($LASTEXITCODE -ne 0) {
    throw 'Could not determine the helper version.'
}
$installer = (Resolve-Path "dist\ccworks-desktop-$version-windows-x64.exe").Path
$uninstaller = Join-Path $installPath 'unins000.exe'
$helper = Join-Path $installPath 'ccworks.exe'
$session = Join-Path $statePath 'concur_session.json'
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'ccworks Desktop.lnk'
$quiet = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')

try {
    Run-Installer $installer $quiet
    if (-not (Test-Path $shortcut) -or -not (Test-Path $uninstaller)) {
        throw 'The Start shortcut or uninstaller did not install.'
    }
    & $python desktop\smoke.py $helper --fresh-chrome
    if ($LASTEXITCODE -ne 0) {
        throw 'The installed helper smoke test failed.'
    }
    $window = Start-Process (Join-Path $installPath 'ccworks-desktop.exe') -PassThru
    try {
        for ($retry = 0; $retry -lt 40 -and -not $window.HasExited; $retry++) {
            $window.Refresh()
            if ($window.MainWindowTitle -eq 'ccworks Desktop') { break }
            Start-Sleep -Milliseconds 250
        }
        if ($window.MainWindowTitle -ne 'ccworks Desktop') {
            throw 'The setup window did not appear.'
        }
        Write-Output 'PASS: the installed Windows setup window opened.'
        if ($ExpectWinSparkle) {
            $preference = Get-ItemPropertyValue -LiteralPath $updateKey -Name CheckForUpdates
            if ([string]$preference -ne '0') {
                throw 'The bundled WinSparkle configuration should enable manual checks only.'
            }
            Write-Output 'PASS: the bundled WinSparkle initialized with manual checks.'
        } elseif (Test-Path $updateKey) {
            throw 'The catalog variant unexpectedly initialized WinSparkle.'
        }
    } finally {
        if (-not $window.HasExited) {
            [void]$window.CloseMainWindow()
            if (-not $window.WaitForExit(5000)) { $window.Kill() }
        }
    }
    New-Item -ItemType Directory -Path $statePath -Force | Out-Null
    Set-Content -Path $session -Value '{"fixture":"not a real session"}'
    $staleMetadata = Join-Path $installPath '_internal\ccworks-0.0.0.dist-info'
    New-Item -ItemType Directory -Path $staleMetadata | Out-Null
    Set-Content -LiteralPath (Join-Path $staleMetadata 'METADATA') -Value @(
        'Metadata-Version: 2.1', 'Name: ccworks', 'Version: 0.0.0'
    )
    Run-Installer $installer $quiet
    if (Test-Path -LiteralPath $staleMetadata) {
        throw 'Reinstall left older ccworks version metadata behind.'
    }
    if ((& $helper --version).Trim() -ne "ccworks $version" -or $LASTEXITCODE -ne 0) {
        throw 'Reinstall did not resolve the installed release version.'
    }
    if ((Get-Content -Raw $session).Trim() -ne '{"fixture":"not a real session"}') {
        throw 'Reinstall changed the saved session.'
    }
    Run-Installer $uninstaller ($quiet + @('/PURGECCWORKSDATA'))
    if ((Test-Path $session) -or (Test-Path $helper) -or (Test-Path $shortcut) -or (Test-Path $updateKey)) {
        throw 'Uninstall left the helper, shortcut or session behind.'
    }
    Write-Output 'PASS: per-user install, reinstall, and uninstall with cleanup.'
} finally {
    if (Test-Path $uninstaller) {
        Run-Installer $uninstaller ($quiet + @('/PURGECCWORKSDATA'))
    }
}
