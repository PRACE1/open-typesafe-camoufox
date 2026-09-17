<# Installs scripts/githooks/* into .git/hooks/. Idempotent. #>
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$dest = Join-Path $root ".git\hooks"
if (-not (Test-Path $dest)) { throw ".git directory not found at $root" }
foreach ($hook in @("pre-commit", "pre-push")) {
    Copy-Item -LiteralPath (Join-Path $root "scripts\githooks\$hook") `
        -Destination (Join-Path $dest $hook) -Force
    Write-Output "installed $hook"
}
Write-Output "Done. Bypass any hook with JEV_HOOKS_OFF=1 (pre-push also: git push --no-verify)."
