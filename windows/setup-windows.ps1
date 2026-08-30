<#
.SYNOPSIS
  Dogany agent -- Windows (WSL2) host setup. Prepares Windows and the WSL2
  distro so the agent stays alive with no terminal open, then hands off to
  the Linux-side installer (install.sh).

.DESCRIPTION
  This script ships inside the cloned repo (windows/setup-windows.ps1) and is
  run from that path -- nothing is downloaded from a URL. It runs as the
  normal interactive user and requires NO administrator elevation:
    - %UserProfile%\.wslconfig is a per-user file.
    - wsl.exe -u root grants root inside the distro to the owning user by WSL
      design, with no Windows elevation.
    - Register-ScheduledTask for a run-as-self logon task needs no elevation.
    - wsl.exe --shutdown needs no elevation.

  It is idempotent: every step is safe to re-run and converges to the same
  result. See windows/README notes and the repo README "Windows (WSL2)"
  section for the platform contract and the honest limits.

.PARAMETER DistroName
  Target WSL distro name. Default: Ubuntu.

.PARAMETER MemoryGB
  Override the computed [wsl2] memory assignment (integer GB).

.PARAMETER Uninstall
  Remove the Dogany-written Windows-side state (scheduled task, .wslconfig
  keys, Linux-side marker). Does NOT delete the distro or agent data.

.EXAMPLE
  powershell.exe -ExecutionPolicy Bypass -File .\windows\setup-windows.ps1

.EXAMPLE
  powershell.exe -ExecutionPolicy Bypass -File .\windows\setup-windows.ps1 -MemoryGB 6

.EXAMPLE
  powershell.exe -ExecutionPolicy Bypass -File .\windows\setup-windows.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string]$DistroName = 'Ubuntu',
    [int]$MemoryGB = 0,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

# Schema version of the Windows-side config this script writes. Bump this
# whenever the required .wslconfig / wsl.conf shape changes; the Linux side
# (install.sh / update.sh) compares against it via the marker file.
$script:SetupVersion = 1
$script:TaskName = 'DoganyWSLKeepAlive'
$script:WslConfigPath = Join-Path $env:USERPROFILE '.wslconfig'
$script:MarkerPath = '/etc/dogany/windows-setup.version'

function Write-Info { param([string]$Message) Write-Host "[dogany] $Message" }
function Write-Warn { param([string]$Message) Write-Host "[dogany][WARN] $Message" -ForegroundColor Yellow }
function Write-Err  { param([string]$Message) Write-Host "[dogany][ERROR] $Message" -ForegroundColor Red }

# Fail loudly and exit non-zero. Never claim success on a broken result.
function Fail-Loud {
    param([string]$Message)
    Write-Err $Message
    exit 1
}

# Verify the target distro exists. wsl -l -q prints one distro name per line.
# Names can arrive UTF-16-encoded from wsl.exe; normalize before comparing.
function Test-DistroExists {
    param([string]$Name)
    $raw = & wsl.exe -l -q 2>$null
    if (-not $raw) { return $false }
    foreach ($line in $raw) {
        $clean = ($line -replace "`0", '').Trim()
        if ($clean -eq $Name) { return $true }
    }
    return $false
}

# Compute the [wsl2] memory assignment in GB.
#   N = min(floor(hostRAM / 2), 8), floor 4.
# Table: 8GB host -> 4; 12 -> 6; 16 -> 8; 32+ -> 8 (capped). Host < 8GB warns
# and still writes 4GB. An explicit -MemoryGB overrides the computation.
function Get-MemoryAssignmentGB {
    if ($MemoryGB -gt 0) {
        Write-Info "Memory assignment overridden by -MemoryGB: ${MemoryGB}GB"
        return $MemoryGB
    }
    $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction SilentlyContinue
    if (-not $cs -or -not $cs.TotalPhysicalMemory) {
        Write-Warn "Could not read host RAM; defaulting memory assignment to 4GB."
        return 4
    }
    $hostGB = [math]::Floor($cs.TotalPhysicalMemory / 1GB)
    if ($hostGB -lt 8) {
        Write-Warn "Host RAM is ${hostGB}GB, below the supported minimum (8GB). Writing 4GB and continuing."
    }
    $half = [math]::Floor($hostGB / 2)
    $n = [math]::Min($half, 8)
    if ($n -lt 4) { $n = 4 }
    Write-Info "Host RAM ${hostGB}GB -> [wsl2] memory=${n}GB"
    return [int]$n
}

# Merge the Dogany keys into %UserProfile%\.wslconfig, preserving any unrelated
# user content. Backs up an existing file first. This is a minimal INI editor:
# it keeps unknown sections/keys untouched and only sets the keys we own.
function Set-WslConfig {
    param([int]$MemGB)

    # The keys Dogany owns, grouped by section.
    $owned = @{
        'general' = [ordered]@{ 'instanceIdleTimeout' = '-1' }
        'wsl2'    = [ordered]@{
            'vmIdleTimeout'   = '-1'
            'guiApplications' = 'false'
            'memory'          = "${MemGB}GB"
        }
    }

    $lines = @()
    if (Test-Path $script:WslConfigPath) {
        $backup = "$($script:WslConfigPath).bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Copy-Item -LiteralPath $script:WslConfigPath -Destination $backup -Force
        Write-Info "Backed up existing .wslconfig -> $backup"
        $lines = @(Get-Content -LiteralPath $script:WslConfigPath)
    }

    # Parse into an ordered map of section -> ordered map of key -> value, and
    # keep verbatim non-key lines (comments/blanks) attached to their section so
    # unrelated content survives a round trip.
    $sections = [ordered]@{}
    $sectionRaw = [ordered]@{}
    $current = ''
    $sections[$current] = [ordered]@{}
    $sectionRaw[$current] = @()
    foreach ($line in $lines) {
        $trim = $line.Trim()
        if ($trim -match '^\[(.+)\]$') {
            $current = $Matches[1].Trim()
            if (-not $sections.Contains($current)) {
                $sections[$current] = [ordered]@{}
                $sectionRaw[$current] = @()
            }
            continue
        }
        if ($trim -match '^([^#;=]+?)\s*=\s*(.*)$') {
            $k = $Matches[1].Trim()
            $v = $Matches[2].Trim()
            $sections[$current][$k] = $v
            continue
        }
        # comment or blank -- preserve verbatim under the current section
        $sectionRaw[$current] += $line
    }

    # Apply owned keys.
    foreach ($sec in $owned.Keys) {
        if (-not $sections.Contains($sec)) {
            $sections[$sec] = [ordered]@{}
            $sectionRaw[$sec] = @()
        }
        foreach ($k in $owned[$sec].Keys) {
            $sections[$sec][$k] = $owned[$sec][$k]
        }
    }

    # Render. The top-level (no-section) content comes first, then each section.
    $out = @()
    foreach ($raw in $sectionRaw['']) { $out += $raw }
    foreach ($k in $sections[''].Keys) { $out += "$k=$($sections[''][$k])" }
    foreach ($sec in $sections.Keys) {
        if ($sec -eq '') { continue }
        $out += "[$sec]"
        foreach ($raw in $sectionRaw[$sec]) { $out += $raw }
        foreach ($k in $sections[$sec].Keys) { $out += "$k=$($sections[$sec][$k])" }
    }

    Set-Content -LiteralPath $script:WslConfigPath -Value $out -Encoding ASCII
    Write-Info "Wrote .wslconfig (instanceIdleTimeout=-1, vmIdleTimeout=-1, guiApplications=false, memory=${MemGB}GB)."
}

# Ensure [boot]\nsystemd=true in the distro's /etc/wsl.conf, from the Windows
# side via wsl -u root. Idempotent: append the section/key only when absent,
# never duplicate.
function Set-WslConfSystemd {
    param([string]$Distro)

    # A small, self-contained shell program run as root inside the distro.
    # It edits /etc/wsl.conf without duplicating an existing systemd=true.
    $remote = @'
set -e
f=/etc/wsl.conf
touch "$f"
if grep -qE '^[[:space:]]*systemd[[:space:]]*=' "$f"; then
  # normalize any existing systemd= line to true
  sed -i -E 's/^[[:space:]]*systemd[[:space:]]*=.*/systemd=true/' "$f"
elif grep -qE '^\[boot\]' "$f"; then
  # [boot] exists but no systemd key -- insert right after the header
  sed -i -E '/^\[boot\]/a systemd=true' "$f"
else
  printf '\n[boot]\nsystemd=true\n' >> "$f"
fi
'@
    & wsl.exe -d $Distro -u root -- bash -c $remote
    if ($LASTEXITCODE -ne 0) {
        Fail-Loud "Failed to write /etc/wsl.conf via 'wsl -u root'. If your organization blocks root access to WSL, run the two root-side commands from the README troubleshooting note manually."
    }
    Write-Info "Ensured [boot] systemd=true in /etc/wsl.conf (distro: $Distro)."
}

# Finish the root-side config AFTER the restart so systemd is PID 1: disable
# systemd-timesyncd (per the Ubuntu WSL time-sync guidance) and write the
# Linux-side marker.
function Complete-RootSideConfig {
    param([string]$Distro)
    $remote = @"
systemctl disable --now systemd-timesyncd 2>/dev/null || true
mkdir -p /etc/dogany
echo $($script:SetupVersion) > /etc/dogany/windows-setup.version
"@
    & wsl.exe -d $Distro -u root -- bash -c $remote
    if ($LASTEXITCODE -ne 0) {
        Fail-Loud "Failed to complete root-side config (timesyncd disable / marker write) via 'wsl -u root'."
    }
    Write-Info "Disabled systemd-timesyncd and wrote marker $($script:MarkerPath)=$($script:SetupVersion)."
}

# Verify systemd is PID 1 in the distro. Fail loudly otherwise.
function Assert-SystemdPid1 {
    param([string]$Distro)
    $comm = (& wsl.exe -d $Distro -- ps -p 1 -o comm= 2>$null)
    $comm = ($comm -replace "`0", '').Trim()
    if ($comm -ne 'systemd') {
        $conf = (& wsl.exe -d $Distro -u root -- cat /etc/wsl.conf 2>$null)
        Write-Err "PID 1 is '$comm', not 'systemd'. /etc/wsl.conf content:"
        Write-Host ($conf -join "`n")
        Fail-Loud "systemd is not PID 1 in $Distro. Setup cannot continue; run 'wsl --shutdown' and re-run this script."
    }
    Write-Info "Verified systemd is PID 1 in $Distro."
}

# Register (idempotently) the per-user, non-elevated logon task that boots the
# distro at sign-in and holds a live client. Unregister an existing same-name
# task first so a re-run is byte-stable.
function Register-KeepAliveTask {
    param([string]$Distro)

    Unregister-ScheduledTask -TaskName $script:TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null

    # Inline command -- no script file on disk to hijack (MINOR-1). The task
    # runs as the current interactive user, no elevation.
    $inner = "wsl.exe -d $Distro --exec sleep infinity"
    $psArgs = "-NoProfile -WindowStyle Hidden -Command `"$inner`""

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $psArgs
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

    Register-ScheduledTask -TaskName $script:TaskName `
        -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Description 'Dogany: keep the WSL2 distro alive at user sign-in (per-user, non-elevated).' | Out-Null

    Write-Info "Registered scheduled task '$($script:TaskName)' (at logon, run-as-self, no elevation)."

    # Start it now so no re-logon is needed to finish setup.
    Start-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
    Write-Info "Started '$($script:TaskName)' (distro is now held alive)."
}

function Invoke-Setup {
    Write-Info "Dogany Windows (WSL2) setup -- distro: $DistroName"

    if (-not (Test-DistroExists -Name $DistroName)) {
        Fail-Loud "WSL distro '$DistroName' not found. Install it first (PowerShell as admin): wsl --install, then re-run this script. Pass -DistroName if you use a different distro."
    }

    # Step 1: .wslconfig
    $memGB = Get-MemoryAssignmentGB
    Set-WslConfig -MemGB $memGB

    # Step 2: /etc/wsl.conf systemd=true (root-side, before restart)
    Set-WslConfSystemd -Distro $DistroName

    # Step 3: restart WSL so both config files take effect
    Write-Info "Your Ubuntu window will close now -- expected."
    & wsl.exe --shutdown
    Start-Sleep -Seconds 2

    # Step 4: finish root-side config now that systemd is PID 1
    Complete-RootSideConfig -Distro $DistroName

    # Step 5: verify systemd is PID 1
    Assert-SystemdPid1 -Distro $DistroName

    # Step 6: register + start the keep-alive task
    Register-KeepAliveTask -Distro $DistroName

    # Step 7: summary
    Write-Host ''
    Write-Info "Windows setup complete."
    Write-Info "  .wslconfig: instanceIdleTimeout=-1, vmIdleTimeout=-1, guiApplications=false, memory=${memGB}GB"
    Write-Info "  /etc/wsl.conf: [boot] systemd=true"
    Write-Info "  systemd-timesyncd: disabled (WSL host time-sync recommended)"
    Write-Info "  marker: $($script:MarkerPath) = $($script:SetupVersion)"
    Write-Info "  scheduled task: $($script:TaskName) (at logon, non-elevated, running now)"
    Write-Host ''
    Write-Info "Recommended for always-on use: keep the machine on AC power and set sleep to Never:"
    Write-Info "  powercfg /change standby-timeout-ac 0"
    if ($memGB -le 8) {
        Write-Info "Voice note: this memory cap keeps the whisper auto-recommendation at 'small'. On a 16GB+ host you may re-run with -MemoryGB to raise the cap and unlock the medium voice model."
    }
    Write-Host ''
    Write-Info "Next: reopen Ubuntu and run:  cd ~/.dogany/framework && bash install.sh"
}

function Invoke-Uninstall {
    Write-Info "Dogany Windows (WSL2) uninstall -- distro: $DistroName"

    # 1. Unregister the keep-alive task.
    if (Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $script:TaskName -Confirm:$false
        Write-Info "Unregistered scheduled task '$($script:TaskName)'."
    } else {
        Write-Info "No scheduled task '$($script:TaskName)' to remove."
    }

    # 2. Remove the Dogany-written keys from .wslconfig (back up first).
    if (Test-Path $script:WslConfigPath) {
        $backup = "$($script:WslConfigPath).bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Copy-Item -LiteralPath $script:WslConfigPath -Destination $backup -Force
        Write-Info "Backed up .wslconfig -> $backup"

        # Prefer restoring the newest Dogany backup that predates our keys, else
        # strip only the keys we own.
        $ownedKeys = @('instanceIdleTimeout', 'vmIdleTimeout', 'guiApplications', 'memory')
        $lines = @(Get-Content -LiteralPath $script:WslConfigPath)
        $kept = @()
        foreach ($line in $lines) {
            $trim = $line.Trim()
            $drop = $false
            if ($trim -match '^([^#;=]+?)\s*=') {
                $k = $Matches[1].Trim()
                if ($ownedKeys -contains $k) { $drop = $true }
            }
            if (-not $drop) { $kept += $line }
        }
        Set-Content -LiteralPath $script:WslConfigPath -Value $kept -Encoding ASCII
        Write-Info "Removed Dogany keys from .wslconfig (unrelated content preserved)."
    }

    # 3. Remove the Linux-side marker if the distro still exists.
    if (Test-DistroExists -Name $DistroName) {
        & wsl.exe -d $DistroName -u root -- bash -c "rm -f /etc/dogany/windows-setup.version" 2>$null
        Write-Info "Removed $($script:MarkerPath)."
    }

    # 4. Print, never run, the data-destroying step.
    Write-Host ''
    Write-Warn "The following command DELETES THE ENTIRE Linux system in '$DistroName',"
    Write-Warn "including the agent's memory and all data. It is NOT run automatically."
    Write-Warn "Back up your instance folder first, then run it yourself only if you are sure:"
    Write-Host "    wsl --unregister $DistroName" -ForegroundColor Red
    Write-Host ''

    # 5. vhdx growth note.
    Write-Info "Disk note: WSL's ext4.vhdx grows and never auto-shrinks. To reclaim space:"
    Write-Info "  wsl --shutdown"
    Write-Info "  wsl --manage $DistroName --set-sparse true   (or Optimize-VHD on Pro/Enterprise SKUs)"
    Write-Host ''
    Write-Info "Uninstall complete (Windows-side state removed; distro left intact)."
}

if ($Uninstall) {
    Invoke-Uninstall
} else {
    Invoke-Setup
}
