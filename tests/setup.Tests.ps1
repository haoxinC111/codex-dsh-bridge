# Standalone installer checks. Run with pwsh or Windows PowerShell; no Pester needed.
# Override only process lookup and the bridge-directory join, never the real user's environment.
$ErrorActionPreference = 'Stop'
$fixtureRoot = [IO.Path]::Combine([IO.Path]::GetTempPath(), 'bridge-installer-' + [guid]::NewGuid())
$global:BridgeInstallerFixtureDirectory = ''
$failures = @()
function global:Get-Process { param($Name, $ErrorAction) return @() }
function global:Join-Path {
    param([AllowNull()][AllowEmptyString()][string]$Path, [string]$ChildPath)
    if ($ChildPath -eq '.codex-dsh-bridge') { return $global:BridgeInstallerFixtureDirectory }
    return Microsoft.PowerShell.Management\Join-Path -Path $Path -ChildPath $ChildPath
}
function Assert-Equal($Actual, $Expected, $Message) {
    if ($Actual -cne $Expected) { throw $Message }
}
function New-Fixture($Name, $Patch = '[]') {
    $caseDir = [IO.Path]::Combine($fixtureRoot, $Name)
    $global:BridgeInstallerFixtureDirectory = [IO.Path]::Combine($caseDir, 'installed')
    $package = [IO.Path]::Combine($caseDir, 'package')
    $dsh = [IO.Path]::Combine($caseDir, 'dsh')
    $profile = [IO.Path]::Combine($dsh, 'profiles', 'desktop')
    [IO.Directory]::CreateDirectory($package) | Out-Null
    [IO.Directory]::CreateDirectory($profile) | Out-Null
    [IO.Directory]::CreateDirectory($global:BridgeInstallerFixtureDirectory) | Out-Null
    $source = [IO.Path]::Combine($PSScriptRoot, '..', 'windows', 'setup.ps1')
    [IO.File]::Copy($source, [IO.Path]::Combine($package, 'setup.ps1'))
    [IO.File]::WriteAllText([IO.Path]::Combine($package, 'worker.mjs'), '// fixture plugin')
    $newConfig = @{ token = ('a' * 64); server = 'http://100.64.0.1:18766'; pairId = 'test-pair'; pollMs = 3000 } | ConvertTo-Json
    [IO.File]::WriteAllText([IO.Path]::Combine($package, 'config.json'), $newConfig)
    [IO.File]::WriteAllText([IO.Path]::Combine($profile, 'package.json'), '{}')
    $patchPath = [IO.Path]::Combine($profile, 'cordis.patch.yml')
    [IO.File]::WriteAllText($patchPath, $Patch)
    return @{ setup = [IO.Path]::Combine($package, 'setup.ps1'); dsh = $dsh; patch = $patchPath; config = $newConfig }
}
function Expect-Rejected($Fixture, $ExpectedError) {
    $rejected = $false
    try { & $Fixture.setup -DshHome $Fixture.dsh } catch {
        if ($_ -notmatch $ExpectedError) { throw "Unexpected rejection: $_" }
        $rejected = $true
    }
    if (-not $rejected) { throw 'Installer unexpectedly accepted this existing installation/layout' }
}
function Test-Case($Name, [scriptblock]$Body) {
    try { & $Body; Write-Host "PASS: $Name" }
    catch { $script:failures += $Name; Write-Host "FAIL: $Name - $_`n$($_.ScriptStackTrace)" }
}
try {
    Test-Case 'fresh install, same pairing reinstall, uninstall' {
        $f = New-Fixture 'fresh'
        & $f.setup -DshHome $f.dsh
        $configPath = [IO.Path]::Combine($global:BridgeInstallerFixtureDirectory, 'config.json')
        $first = [IO.File]::ReadAllText($configPath)
        & $f.setup -DshHome $f.dsh
        Assert-Equal ([IO.File]::ReadAllText($configPath)) $first 'Reinstall changed pairing'
        & $f.setup -DshHome $f.dsh -Uninstall
        Assert-Equal ([IO.File]::ReadAllText($f.patch).Trim()) '[]' 'Uninstall must keep valid empty YAML'
        Assert-Equal ([IO.File]::ReadAllText($configPath)) $first 'Uninstall must preserve credentials'
    }
    Test-Case 'foreign unpaired installation is preserved' {
        $f = New-Fixture 'foreign'
        $old = @{ token = ('b' * 64); server = 'http://100.64.0.2:18766' } | ConvertTo-Json
        $configPath = [IO.Path]::Combine($global:BridgeInstallerFixtureDirectory, 'config.json')
        $workerPath = [IO.Path]::Combine($global:BridgeInstallerFixtureDirectory, 'worker.mjs')
        [IO.File]::WriteAllText($configPath, $old)
        [IO.File]::WriteAllText($workerPath, '// original')
        Expect-Rejected $f 'different bridge'
        Assert-Equal ([IO.File]::ReadAllText($configPath)) $old 'Old credentials changed'
        Assert-Equal ([IO.File]::ReadAllText($workerPath)) '// original' 'Old plugin changed'
        Assert-Equal ([IO.File]::ReadAllText($f.patch)) '[]' 'Profile changed before rejection'
    }
    Test-Case 'orphan worker state is preserved' {
        $f = New-Fixture 'orphan'
        [IO.File]::WriteAllText([IO.Path]::Combine($global:BridgeInstallerFixtureDirectory, 'worker-state.json'), '{"active":"saved-task"}')
        Expect-Rejected $f 'state.*configuration|configuration.*state'
        if (Test-Path ([IO.Path]::Combine($global:BridgeInstallerFixtureDirectory, 'config.json'))) { throw 'Rebound orphan state' }
    }
    Test-Case 'indented root YAML is rejected without writing files' {
        $original = "  - insert:`n      - id: existing`n        name: example`n"
        $f = New-Fixture 'indent' $original
        Expect-Rejected $f 'YAML|layout'
        Assert-Equal ([IO.File]::ReadAllText($f.patch)) $original 'Existing YAML changed'
        if (Test-Path ([IO.Path]::Combine($global:BridgeInstallerFixtureDirectory, 'config.json'))) { throw 'Files written before layout rejection' }
    }
    Test-Case 'ordinary root list is preserved' {
        $original = "# user configuration`n- insert:`n    - id: existing`n      name: example`n"
        $f = New-Fixture 'ordinary' $original
        & $f.setup -DshHome $f.dsh
        if (-not ([IO.File]::ReadAllText($f.patch).StartsWith($original.TrimEnd()))) { throw 'Existing configuration lost' }
    }
    Test-Case 'document end with a trailing comment is rejected' {
        $original = "- insert:`n    - id: existing`n      name: example`n... # end of user patch`n"
        $f = New-Fixture 'document-end' $original
        Expect-Rejected $f 'YAML|layout'
        Assert-Equal ([IO.File]::ReadAllText($f.patch)) $original 'Document boundary was changed'
        if (Test-Path ([IO.Path]::Combine($global:BridgeInstallerFixtureDirectory, 'config.json'))) { throw 'Files written before document rejection' }
    }
} finally {
    if ([IO.Directory]::Exists($fixtureRoot)) { Remove-Item -LiteralPath $fixtureRoot -Recurse -Force }
}
if ($failures.Count) { throw "$($failures.Count) installer checks failed" }
Write-Host 'All installer checks passed in isolated temporary directories.'
