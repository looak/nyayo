<#
.SYNOPSIS
  Sets up this Windows PC as a nyayo worker.

.DESCRIPTION
  Installs what a worker needs, if it is missing: Python, Git for Windows (the makefile's POSIX tools), make, and
  Visual Studio Build Tools with clang. It downloads the client from nyayo, checks its sha256, makes a venv, and writes
  run.cmd, which starts the worker. Nothing goes on the global PATH: run.cmd sets the toolchain for the worker only.

  The worker is pending until it is approved on nyayo's Lab, Machines page, unless you give it a join code made there.

  Run it again at any time: what is already there is kept, worker.token included.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup-windows-worker.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup-windows-worker.ps1 -Name laptop -Threads 4 -Join ABCD-1234 -StartAtLogon
#>
[CmdletBinding()]
param(
  [string]$Server = 'http://172.16.100.11:8001',
  [string]$Name = $env:COMPUTERNAME.ToLower(),
  [int]$Threads = 0,                       # 0: this PC's physical cores
  [string]$Dir = (Join-Path $env:USERPROFILE 'nyayo-worker'),
  [string]$Join = '',                      # a join code from Lab, Machines; used on the first start only
  [switch]$StartAtLogon,                   # start the worker when you sign in
  [string]$MakeDir = '',                   # a folder with make.exe, if you'd rather use your own
  [switch]$NoInstall                       # only check, don't install anything
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar on
$Server = $Server.TrimEnd('/')

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "   $text" -ForegroundColor Green }
function Fail($text) { Write-Host "`n$text" -ForegroundColor Red; exit 1 }

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
              [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Winget-Install($id, $what, $extra = @()) {
  if ($NoInstall) { Fail "$what is missing. Run without -NoInstall to install it, or install it yourself." }
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Fail "$what is missing, and winget isn't here to install it. Install 'App Installer' from the Microsoft Store, then run this again."
  }
  Write-Host "   installing $what ($id) with winget..."
  & winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements @extra
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {   # that code is 'already installed'
    Fail "winget couldn't install $what (exit $LASTEXITCODE)."
  }
  Refresh-Path
}

# ---------------------------------------------------------------------------------------------------------------
Step "nyayo at $Server"
try {
  $ref = Invoke-RestMethod "$Server/clientVersionRef/" -TimeoutSec 15
} catch {
  Fail "Can't reach $Server/clientVersionRef/. Is this PC on the same network as bobby? ($($_.Exception.Message))"
}
Ok "client version $($ref.client_version)"

# ---------------------------------------------------------------------------------------------------------------
Step 'Python'
function Find-Python {
  foreach ($c in @(@('py', '-3'), @('python'))) {
    $exe = Get-Command $c[0] -ErrorAction SilentlyContinue
    if (-not $exe -or $exe.Source -like '*WindowsApps*') { continue }   # the Store's stub isn't Python
    $args_ = @($c | Select-Object -Skip 1)
    $v = & $exe.Source @args_ -c 'import sys; print(sys.version_info >= (3, 9))' 2>$null
    if ($v -eq 'True') { return [pscustomobject]@{ Exe = $exe.Source; Args = $args_ } }
  }
  return $null
}
$found = Find-Python
if (-not $found) { Winget-Install 'Python.Python.3.12' 'Python 3.12'; $found = Find-Python }
if (-not $found) { Fail 'Python was installed, but this window can''t see it yet. Open a new window and run this again.' }
$pyExe = $found.Exe; $pyArgs = @($found.Args)
Ok (& $pyExe @pyArgs --version)

# ---------------------------------------------------------------------------------------------------------------
Step 'Git for Windows (sh, sed, find for the makefile)'
$gitRoot = $null
foreach ($p in @("$env:ProgramFiles\Git", "${env:ProgramFiles(x86)}\Git", "$env:LOCALAPPDATA\Programs\Git")) {
  if (Test-Path "$p\usr\bin\sh.exe") { $gitRoot = $p; break }
}
if (-not $gitRoot) {
  Winget-Install 'Git.Git' 'Git for Windows'
  foreach ($p in @("$env:ProgramFiles\Git", "$env:LOCALAPPDATA\Programs\Git")) {
    if (Test-Path "$p\usr\bin\sh.exe") { $gitRoot = $p; break }
  }
}
if (-not $gitRoot) { Fail 'Git for Windows is not where it should be (Program Files\Git).' }
Ok $gitRoot

# ---------------------------------------------------------------------------------------------------------------
Step 'make'
function Find-Make {
  if ($MakeDir) { if (Test-Path (Join-Path $MakeDir 'make.exe')) { return $MakeDir } else { Fail "No make.exe in $MakeDir." } }
  $m = Get-Command make -ErrorAction SilentlyContinue
  if ($m) { return Split-Path $m.Source }
  $links = "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
  if (Test-Path "$links\make.exe") { return $links }
  $pkg = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter make.exe -Recurse -ErrorAction SilentlyContinue |
         Select-Object -First 1
  if ($pkg) { return $pkg.DirectoryName }
  return $null
}
$makeDir = Find-Make
if (-not $makeDir) { Winget-Install 'ezwinports.make' 'GNU make'; $makeDir = Find-Make }
if (-not $makeDir) { Fail 'make was installed, but I can''t find make.exe. Open a new window and run this again.' }
Ok $makeDir

# ---------------------------------------------------------------------------------------------------------------
Step 'Visual Studio with clang'
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
function Find-VS {
  if (-not (Test-Path $vswhere)) { return $null }
  $p = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Llvm.Clang -property installationPath
  if ($p -and (Test-Path "$p\VC\Tools\Llvm\x64\bin\clang++.exe")) { return $p }
  return $null
}
$vs = Find-VS
if (-not $vs) {
  Write-Host '   Visual Studio Build Tools with clang are missing: a download of a few GB, and Windows asks for admin once.'
  Winget-Install 'Microsoft.VisualStudio.2022.BuildTools' 'Visual Studio Build Tools' @(
    '--override',
    '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Llvm.Clang --includeRecommended')
  $vs = Find-VS
}
if (-not $vs) {
  Fail ('Visual Studio is there but without clang. In the Visual Studio Installer, modify it and add ' +
        '"C++ Clang tools for Windows" (under Desktop development with C++), then run this again.')
}
Ok $vs

# ---------------------------------------------------------------------------------------------------------------
Step "The client, in $Dir"
$clientDir = Join-Path $Dir 'client'
New-Item -ItemType Directory -Force $clientDir | Out-Null
if (Test-Path (Join-Path $clientDir 'client.py')) {
  Ok 'already there; it updates itself from nyayo'
} else {
  $zip = Join-Path $env:TEMP "nyayo-client-$($ref.client_version).zip"
  Invoke-WebRequest $ref.client_url -OutFile $zip -UseBasicParsing
  $sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
  if ($sha -ne $ref.client_sha256.ToLower()) { Fail "client.zip's sha256 is $sha, nyayo says $($ref.client_sha256)." }
  Expand-Archive $zip $clientDir -Force
  Remove-Item $zip
  Ok "client $($ref.client_version), sha256 checked"
}

# ---------------------------------------------------------------------------------------------------------------
Step 'The venv'
$venvPy = Join-Path $Dir 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { & $pyExe @pyArgs -m venv (Join-Path $Dir 'venv') }
& $venvPy -m pip install --quiet --disable-pip-version-check -r (Join-Path $clientDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail 'pip couldn''t install the client''s requirements.' }
Ok 'requirements installed'

# ---------------------------------------------------------------------------------------------------------------
Step 'run.cmd'
if ($Threads -le 0) {
  $Threads = (Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum
}
$sockets = @(Get-CimInstance Win32_Processor).Count
$joinLine = ''
if ($Join) { $joinLine = "if not exist `"%~dp0client\worker.token`" set `"JOIN=--join $Join`"" }

$run = @"
@echo off
rem Starts this PC's nyayo worker, $Name, on $Server. Written by setup-windows-worker.ps1.
rem Nothing here is global: the toolchain is set for this worker only.
rem Compilers: Visual Studio's clang. make: GNU make. The makefile's POSIX tools (sh, sed, find...): Git's.
call "$vs\Common7\Tools\VsDevCmd.bat" -arch=x64 -no_logo
set "PATH=$vs\VC\Tools\Llvm\x64\bin;$gitRoot\usr\bin;$gitRoot\cmd;$makeDir;%PATH%"
rem Elephant's makefile says CXX := g++. MAKEFLAGS=e lets this CXX win, and its LTO needs the lld linker.
set "CXX=clang++ -fuse-ld=lld"
set MAKEFLAGS=e
set LDFLAGS=
rem The client starts fastchess-ob.exe from its own folder; some terminals stop Windows looking there.
set NoDefaultCurrentDirectoryInExePath=
set JOIN=
$joinLine
cd /d "%~dp0client"
"%~dp0venv\Scripts\python.exe" client.py --server $Server -T $Threads -N $sockets -I $Name %JOIN%
"@
Set-Content -Path (Join-Path $Dir 'run.cmd') -Value $run -Encoding ASCII
Ok "$Name, $Threads threads"

$readme = @"
# This PC's nyayo worker

``run.cmd`` starts it. It is ``$Name``, on ``$Server``, with $Threads threads.

- Keep this folder: ``client\worker.token`` is who this worker is, and the client updates itself in place.
- It waits until it is approved on nyayo's Lab, Machines page, unless it was started with a join code.
- Stop it: create the file ``client\openbench.exit``. It stops at once, mid-workload; the last results since its 30-second report are lost, the finished games are still uploaded.
- A token is good for one server. If the server changes, edit ``run.cmd``, delete ``client\worker.token`` and start it again.
- Set up again, or change the name or threads: run ``setup-windows-worker.ps1`` again.
"@
Set-Content -Path (Join-Path $Dir 'README.md') -Value $readme -Encoding UTF8

# ---------------------------------------------------------------------------------------------------------------
if ($StartAtLogon) {
  Step 'Start at sign-in'
  $action  = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$(Join-Path $Dir 'run.cmd')`""
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 `
               -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName 'nyayo worker' -Action $action -Trigger $trigger -Settings $set -Force | Out-Null
  Ok "scheduled task 'nyayo worker' starts it when $env:USERNAME signs in"
}

Step 'Done'
Write-Host "   Start it:  $(Join-Path $Dir 'run.cmd')"
if (-not $Join) { Write-Host "   Then approve '$Name' on $Server under Lab, Machines." }
