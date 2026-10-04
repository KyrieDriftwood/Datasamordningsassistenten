param(
    [string]$Python = "",
    [string]$NativeRoot = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
if (-not $Python) { $Python = Join-Path $root ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python not found: $Python"
}

$previousNativeRoot = $env:DATASAMORDNING_BUILD_NATIVE_ROOT
Push-Location $root
try {
    if ($NativeRoot) {
        $env:DATASAMORDNING_BUILD_NATIVE_ROOT = (Resolve-Path -LiteralPath $NativeRoot).Path
    }
    & $Python -m PyInstaller --noconfirm --clean packaging\windows.spec
    if ($LASTEXITCODE -ne 0) { throw "Windows packaging failed." }

    $output = Join-Path $root "dist\DatasamordningsAssistent"
    $report = Join-Path $root "build\windows-self-test.json"
    if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report }
    $process = Start-Process -FilePath (Join-Path $output "DatasamordningsAssistent.exe") `
        -ArgumentList "--self-test", "`"$report`"" -PassThru
    if (-not $process.WaitForExit(60000)) {
        $process.Kill()
        throw "Packaged self-test timed out."
    }
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $report)) {
        throw "Packaged self-test failed. Exit code: $($process.ExitCode)"
    }
    $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if (-not $result.frozen) { throw "Self-test did not run in a frozen executable." }

    $version = & $Python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
    if ($LASTEXITCODE -ne 0) { throw "Could not read application version." }
    $archive = Join-Path $root "dist\DatasamordningsAssistent-$version-windows-x64.zip"
    & $Python -c "import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', root_dir=sys.argv[2], base_dir='DatasamordningsAssistent')" `
        ($archive.Substring(0, $archive.Length - 4)) (Split-Path $output -Parent)
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Windows ZIP." }
    Get-FileHash -LiteralPath $archive -Algorithm SHA256
    Write-Output "Local build only. License review and GitHub publication are separate steps."
}
finally {
    $env:DATASAMORDNING_BUILD_NATIVE_ROOT = $previousNativeRoot
    Pop-Location
}
