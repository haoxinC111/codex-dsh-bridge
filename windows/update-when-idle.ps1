param([string]$DshHome = '')
$ErrorActionPreference = 'Stop'
$bridgeDir = Join-Path $env:USERPROFILE '.codex-dsh-bridge'
if ([string]::IsNullOrWhiteSpace($DshHome)) {
    $DshHome = if ([string]::IsNullOrWhiteSpace($env:DSH_HOME)) { Join-Path $env:USERPROFILE '.dsh' } else { $env:DSH_HOME }
}
$patch = Join-Path $DshHome 'profiles\desktop\cordis.patch.yml'
$oldConfig = [IO.File]::ReadAllText((Join-Path $bridgeDir 'config.json')) | ConvertFrom-Json
$newConfig = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'config.json')) | ConvertFrom-Json
if ($oldConfig.token -ne $newConfig.token -or $oldConfig.server -ne $newConfig.server) { throw 'Different bridge credentials; update refused.' }
$healthText = & curl.exe --silent --show-error --fail --noproxy '*' --connect-timeout 5 --max-time 10 ($oldConfig.server + '/health')
if ($LASTEXITCODE -ne 0) { throw 'Could not verify Mac maintenance state; update refused.' }
$health = ($healthText -join "`n") | ConvertFrom-Json
if ($health.dispatch_paused -ne $true) { throw 'Pause Mac dispatch before updating this plugin.' }

# Run as a separate process. The current report must leave the durable outbox
# before replacing the plugin; otherwise its in-memory worker could lose that report.
$idle = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $state = [IO.File]::ReadAllText((Join-Path $bridgeDir 'worker-state.json')) | ConvertFrom-Json
    if ($null -eq $state.active -and $null -eq $state.outbox) { $idle = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $idle) { throw 'Bridge did not become idle within 120 seconds. Nothing changed.' }
$original = [IO.File]::ReadAllText($patch)
$pattern = '(?ms)^# BEGIN CODEX-DSH-BRIDGE\r?\n.*?^# END CODEX-DSH-BRIDGE\r?\n?'
if ([regex]::Matches($original, $pattern).Count -ne 1) { throw 'Expected one bridge block; original patch preserved.' }
$target = Join-Path $bridgeDir 'worker-v4.mjs'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'worker.mjs') -Destination $target -Force
$modulePath = $target.Replace('\', '/') | ConvertTo-Json -Compress
$block = "# BEGIN CODEX-DSH-BRIDGE`r`n- insert:`r`n    - id: codex-dsh-bridge`r`n      name: $modulePath`r`n# END CODEX-DSH-BRIDGE`r`n"
$updated = [regex]::Replace($original, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $block })
Copy-Item -LiteralPath $patch -Destination ($patch + '.bridge-backup-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss-fff'))
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText(($patch + '.bridge-tmp'), $updated, $utf8)
Move-Item -LiteralPath ($patch + '.bridge-tmp') -Destination $patch -Force
Write-Host 'Bridge protocol 4 installed. Desktop needs a normal DSH restart. No process was killed.'
