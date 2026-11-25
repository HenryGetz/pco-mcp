<#
.SYNOPSIS
    One-line installer for the Planning Center MCP server.

.DESCRIPTION
    Downloads the main branch, installs it under
    %LOCALAPPDATA%\pco-mcp (or PCO_MCP_HOME), creates a virtual environment,
    installs dependencies, optionally saves Planning Center credentials, and
    prints an MCP client snippet.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/HenryGetz/pco-mcp/main/install.ps1 | iex"

.EXAMPLE
    .\install.ps1 --help
#>

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # Older/newer runtimes may not allow changing this; Invoke-WebRequest still works.
}

$TarballUrl = 'https://github.com/HenryGetz/pco-mcp/archive/refs/heads/main.tar.gz'
$ZipUrl = 'https://github.com/HenryGetz/pco-mcp/archive/refs/heads/main.zip'
$ReadmeUrl = 'https://github.com/HenryGetz/pco-mcp#readme'

if ($args -contains '--help' -or $args -contains '-h') {
    Write-Host @'
Planning Center MCP installer

Installs the Planning Center MCP server and prepares it for any MCP-capable
AI app. You can run this script again at any time to update; your credentials
are never removed.

Usage:
  powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/HenryGetz/pco-mcp/main/install.ps1 | iex"
  .\install.ps1 --help

Install location:
  $env:LOCALAPPDATA\pco-mcp  (override with PCO_MCP_HOME)

Environment variables:
  PCO_MCP_HOME          Where to install.
  PCO_MCP_SOURCE        Install from a local checkout instead of downloading.
  PCO_NONINTERACTIVE=1  Never prompt; skip credential setup if none is supplied.
  PCO_SETUP_ACCOUNT     Account nickname (default: main).
  PCO_SETUP_ORG_NAME    Organization display name (default: the nickname).
  PCO_SETUP_APP_ID      Planning Center application ID.
  PCO_SETUP_APP_SECRET  Planning Center application secret.
  PCO_SETUP_OVERWRITE=1 Replace an existing account with the same nickname.

Credentials live in %USERPROFILE%\.mpco_credentials.json.
Create a Personal Access Token at:
  https://api.planningcenteronline.com/oauth/applications
'@
    return
}

function Get-CredentialsInfo {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ Kind = 'missing'; Names = @() }
    }

    try {
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return [pscustomobject]@{ Kind = 'empty'; Names = @() }
        }
        $data = $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ Kind = 'invalid'; Names = @() }
    }

    if ($null -eq $data -or $data -is [System.Array] -or $data -is [string] -or $data -is [ValueType]) {
        return [pscustomobject]@{ Kind = 'invalid'; Names = @() }
    }

    $names = New-Object System.Collections.Generic.List[string]
    foreach ($property in $data.PSObject.Properties) {
        $entry = $property.Value
        if ($entry -is [pscustomobject]) {
            $idProperty = $entry.PSObject.Properties['appId']
            $secretProperty = $entry.PSObject.Properties['appSecret']
            $hasId = $idProperty -and -not [string]::IsNullOrWhiteSpace([string]$idProperty.Value)
            $hasSecret = $secretProperty -and -not [string]::IsNullOrWhiteSpace([string]$secretProperty.Value)
            if ($hasId -or $hasSecret) {
                $names.Add($property.Name)
            }
        }
    }

    if ($names.Count -eq 0) {
        return [pscustomobject]@{ Kind = 'empty'; Names = @() }
    }
    return [pscustomobject]@{ Kind = 'ok'; Names = $names.ToArray() }
}

function Write-CredentialsFile {
    param(
        [string]$Path,
        [string]$Account,
        [string]$OrgName,
        [string]$AppId,
        [string]$AppSecret
    )

    if ([string]::IsNullOrWhiteSpace($Account) -or
        [string]::IsNullOrWhiteSpace($AppId) -or
        [string]::IsNullOrWhiteSpace($AppSecret)) {
        throw 'Account nickname, application ID, and secret are all required.'
    }

    $overwrite = $env:PCO_SETUP_OVERWRITE -eq '1'
    $data = [ordered]@{}

    if (Test-Path -LiteralPath $Path) {
        try {
            $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
            if (-not [string]::IsNullOrWhiteSpace($raw)) {
                $existing = $raw | ConvertFrom-Json -ErrorAction Stop
                if ($null -eq $existing -or $existing -is [System.Array] -or
                    $existing -is [string] -or $existing -is [ValueType]) {
                    throw 'the file must contain a JSON object of accounts'
                }
                foreach ($property in $existing.PSObject.Properties) {
                    $data[$property.Name] = $property.Value
                }
            }
        } catch {
            throw "The existing credentials file $Path could not be read ($($_.Exception.Message)). Fix or move it, then rerun the installer."
        }
    }

    if ($data.Contains($Account) -and -not $overwrite) {
        Write-Host "An account named '$Account' already exists in $Path; leaving it unchanged."
        Write-Host 'Set PCO_SETUP_OVERWRITE=1 to replace it.'
        return
    }

    $org = if ([string]::IsNullOrWhiteSpace($OrgName)) { $Account } else { $OrgName.Trim() }
    $data[$Account] = [pscustomobject]@{
        appId     = $AppId.Trim()
        appSecret = $AppSecret
        orgName   = $org
    }

    $json = $data | ConvertTo-Json -Depth 10
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $temporary = Join-Path $directory ('.mpco_credentials.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backup = $temporary + '.bak'
    try {
        $encoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
        [IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, $encoding)
        if (Test-Path -LiteralPath $Path) {
            [IO.File]::Replace($temporary, $Path, $backup)
        } else {
            [IO.File]::Move($temporary, $Path)
        }
    } catch {
        foreach ($leftover in @($temporary, $backup)) {
            if (Test-Path -LiteralPath $leftover) {
                Remove-Item -LiteralPath $leftover -Force -ErrorAction SilentlyContinue
            }
        }
        throw "Could not save credentials to $Path ($($_.Exception.Message))."
    }
    if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $backup) {
            Write-Warning "An old copy of the credentials file remains at $backup. Delete it manually."
        }
    }

    # Best effort on Windows: restrict the credentials file to the current user.
    try {
        $acl = Get-Acl -LiteralPath $Path
        $acl.SetAccessRuleProtection($true, $false)
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'Allow')
        $acl.SetAccessRule($rule)
        Set-Acl -LiteralPath $Path -AclObject $acl
    } catch {
        Write-Warning "Could not restrict permissions on $Path. Consider restricting it to your Windows account."
    }

    # Validate the file we just wrote.
    $check = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $entry = $check.PSObject.Properties[$Account]
    if (-not $entry -or $entry.Value.appId -ne $AppId.Trim() -or $entry.Value.appSecret -ne $AppSecret) {
        throw "Could not validate the credentials written to $Path."
    }
    Write-Host "Saved credentials for '$Account' to $Path."
}

function Get-PythonCommand {
    $candidates = @(
        [pscustomobject]@{ Name = 'py'; Prefix = @('-3') },
        [pscustomobject]@{ Name = 'python'; Prefix = @() },
        [pscustomobject]@{ Name = 'python3'; Prefix = @() }
    )
    $found = @()

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue
        if (-not $command) { continue }

        $prefix = $candidate.Prefix
        $version = & $command.Source @prefix -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$version)) { continue }
        $found += "$($candidate.Name) $version"

        & $command.Source @prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Command = $command.Source; Prefix = @($prefix); Version = [string]$version }
        }
    }

    return [pscustomobject]@{ Command = $null; Prefix = @(); Version = $null; Found = $found }
}

function Read-Value {
    param(
        [string]$Prompt,
        [string]$Default
    )
    while ($true) {
        $label = if ($Default) { "$Prompt [$Default]" } else { $Prompt }
        $value = Read-Host $label
        if ($null -eq $value) { $value = '' }
        $value = $value.Trim()
        if ($value) { return $value }
        if ($Default) { return $Default }
        Write-Host 'Please enter a value.'
    }
}

function Read-SecretValue {
    while ($true) {
        $secure = Read-Host 'Application Secret (typing is hidden)' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        if ($value) { return $value }
        Write-Host 'Please enter the application secret.'
    }
}

$TempDir = $null
try {
    $LocalAppData = [Environment]::GetFolderPath('LocalApplicationData')
    if ($env:PCO_MCP_HOME) {
        $InstallDir = $env:PCO_MCP_HOME
    } else {
        $InstallDir = Join-Path $LocalAppData 'pco-mcp'
    }
    $CredsFile = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.mpco_credentials.json'

    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $InstallDir = [IO.Path]::GetFullPath($InstallDir)

    $TempDir = Join-Path ([IO.Path]::GetTempPath()) ('pco-mcp-install-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $TempDir -Force | Out-Null

    $python = Get-PythonCommand
    if (-not $python.Command) {
        $foundText = (@($python.Found) -join '; ')
        if ($foundText) {
            throw "Python 3.10 or newer is required. Found: $foundText. Install a newer Python from https://www.python.org/downloads/ and rerun."
        }
        throw 'Python 3 was not found. Install Python 3.10 or newer from https://www.python.org/downloads/ and rerun.'
    }
    $Python = $python.Command
    $PythonPrefix = $python.Prefix

    & $Python @PythonPrefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Note: Python $($python.Version) found. Python 3.12 or newer is recommended, but $($python.Version) works."
    }

    $SourceDir = $env:PCO_MCP_SOURCE
    if ($SourceDir) {
        if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
            throw "PCO_MCP_SOURCE points to '$SourceDir', which is not a folder."
        }
        $SourceDir = (Resolve-Path -LiteralPath $SourceDir).ProviderPath
    } else {
        $ArchiveRoot = Join-Path $TempDir 'src'
        New-Item -ItemType Directory -Path $ArchiveRoot -Force | Out-Null

        Write-Host 'Downloading the latest version...'
        $Tarball = Join-Path $TempDir 'pco-mcp.tar.gz'
        Invoke-WebRequest -Uri $TarballUrl -OutFile $Tarball -UseBasicParsing

        $tarCommand = Get-Command tar.exe -ErrorAction SilentlyContinue
        if ($tarCommand) {
            & $tarCommand.Source -xzf $Tarball -C $ArchiveRoot
            if ($LASTEXITCODE -ne 0) {
                throw 'Could not unpack the downloaded files.'
            }
        } else {
            # Expand-Archive only reads .zip, so use the zip archive when tar.exe is absent.
            $Zip = Join-Path $TempDir 'pco-mcp.zip'
            Invoke-WebRequest -Uri $ZipUrl -OutFile $Zip -UseBasicParsing
            Expand-Archive -LiteralPath $Zip -DestinationPath $ArchiveRoot -Force
        }

        $extracted = Get-ChildItem -LiteralPath $ArchiveRoot -Directory | Select-Object -First 1
        if (-not $extracted) {
            throw 'The download did not contain the expected files.'
        }
        $SourceDir = $extracted.FullName
    }

    $ServerSource = Join-Path $SourceDir 'server.py'
    $RequirementsSource = Join-Path $SourceDir 'requirements.txt'
    if (-not (Test-Path -LiteralPath $ServerSource)) {
        throw 'The server files are incomplete (server.py is missing).'
    }
    if (-not (Test-Path -LiteralPath $RequirementsSource)) {
        throw 'The server files are incomplete (requirements.txt is missing).'
    }

    $sourceFull = [IO.Path]::GetFullPath($SourceDir).TrimEnd('\')
    $installFull = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
    if (-not [string]::Equals($sourceFull, $installFull, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "Installing files to $InstallDir..."
        $excludeNames = @('.git', 'venv', '__pycache__', '.env', '.DS_Store')
        $children = Get-ChildItem -LiteralPath $SourceDir -Force | Where-Object {
            $name = $_.Name
            if ($excludeNames -contains $name) { return $false }
            if ($name -like 'recon*.md' -or $name -like 'recon*.json') { return $false }
            if ($name -like '*_history.*') { return $false }
            if ($name -like 'server.py.bak-*' -or $name -like 'server.py.pre-repo-*') { return $false }
            if ($_.FullName.TrimEnd('\') -eq $installFull) { return $false }
            return $true
        }
        $children | Copy-Item -Destination $InstallDir -Recurse -Force

        Get-ChildItem -LiteralPath $InstallDir -Directory -Recurse -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath $InstallDir -File -Recurse -Filter '*.pyc' -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "Using the files already in $InstallDir."
    }

    $VenvDir = Join-Path $InstallDir 'venv'
    $VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
    $venvUsable = $false
    if (Test-Path -LiteralPath $VenvPython) {
        & $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $venvUsable = $true }
    }
    if (-not $venvUsable) {
        if (Test-Path -LiteralPath $VenvDir) {
            Write-Host 'The existing Python environment is unusable; recreating it.'
            Remove-Item -LiteralPath $VenvDir -Recurse -Force
        }
        Write-Host 'Creating a Python environment...'
        & $Python @PythonPrefix -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not create a Python environment. Install Python 3.10 or newer and rerun.'
        }
        if (-not (Test-Path -LiteralPath $VenvPython)) {
            throw "Python created an environment without a usable interpreter at $VenvPython."
        }
    }

    & $VenvPython -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        & $VenvPython -m ensurepip --upgrade *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'The Python environment is missing pip. Reinstall Python 3 and rerun.'
        }
    }

    Write-Host 'Installing dependencies (this can take a minute)...'
    $Requirements = Join-Path $InstallDir 'requirements.txt'
    & $VenvPython -m pip install --disable-pip-version-check --no-input --quiet -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not install the Python dependencies. Check your internet connection and rerun.'
    }

    Write-Host 'Checking the server...'
    Push-Location -LiteralPath $InstallDir
    try {
        $smokeName = [string](& $VenvPython -c "import server; print(server.mcp.name)")
        $smokeExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($smokeExit -ne 0) {
        throw "The server was installed but did not start. Rerun the installer; if it keeps failing, see $ReadmeUrl."
    }
    if (-not $smokeName) { $smokeName = 'planning-center' }
    Write-Host "Server check passed: $smokeName."

    $creds = Get-CredentialsInfo -Path $CredsFile
    $envAppId = $env:PCO_SETUP_APP_ID
    $envSecret = $env:PCO_SETUP_APP_SECRET

    if ($envAppId -or $envSecret) {
        if (-not $envAppId) { throw 'PCO_SETUP_APP_SECRET is set, but PCO_SETUP_APP_ID is missing.' }
        if (-not $envSecret) { throw 'PCO_SETUP_APP_ID is set, but PCO_SETUP_APP_SECRET is missing.' }
        $account = if ($env:PCO_SETUP_ACCOUNT) { $env:PCO_SETUP_ACCOUNT.Trim() } else { 'main' }
        $org = if ($env:PCO_SETUP_ORG_NAME) { $env:PCO_SETUP_ORG_NAME.Trim() } else { '' }
        Write-CredentialsFile -Path $CredsFile -Account $account -OrgName $org -AppId $envAppId -AppSecret $envSecret
        $creds = Get-CredentialsInfo -Path $CredsFile
    } elseif ($creds.Kind -eq 'ok') {
        Write-Host ("Planning Center credentials are already configured for: " + ($creds.Names -join ', ') + '. Leaving them unchanged.')
    } elseif ($creds.Kind -eq 'invalid') {
        Write-Warning "$CredsFile exists but could not be read as JSON. It was left untouched."
        Write-Warning 'Fix or move that file, then rerun the installer to add credentials.'
    } elseif ($env:PCO_NONINTERACTIVE -eq '1') {
        Write-Host 'Skipping credential setup because PCO_NONINTERACTIVE=1 and no PCO_SETUP_APP_ID/PCO_SETUP_APP_SECRET were supplied.'
    } else {
        $hasConsole = $true
        try { $hasConsole = -not [Console]::IsInputRedirected } catch { $hasConsole = $true }
        if (-not $hasConsole) {
            Write-Host 'No interactive console is available, so credential prompts were skipped.'
            Write-Host 'Rerun the installer in a terminal, or set PCO_SETUP_APP_ID and PCO_SETUP_APP_SECRET.'
        } else {
            Write-Host ''
            Write-Host 'Planning Center access needs a Personal Access Token.'
            Write-Host 'Create one here (or open an existing application):'
            Write-Host '  https://api.planningcenteronline.com/oauth/applications'
            Write-Host 'Copy the Application ID and Secret from that page.'
            Write-Host ''
            $account = Read-Value -Prompt 'Account nickname' -Default 'main'
            $org = Read-Value -Prompt 'Organization display name (optional)' -Default $account
            $appId = Read-Value -Prompt 'Application ID' -Default ''
            $secret = Read-SecretValue
            Write-CredentialsFile -Path $CredsFile -Account $account -OrgName $org -AppId $appId -AppSecret $secret
            $creds = Get-CredentialsInfo -Path $CredsFile
        }
    }

    $ServerPath = Join-Path $InstallDir 'server.py'
    Write-Host ''
    Write-Host 'Planning Center MCP is installed.'
    Write-Host ''
    Write-Host "  Install folder: $InstallDir"
    Write-Host "  Python:         $VenvPython"
    Write-Host "  Server:         $ServerPath"
    if ($creds.Kind -eq 'ok') {
        Write-Host ("  Planning Center: connected (" + ($creds.Names -join ', ') + ')')
    } elseif ($creds.Kind -eq 'invalid') {
        Write-Host '  Planning Center: credentials file needs attention'
    } else {
        Write-Host '  Planning Center: not connected yet'
    }

    Write-Host ''
    Write-Host "Add this to your AI app's MCP configuration:"
    Write-Host ''
    $config = [ordered]@{
        mcpServers = [ordered]@{
            pco = [ordered]@{
                command = $VenvPython
                args    = @($ServerPath)
            }
        }
    }
    $config | ConvertTo-Json -Depth 6
    Write-Host ''
    Write-Host 'Then restart your AI app and ask:'
    Write-Host '  "List my Planning Center service types."'
    Write-Host ''
    if ($creds.Kind -ne 'ok') {
        Write-Host 'Credentials are not set up yet. Rerun this installer in a terminal, or see the README to add %USERPROFILE%\.mpco_credentials.json.'
        Write-Host ''
    }
    Write-Host "Client-specific setup examples are in the README: $ReadmeUrl"
} catch {
    Write-Host ''
    Write-Host ("Error: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    if ($TempDir -and (Test-Path -LiteralPath $TempDir)) {
        Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
