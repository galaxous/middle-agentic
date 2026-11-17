<#
.SYNOPSIS
    Bootstraps a Windows 10/11 machine as an SSH-reachable middle-agent node.

.DESCRIPTION
    Idempotent. Safe to run multiple times. Performs:
      1. Installs the OpenSSH.Server capability (if missing), sets sshd/ssh-agent to Automatic and starts them.
      2. Creates/enables the inbound firewall rule "OpenSSH-Server-In-TCP" (TCP 22).
      3. Creates a local user (default: sshuser), initialises its profile and adds it to the
         local Administrators group (resolved by SID, so localized Windows editions work).
      4. Writes the public key to:
           C:\Users\<user>\.ssh\authorized_keys
           C:\ProgramData\ssh\administrators_authorized_keys
         without BOM and without a ".txt" extension, then locks both files down with icacls.
      5. Optionally sets the OpenSSH DefaultShell (PowerShell or Git Bash) in the registry.
      6. Installs Python 3.12 and Tailscale via winget (joins the tailnet with -TsAuthKey).
      7. Role "llm": installs Ollama via winget and starts it on 127.0.0.1:11434.
      8. Role "gateway": opens TCP 8000 for tailnet addresses only.
      9. With -ProjectDir (a cloned repo with venv): runs `magent setup`, pulls the model and
         registers the gateway as a Scheduled Task (at startup, as SYSTEM).

.PARAMETER PublicKey
    The OpenSSH public key line, e.g. "ssh-ed25519 AAAA... user@host".

.PARAMETER UserName
    Local account that will log in over SSH. Default: sshuser.

.PARAMETER Password
    Optional password (SecureString). If the user does not exist and no password is given,
    a strong random password is generated and printed once.

.PARAMETER DefaultShell
    PowerShell | GitBash | None. Default: PowerShell.

.PARAMETER Roles
    Comma separated node roles: gateway, llm, client. Default: gateway,llm,client.

.PARAMETER TsAuthKey
    Tailscale auth key (tskey-auth-...). When given, the machine joins the tailnet unattended.

.PARAMETER Model
    Local model for the llm role. Default: the value in config.yaml (qwen2.5:3b).

.PARAMETER ProjectDir
    Path of a cloned middle-agentic repository whose venv is ready (bootstrap.py passes this).

.EXAMPLE
    .\scripts\bootstrap-windows.ps1 -PublicKey "ssh-ed25519 AAAA... me@macbook" -Roles gateway,llm -TsAuthKey tskey-auth-...

.EXAMPLE
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/galaxous/middle-agentic/main/scripts/bootstrap-windows.ps1))) -PublicKey "ssh-ed25519 AAAA..." -DefaultShell GitBash
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PublicKey,

    [ValidatePattern('^[A-Za-z0-9._-]{1,20}$')]
    [string]$UserName = 'sshuser',

    [System.Security.SecureString]$Password,

    [ValidateSet('PowerShell', 'GitBash', 'None')]
    [string]$DefaultShell = 'PowerShell',

    [ValidatePattern('^(gateway|llm|client)(,(gateway|llm|client))*$')]
    [string]$Roles = 'gateway,llm,client',

    [string]$TsAuthKey,

    [string]$Model,

    [string]$ProjectDir,

    [switch]$SkipTailscale,
    [switch]$SkipLlm,
    [switch]$SkipService
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Well-known SIDs: names differ on localized Windows (e.g. "Yöneticiler" on Turkish editions).
$AdministratorsSid = 'S-1-5-32-544'
$SystemSid = 'S-1-5-18'

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "    [OK] $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "    [WARN] $Message" -ForegroundColor Yellow }

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script must be run from an elevated (Run as Administrator) PowerShell session.'
    }
}

function Resolve-SidName([string]$Sid) {
    $identifier = New-Object System.Security.Principal.SecurityIdentifier($Sid)
    return $identifier.Translate([System.Security.Principal.NTAccount]).Value
}

function Assert-PublicKeyFormat([string]$Key) {
    $pattern = '^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)\s+[A-Za-z0-9+/]+={0,3}(\s+.*)?$'
    if ($Key.Trim() -notmatch $pattern) {
        throw "PublicKey does not look like a valid OpenSSH public key: '$Key'"
    }
}

function New-RandomPassword([int]$Length = 20) {
    $charSets = @('ABCDEFGHJKLMNPQRSTUVWXYZ', 'abcdefghijkmnopqrstuvwxyz', '23456789', '!@#%^*-_=+')
    $allChars = -join $charSets
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $buffer = New-Object byte[] 4
    $chars = New-Object System.Collections.Generic.List[char]

    # Guarantee at least one character from every set to satisfy complexity policies.
    foreach ($set in $charSets) {
        $rng.GetBytes($buffer)
        $chars.Add($set[[BitConverter]::ToUInt32($buffer, 0) % $set.Length])
    }
    while ($chars.Count -lt $Length) {
        $rng.GetBytes($buffer)
        $chars.Add($allChars[[BitConverter]::ToUInt32($buffer, 0) % $allChars.Length])
    }
    # Fisher-Yates shuffle so the guaranteed characters are not always at the front.
    for ($i = $chars.Count - 1; $i -gt 0; $i--) {
        $rng.GetBytes($buffer)
        $j = [BitConverter]::ToUInt32($buffer, 0) % ($i + 1)
        $tmp = $chars[$i]; $chars[$i] = $chars[$j]; $chars[$j] = $tmp
    }
    $rng.Dispose()
    return -join $chars
}

function Install-OpenSshServer {
    Write-Step 'Checking OpenSSH.Server capability'
    $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
    if ($null -eq $capability) {
        throw 'OpenSSH.Server capability is not available on this Windows build (requires Windows 10 1809+).'
    }
    if ($capability.State -ne 'Installed') {
        Write-Host "    Installing $($capability.Name) (this may take a few minutes)..."
        Add-WindowsCapability -Online -Name $capability.Name | Out-Null
        Write-Ok 'OpenSSH.Server installed'
    } else {
        Write-Ok 'OpenSSH.Server already installed'
    }

    foreach ($serviceName in @('sshd', 'ssh-agent')) {
        Set-Service -Name $serviceName -StartupType Automatic
        Start-Service -Name $serviceName
        Write-Ok "$serviceName service: Automatic + Running"
    }
}

function Enable-SshFirewallRule {
    Write-Step 'Configuring firewall rule OpenSSH-Server-In-TCP'
    $rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    if ($null -eq $rule) {
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any | Out-Null
        Write-Ok 'Firewall rule created (TCP 22 inbound)'
    } else {
        $rule | Enable-NetFirewallRule
        $rule | Set-NetFirewallRule -Profile Any -Action Allow -Direction Inbound
        Write-Ok 'Firewall rule already exists, ensured it is enabled'
    }
}

function Initialize-UserProfile([string]$Name) {
    # The profile folder must be created by Windows itself; pre-creating C:\Users\<user> manually
    # makes Windows create "C:\Users\<user>.<COMPUTER>" on first logon and the keys get lost.
    if (-not ([System.Management.Automation.PSTypeName]'MiddleAgent.UserEnvNative').Type) {
        Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
using System.Text;
namespace MiddleAgent {
    public static class UserEnvNative {
        [DllImport("userenv.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern int CreateProfile(string pszUserSid, string pszUserName, StringBuilder pszProfilePath, uint cchProfilePath);
    }
}
'@
    }

    $sid = (Get-LocalUser -Name $Name).SID.Value
    $profileKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid"

    if (-not (Test-Path $profileKey)) {
        $pathBuffer = New-Object System.Text.StringBuilder(260)
        $hresult = [MiddleAgent.UserEnvNative]::CreateProfile($sid, $Name, $pathBuffer, 260)
        $alreadyExists = -2147024713 # 0x800700B7 HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
        if ($hresult -ne 0 -and $hresult -ne $alreadyExists) {
            throw ('CreateProfile failed with HRESULT 0x{0:X8}' -f $hresult)
        }
    }

    $profilePath = (Get-ItemProperty -Path $profileKey -Name ProfileImagePath).ProfileImagePath
    return [Environment]::ExpandEnvironmentVariables($profilePath)
}

function Initialize-SshUser {
    Write-Step "Ensuring local user '$UserName'"
    $existing = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
    $generatedPassword = $null

    if ($null -eq $existing) {
        $securePassword = $Password
        if ($null -eq $securePassword) {
            $generatedPassword = New-RandomPassword
            $securePassword = ConvertTo-SecureString $generatedPassword -AsPlainText -Force
        }
        New-LocalUser -Name $UserName -Password $securePassword -PasswordNeverExpires `
            -AccountNeverExpires -Description 'Middle-agent SSH account' | Out-Null
        Write-Ok "User '$UserName' created"
    } else {
        if ($null -ne $Password) {
            Set-LocalUser -Name $UserName -Password $Password
            Write-Ok "User '$UserName' exists, password updated"
        } else {
            Write-Ok "User '$UserName' already exists"
        }
        if (-not $existing.Enabled) {
            Enable-LocalUser -Name $UserName
            Write-Ok "User '$UserName' enabled"
        }
    }

    $adminGroup = Get-LocalGroup -SID $AdministratorsSid
    try {
        Add-LocalGroupMember -Group $adminGroup -Member $UserName -ErrorAction Stop
        Write-Ok "Added '$UserName' to '$($adminGroup.Name)'"
    } catch {
        # Matched by error id: PowerShell 7 may load LocalAccounts via WinCompat, where typed catches fail.
        if ($_.FullyQualifiedErrorId -notlike 'MemberExists*') { throw }
        Write-Ok "'$UserName' is already a member of '$($adminGroup.Name)'"
    }

    if ($null -ne $generatedPassword) {
        Write-Warn "Generated password for '$UserName' (shown only once, store it safely): $generatedPassword"
    }
}

function Set-StrictKeyFileAcl([string]$Path, [string]$OwnerName, [string[]]$Principals) {
    # Remove inherited ACEs and grant Full Control only to the listed principals.
    & icacls.exe $Path /reset | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls /reset failed for $Path" }

    $grantArgs = @()
    foreach ($principal in $Principals) { $grantArgs += @('/grant:r', "${principal}:F") }
    & icacls.exe $Path /inheritance:r @grantArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls /inheritance:r /grant failed for $Path" }

    & icacls.exe $Path /setowner $OwnerName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls /setowner failed for $Path" }
}

function Add-KeyToFile([string]$Path, [string]$Key) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }

    # Leftovers from Notepad "Save as" break sshd silently; the real file must have no extension.
    $txtVariant = "$Path.txt"
    if (Test-Path $txtVariant) {
        Remove-Item -Path $txtVariant -Force
        Write-Warn "Removed stray file $txtVariant"
    }

    $lines = @()
    if (Test-Path $Path) {
        $lines = @([System.IO.File]::ReadAllLines($Path) | ForEach-Object { $_.Trim().TrimStart([char]0xFEFF) } | Where-Object { $_ })
    }

    if ($lines -contains $Key) {
        Write-Ok "Key already present in $Path"
    } else {
        $lines += $Key
        Write-Ok "Key added to $Path"
    }
    # Rewrite every time so an existing file with BOM/CRLF issues gets normalised.
    [System.IO.File]::WriteAllText($Path, (($lines -join "`n") + "`n"), $utf8NoBom)
}

function Install-AuthorizedKeys([string]$ProfilePath) {
    Write-Step 'Writing authorized keys'
    $key = ($PublicKey -replace '\s+', ' ').Trim()
    $adminsName = Resolve-SidName $AdministratorsSid
    $systemName = Resolve-SidName $SystemSid
    $userAccount = "$env:COMPUTERNAME\$UserName"

    $userSshDir = Join-Path $ProfilePath '.ssh'
    $userKeyFile = Join-Path $userSshDir 'authorized_keys'
    Add-KeyToFile -Path $userKeyFile -Key $key
    Set-StrictKeyFileAcl -Path $userKeyFile -OwnerName $userAccount -Principals @($adminsName, $systemName, $userAccount)
    Write-Ok "ACL locked: $userKeyFile ($adminsName, $systemName, $userAccount)"

    # Members of Administrators are matched by the "Match Group administrators" block in
    # sshd_config, which ignores the per-user file and reads this one instead.
    $adminKeyFile = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
    Add-KeyToFile -Path $adminKeyFile -Key $key
    Set-StrictKeyFileAcl -Path $adminKeyFile -OwnerName $adminsName -Principals @($adminsName, $systemName)
    Write-Ok "ACL locked: $adminKeyFile ($adminsName, $systemName)"

    return @{ User = $userKeyFile; Admin = $adminKeyFile }
}

function Set-OpenSshDefaultShell {
    Write-Step "Configuring OpenSSH DefaultShell ($DefaultShell)"
    $registryPath = 'HKLM:\SOFTWARE\OpenSSH'

    if ($DefaultShell -eq 'None') {
        Write-Ok 'DefaultShell left unchanged'
        return '(unchanged)'
    }

    $shellPath = switch ($DefaultShell) {
        'PowerShell' { Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe' }
        'GitBash' {
            @($env:ProgramFiles, ${env:ProgramFiles(x86)}) |
                Where-Object { $_ } |
                ForEach-Object { Join-Path $_ 'Git\bin\bash.exe' } |
                Where-Object { Test-Path $_ } |
                Select-Object -First 1
        }
    }

    if (-not $shellPath -or -not (Test-Path $shellPath)) {
        Write-Warn "Shell executable for '$DefaultShell' not found. Install Git for Windows (https://git-scm.com/download/win) and re-run. DefaultShell left unchanged."
        return '(not found)'
    }

    if (-not (Test-Path $registryPath)) { New-Item -Path $registryPath -Force | Out-Null }
    New-ItemProperty -Path $registryPath -Name DefaultShell -Value $shellPath -PropertyType String -Force | Out-Null
    Write-Ok "DefaultShell = $shellPath"
    return $shellPath
}

# ------------------------------------------------------------------ middle-agent node (roles)
function Test-RoleEnabled([string]$Role) {
    return (($Roles -split ',') | ForEach-Object { $_.Trim() }) -contains $Role
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Install-WingetPackage([string]$Id, [string]$DisplayName) {
    # Native tools write progress to stderr; with 'Stop' PowerShell 5.1 would turn that into an error.
    $ErrorActionPreference = 'Continue'
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($null -eq $winget) {
        Write-Warn "winget not found; install $DisplayName manually (winget ships with 'App Installer' from the Microsoft Store)"
        return $false
    }
    Write-Host "    Installing $DisplayName via winget..."
    & $winget.Source install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Null
    $alreadyInstalled = -1978335189 # 0x8A15002B APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne $alreadyInstalled) {
        Write-Warn "winget returned $LASTEXITCODE while installing $Id"
        return $false
    }
    Update-SessionPath
    Write-Ok "$DisplayName installed"
    return $true
}

function Test-PythonCommand([string[]]$Command) {
    $ErrorActionPreference = 'Continue'
    try {
        $arguments = @($Command | Select-Object -Skip 1) + @('-c', 'import sys; print(sys.version_info >= (3, 10))')
        $output = & $Command[0] @arguments 2>$null
        return ($LASTEXITCODE -eq 0 -and "$output".Trim() -eq 'True')
    } catch {
        return $false
    }
}

function Install-Python {
    Write-Step 'Checking Python 3.10+'
    if ((Test-PythonCommand @('py', '-3')) -or (Test-PythonCommand @('python'))) {
        Write-Ok 'Python 3.10+ available'
        return
    }
    Install-WingetPackage -Id 'Python.Python.3.12' -DisplayName 'Python 3.12' | Out-Null
}

function Get-TailscaleExe {
    $default = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $default) { return $default }
    $command = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Install-Tailscale {
    Write-Step 'Configuring Tailscale'
    $ErrorActionPreference = 'Continue'
    $tailscale = Get-TailscaleExe
    if (-not $tailscale) {
        Install-WingetPackage -Id 'Tailscale.Tailscale' -DisplayName 'Tailscale' | Out-Null
        $tailscale = Get-TailscaleExe
    } else {
        Write-Ok "Tailscale already installed ($tailscale)"
    }
    if (-not $tailscale) {
        Write-Warn 'Tailscale is not available; nodes will only be reachable on the LAN'
        return $null
    }

    & $tailscale status *> $null
    if ($LASTEXITCODE -ne 0) {
        if (-not $TsAuthKey) {
            Write-Warn 'Tailscale is installed but not connected: run "tailscale up --unattended" or pass -TsAuthKey'
            return $null
        }
        # --unattended keeps the node on the tailnet when nobody is logged in (headless worker).
        & $tailscale up --authkey $TsAuthKey --unattended --hostname ($env:COMPUTERNAME.ToLower()) | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "tailscale up failed with exit code $LASTEXITCODE"
            return $null
        }
    }
    $ip = (& $tailscale ip -4 2>$null | Select-Object -First 1)
    Write-Ok "Connected to tailnet as $ip"
    return $ip
}

function Test-OllamaApi {
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-OllamaExe {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($found) { return $found }
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Install-Ollama {
    Write-Step 'Installing local LLM runtime (Ollama)'
    $ollama = Get-OllamaExe
    if (-not $ollama) {
        Install-WingetPackage -Id 'Ollama.Ollama' -DisplayName 'Ollama' | Out-Null
        $ollama = Get-OllamaExe
    } else {
        Write-Ok "Ollama already installed ($ollama)"
    }
    if (-not $ollama) {
        Write-Warn 'Ollama is not available; the gateway will skip the local tier'
        return $false
    }

    if (-not (Test-OllamaApi)) {
        Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
        for ($i = 0; $i -lt 30 -and -not (Test-OllamaApi); $i++) { Start-Sleep -Seconds 1 }
    }
    if (-not (Test-OllamaApi)) {
        Write-Warn 'Ollama API is not reachable on 127.0.0.1:11434'
        return $false
    }
    Write-Ok 'Ollama API listening on 127.0.0.1:11434 (remote access goes through the gateway)'
    Write-Warn 'Ollama for Windows starts at user logon; enable auto-logon on a headless worker, otherwise the local tier is skipped until someone logs in'
    return $true
}

function Enable-GatewayFirewallRule {
    Write-Step 'Allowing gateway port 8000 from tailnet addresses only'
    $name = 'MiddleAgent-Gateway-In-TCP'
    $tailnetRanges = @('100.64.0.0/10', 'fd7a:115c:a1e0::/48')
    $rule = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
    if ($null -eq $rule) {
        New-NetFirewallRule -Name $name -DisplayName 'middle-agentic gateway (tailnet only)' -Direction Inbound `
            -Protocol TCP -LocalPort 8000 -RemoteAddress $tailnetRanges -Action Allow -Profile Any | Out-Null
        Write-Ok 'Firewall rule created (TCP 8000 from 100.64.0.0/10)'
    } else {
        $rule | Set-NetFirewallRule -Enabled True -Action Allow -LocalPort 8000 -RemoteAddress $tailnetRanges
        Write-Ok 'Firewall rule already exists, ensured it is enabled'
    }
}

function Initialize-MiddleAgentNode {
    $venvPython = Join-Path $ProjectDir 'venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        Write-Warn "venv not found in $ProjectDir; run 'python bootstrap.py ...' from the repository"
        return
    }
    $ErrorActionPreference = 'Continue'

    Write-Step "Writing node configuration (roles: $Roles)"
    $setupArgs = @('-m', 'middle_agentic', 'setup', '--roles', $Roles, '--no-pull')
    if ($Model) { $setupArgs += @('--model', $Model) }
    & $venvPython @setupArgs | Out-Host

    if ((Test-RoleEnabled 'llm') -and -not $SkipLlm -and (Test-OllamaApi)) {
        Write-Step 'Pulling local model'
        & $venvPython -m middle_agentic llm pull | Out-Host
    }
    if ((Test-RoleEnabled 'gateway') -and -not $SkipService) {
        Write-Step 'Registering gateway Scheduled Task (at startup, as SYSTEM)'
        & $venvPython -m middle_agentic service install | Out-Host
    }
}

# ------------------------------------------------------------------ main
Assert-Administrator
Assert-PublicKeyFormat $PublicKey

Install-OpenSshServer
Enable-SshFirewallRule
Initialize-SshUser

Write-Step "Initialising profile for '$UserName'"
$profilePath = Initialize-UserProfile -Name $UserName
Write-Ok "Profile path: $profilePath"

$keyFiles = Install-AuthorizedKeys -ProfilePath $profilePath
$shell = Set-OpenSshDefaultShell

Write-Step 'Restarting sshd to apply configuration'
Restart-Service -Name sshd
Write-Ok 'sshd restarted'

Install-Python

$tailscaleIp = $null
if (-not $SkipTailscale) { $tailscaleIp = Install-Tailscale }

$ollamaReady = $false
if ((Test-RoleEnabled 'llm') -and -not $SkipLlm) { $ollamaReady = Install-Ollama }

if (Test-RoleEnabled 'gateway') { Enable-GatewayFirewallRule }

if ($ProjectDir) {
    Initialize-MiddleAgentNode
} else {
    Write-Warn 'No -ProjectDir: clone https://github.com/galaxous/middle-agentic and run "python bootstrap.py --pubkey ..." to finish the node setup'
}

$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress)

Write-Host ''
Write-Host '================ Bootstrap summary ================' -ForegroundColor Magenta
[pscustomobject]@{
    'User'                   = $UserName
    'Roles'                  = $Roles
    'sshd'                   = (Get-Service sshd).Status
    'Firewall rules'         = 'OpenSSH-Server-In-TCP (TCP 22)' + $(if (Test-RoleEnabled 'gateway') { ', MiddleAgent-Gateway-In-TCP (TCP 8000, tailnet only)' } else { '' })
    'User authorized_keys'   = $keyFiles.User
    'Admin authorized_keys'  = $keyFiles.Admin
    'DefaultShell'           = $shell
    'Tailscale IP'           = $(if ($tailscaleIp) { $tailscaleIp } else { 'not connected' })
    'Ollama'                 = $(if ($ollamaReady) { 'running on 127.0.0.1:11434' } else { 'not running' })
    'IPv4 addresses'         = ($addresses -join ', ')
} | Format-List
Write-Host "Connect from your Mac:  ssh $UserName@$(if ($addresses) { $addresses[0] } else { '<this-host>' })" -ForegroundColor Green
