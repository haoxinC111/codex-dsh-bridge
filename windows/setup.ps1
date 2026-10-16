param([string]$DshHome = '', [switch]$Uninstall)
$ErrorActionPreference = 'Stop'

# Only the user-owned Desktop patch and this bridge's own directory are changed.
if ([string]::IsNullOrWhiteSpace($DshHome)) {
    $DshHome = if ([string]::IsNullOrWhiteSpace($env:DSH_HOME)) { Join-Path $env:USERPROFILE '.dsh' } else { $env:DSH_HOME }
}
if (Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue) {
    throw 'Quit DeepSeek Harness completely, then run setup again. No process was stopped.'
}
$profileDir = Join-Path $DshHome 'profiles\desktop'
if (-not (Test-Path (Join-Path $profileDir 'package.json'))) {
    throw "Desktop profile not found: $profileDir. Launch DSH once, quit it, or pass -DshHome with its actual home."
}
$bridgeDir = Join-Path $env:USERPROFILE '.codex-dsh-bridge'
$patchPath = Join-Path $profileDir 'cordis.patch.yml'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$original = if (Test-Path $patchPath) { [IO.File]::ReadAllText($patchPath) } else { '' }
$begin = '# BEGIN CODEX-DSH-BRIDGE'
$end = '# END CODEX-DSH-BRIDGE'
$pattern = '(?ms)^# BEGIN CODEX-DSH-BRIDGE\r?\n.*?^# END CODEX-DSH-BRIDGE\r?\n?'
$hasBlock = $original.Contains($begin)
if ($hasBlock -and (-not $original.Contains($end))) { throw 'Incomplete existing bridge block. Original file was preserved.' }
$clean = [regex]::Replace($original, $pattern, '')
if ($clean.Contains($begin) -or $clean.Contains($end)) { throw 'Ambiguous bridge block. Original file was preserved.' }

if ($Uninstall) {
    # DSH rejects an empty/comments-only patch; keep a valid empty YAML list.
    $remaining = (($clean -split '\r?\n' | Where-Object { $_ -notmatch '^\s*(#.*)?$' }) -join "`n").Trim()
    $updated = if ($remaining) { $clean } else { $clean.TrimEnd() + "`r`n[]`r`n" }
} else {
    # This installer deliberately does not rewrite or reserialize unrelated YAML.
    $contentLines = @($clean -split '\r?\n' | Where-Object { $_ -notmatch '^\s*(#.*)?$' })
    $meaningful = ($contentLines -join "`n").Trim()
    if ($meaningful -match '^\[\s*\]$') { $clean = [regex]::Replace($clean, '(?m)^\s*\[\s*\]\s*$', '') }
    # The appended block begins at column zero. Indented root sequences need a
    # manual merge; trimming their indentation here would produce invalid YAML.
    elseif ($meaningful -and ($contentLines[0] -notmatch '^-(?:[ \t]|$)' -or $meaningful -match '(?m)^\s*(?:\.\.\.|---)(?:\s|$)')) {
        throw 'Existing patch uses a complex YAML layout. Ask Codex to merge the local plugin row; nothing was overwritten.'
    }
    $newConfig = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'config.json'))
    $parsed = $newConfig | ConvertFrom-Json
    if ($parsed.token -notmatch '^[a-f0-9]{64}$') { throw 'Invalid bridge configuration.' }
    $oldConfigPath = Join-Path $bridgeDir 'config.json'
    # An installation has credentials before its first pairing creates worker state.
    # Always preserve those credentials, including the not-yet-paired case.
    if (Test-Path $oldConfigPath) {
        $old = [IO.File]::ReadAllText($oldConfigPath) | ConvertFrom-Json
        if ($old.token -ne $parsed.token -or $old.server -ne $parsed.server) { throw 'Existing pairing belongs to a different bridge. Existing state was preserved.' }
    } elseif (Test-Path (Join-Path $bridgeDir 'worker-state.json')) {
        throw 'Existing worker state has no configuration. Restore its configuration before updating; nothing was overwritten.'
    }
    New-Item -ItemType Directory -Force -Path $bridgeDir | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'worker.mjs') -Destination (Join-Path $bridgeDir 'worker.mjs') -Force
    [IO.File]::WriteAllText($oldConfigPath, $newConfig, $utf8)
    $modulePath = (Join-Path $bridgeDir 'worker.mjs').Replace('\', '/') | ConvertTo-Json -Compress
    $block = @"
$begin
- insert:
    - id: codex-dsh-bridge
      name: $modulePath
$end
"@
    $updated = $clean.TrimEnd() + "`r`n" + $block + "`r`n"
}
if ($original -ne $updated) {
    if (Test-Path $patchPath) {
        $backup = $patchPath + '.bridge-backup-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss-fff')
        Copy-Item -LiteralPath $patchPath -Destination $backup
        Write-Host "Backup: $backup"
    }
    $tempPatch = $patchPath + '.bridge-tmp'
    [IO.File]::WriteAllText($tempPatch, $updated, $utf8)
    Move-Item -LiteralPath $tempPatch -Destination $patchPath -Force
}
Write-Host "Desktop patch: $patchPath"
if ($Uninstall) {
    Write-Host 'Bridge plugin disabled. Local bridge configuration and results were retained.'
} else {
    Write-Host 'Installed. Start DSH, open the intended session, and ask it to call bridge_pair once.'
}
