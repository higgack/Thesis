param(
    [switch]$All
)

$ErrorActionPreference = "Stop"

# Run from repository root regardless of where the user calls this script.
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

$bashExe = Get-Command bash -ErrorAction SilentlyContinue
if (-not $bashExe) {
    Write-Error "bash를 찾지 못했습니다. Git Bash를 설치하거나 WSL에서 'bash scripts/preflight.sh'를 실행하세요."
    exit 2
}

$argList = @("scripts/preflight.sh")
if ($All) {
    $argList += "--all"
}

& $bashExe.Source @argList
exit $LASTEXITCODE
