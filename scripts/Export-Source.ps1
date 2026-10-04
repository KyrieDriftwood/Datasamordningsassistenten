param([Parameter(Mandatory = $true)][string]$Destination)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) {
    throw "Destination already exists; refusing to overwrite: $destinationPath"
}
New-Item -ItemType Directory -Path $destinationPath | Out-Null

# Explicit allowlist; exported source still needs a confidentiality review.
$directories = @("app", "backend", "metadata", "dwg", "config", "acc", "tb", "scripts", "packaging", "tests")
foreach ($directory in $directories) {
    $source = Join-Path $root $directory
    foreach ($file in Get-ChildItem -LiteralPath $source -Recurse -File) {
        if ($file.Extension -notin @(".py", ".svg", ".ps1", ".spec")) { continue }
        if ($file.FullName -match '\\(__pycache__|\.pytest_cache)\\') { continue }
        $relative = $file.FullName.Substring($root.Length + 1)
        $target = Join-Path $destinationPath $relative
        New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target
    }
}
foreach ($name in @("README.md", "pyproject.toml", "requirements.txt", ".gitignore", "Starta.bat")) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination (Join-Path $destinationPath $name)
}
foreach ($name in @("rust_dwg_extractor", "dwg_pdf_backend", "tb_sync_rust")) {
    $target = Join-Path $destinationPath $name
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    foreach ($fileName in @("Cargo.toml", "Cargo.lock", "build.rs", "wrapper.h", "README.md")) {
        $file = Join-Path (Join-Path $root $name) $fileName
        if (Test-Path -LiteralPath $file -PathType Leaf) {
            Copy-Item -LiteralPath $file -Destination (Join-Path $target $fileName)
        }
    }
    Copy-Item -LiteralPath (Join-Path (Join-Path $root $name) "src") -Destination $target -Recurse
}
Copy-Item -LiteralPath (Join-Path $root ".cargo") -Destination $destinationPath -Recurse
Copy-Item -LiteralPath (Join-Path $root "packaging\README.md") `
    -Destination (Join-Path $destinationPath "packaging\README.md")
$tasks = Join-Path $root ".vscode\tasks.json"
if (Test-Path -LiteralPath $tasks -PathType Leaf) {
    New-Item -ItemType Directory -Path (Join-Path $destinationPath ".vscode") | Out-Null
    Copy-Item -LiteralPath $tasks -Destination (Join-Path $destinationPath ".vscode\tasks.json")
}
Write-Output "Source exported locally to $destinationPath. Review before uploading; no Git operation performed."
