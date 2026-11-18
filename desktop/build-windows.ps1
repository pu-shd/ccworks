param(
    [switch] $BundleOnly,
    [switch] $UnsignedInstaller,
    [switch] $DryRun
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location (Split-Path -Parent $PSScriptRoot)
. "$PSScriptRoot/windows-signing.ps1"

function Invoke-Checked([string] $Program, [string[]] $Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Build step failed (exit $LASTEXITCODE): $Program"
    }
}

if ($DryRun) {
    Write-Output 'Build the Windows x64 helper. The release build also signs the binaries, installer, and uninstaller.'
    exit 0
}

if ($BundleOnly -and $UnsignedInstaller) {
    throw 'Choose either -BundleOnly or -UnsignedInstaller.'
}
if (-not $BundleOnly) {
    if (-not $env:CCWORKS_INNO_COMPILER -or -not (Test-Path $env:CCWORKS_INNO_COMPILER -PathType Leaf)) {
        throw 'Set CCWORKS_INNO_COMPILER to ISCC.exe from Inno Setup 6.7.4.'
    }
    $compiler = Get-Item -LiteralPath $env:CCWORKS_INNO_COMPILER

}
if (-not $BundleOnly -and -not $UnsignedInstaller) {
    if (-not $env:CCWORKS_SIGNTOOL -or -not (Test-Path $env:CCWORKS_SIGNTOOL -PathType Leaf)) {
        throw 'Set CCWORKS_SIGNTOOL to the Windows SDK signtool.exe.'
    }
    $signToolPath = (Resolve-Path -LiteralPath $env:CCWORKS_SIGNTOOL).Path
    $sign = Get-CCWorksSigningArguments
}

$python = '.venv\desktop\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    if ($env:CCWORKS_WINDOWS_BUILD_PYTHON) {
        $buildPython = (Resolve-Path -LiteralPath $env:CCWORKS_WINDOWS_BUILD_PYTHON).Path
        Invoke-Checked $buildPython @('-m', 'venv', '.venv\desktop')
    } else {
        Invoke-Checked 'py' @('-3.12', '-m', 'venv', '.venv\desktop')
    }
}
Invoke-Checked $python @('-c', 'import sys, sysconfig, tkinter; assert sys.platform == ''win32'' and sysconfig.get_platform() == ''win-amd64''; print(''Tcl/Tk'', tkinter.TkVersion)')
Invoke-Checked $python @('-m', 'pip', 'install', '-r', 'requirements-desktop.lock', '-e', '.')
Invoke-Checked $python @('-m', 'PyInstaller', '--noconfirm', '--clean', 'desktop\ccworks.spec')
$helper = 'dist\ccworks-desktop\ccworks.exe'
Invoke-Checked $helper @('--version')
if ($BundleOnly) {
    Write-Output 'Built the Windows smoke-test bundle.'
    exit 0
}

$version = (& $python -c 'from ccworks import __version__; print(__version__)').Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') {
    throw 'Could not determine the helper version.'
}
if ($UnsignedInstaller) {
    Invoke-Checked $compiler.FullName @("/DAppVersion=$version", 'desktop\ccworks.iss')
    Write-Output "Built the unsigned Windows installer for testing."
    exit 0
}
Get-ChildItem 'dist\ccworks-desktop' -File -Recurse | Where-Object {
    $_.Extension -in '.exe', '.dll', '.pyd'
} | ForEach-Object {
    if ((Get-AuthenticodeSignature -LiteralPath $_.FullName).Status -ne 'Valid') {
        Invoke-Checked $signToolPath ($sign + @($_.FullName))
    }
    Invoke-Checked $signToolPath @('verify', '/pa', $_.FullName)
}

$signTool = Format-CCWorksInnoSignTool $signToolPath $sign
Invoke-Checked $compiler.FullName @("/DAppVersion=$version", '/DReleaseSigning=1', $signTool, 'desktop\ccworks.iss')
$installer = "dist\ccworks-desktop-$version-windows-x64.exe"
Invoke-Checked $signToolPath @('verify', '/pa', $installer)
Write-Output "Built $installer."
