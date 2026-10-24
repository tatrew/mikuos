<#
  MikuOS launcher.
    .\run_mikuos.ps1              overlay + backend (default)
    .\run_mikuos.ps1 -Cli         backend in background + terminal chat
    .\run_mikuos.ps1 -BackendOnly backend in this console (Ctrl+C to stop)
    .\run_mikuos.ps1 -Install     (re)create .venv and install requirements
    .\run_mikuos.ps1 -Test        run automated tests (+ live tests if LM Studio is up)
    .\run_mikuos.ps1 -Rebuild3D   rebuild the 3D Miku from assets\miku_model (needs Blender)
    .\run_mikuos.ps1 -Shortcut    create a "MikuOS" shortcut on the desktop
  Everything else (venv, LM Studio server + model, 3D model build) happens automatically.
#>
param([switch]$Cli, [switch]$BackendOnly, [switch]$Install, [switch]$Test, [switch]$NoLmStudio,
      [switch]$Rebuild3D, [switch]$Shortcut)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$pyw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"

function Ensure-Venv {
    if ((Test-Path $py) -and -not $Install) { return }
    Write-Host "Creating Python 3.12 environment..." -ForegroundColor Cyan
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv venv --python 3.12 .venv
        uv pip install --python $py -r requirements.txt
    } else {
        py -3.12 -m venv .venv
        & $py -m pip install -r requirements.txt
    }
    if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }
}

function Ensure-LmStudio {
    if ($NoLmStudio) { return }
    $settings = Get-Content (Join-Path $PSScriptRoot "config\settings.json") -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
    $base = if ($settings) { $settings.lmstudio.base_url } else { "http://127.0.0.1:1234/v1" }
    $native = $base -replace "/v1$", "/api/v0"
    $lms = Join-Path $HOME ".lmstudio\bin\lms.exe"
    try { $models = (Invoke-RestMethod "$native/models" -TimeoutSec 3).data }
    catch {
        if (Test-Path $lms) {
            Write-Host "Starting LM Studio server..." -ForegroundColor Cyan
            & $lms server start | Out-Null
            Start-Sleep 2
            try { $models = (Invoke-RestMethod "$native/models" -TimeoutSec 5).data } catch { $models = $null }
        }
    }
    if (-not $models) { Write-Warning "LM Studio is not reachable: MikuOS will run without the LLM (memory and pet still work)."; return }
    $loaded = $models | Where-Object { $_.state -eq "loaded" -and $_.type -in @("llm", "vlm") }
    if ($loaded) { Write-Host ("LM Studio model: " + $loaded[0].id) -ForegroundColor Green; return }
    $prefs = if ($settings) { $settings.lmstudio.model_preference } else { @("miku") }
    $cand = $null
    foreach ($m in $models) {
        if ($m.type -ne "llm") { continue }
        foreach ($p in $prefs) { if ($m.id.ToLower().Contains($p.ToLower())) { $cand = $m; break } }
        if ($cand) { break }
    }
    if ($cand -and (Test-Path $lms)) {
        Write-Host ("Loading " + $cand.id + " in LM Studio...") -ForegroundColor Cyan
        & $lms load $cand.id --context-length 8192 -y | Out-Null
    } else {
        Write-Warning "No model loaded in LM Studio. Load the Miku model in LM Studio, MikuOS will connect automatically."
    }
}

function Ensure-Model3D {
    $built = Join-Path $PSScriptRoot "assets\miku3d\qml\Miku.qml"
    $source = Join-Path $PSScriptRoot "assets\miku_model\source\GameObject\GameObject.blend"
    if (((Test-Path $built) -and -not $Rebuild3D) -or -not (Test-Path $source)) { return }
    $blender = Get-ChildItem "C:\Program Files\Blender Foundation\Blender *\blender.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName | Select-Object -Last 1
    if (-not $blender) { Write-Warning "Blender not found: showing the placeholder Miku (install Blender, then run with -Rebuild3D)."; return }
    Write-Host "Building the 3D Miku (one time, ~1 min)..." -ForegroundColor Cyan
    & $py (Join-Path $PSScriptRoot "tools\build_miku3d.py") --blender $blender.FullName
}

function New-DesktopShortcut {
    $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "MikuOS.lnk"
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = Join-Path $PSScriptRoot "run_mikuos.bat"
    $sc.WorkingDirectory = $PSScriptRoot
    $sc.WindowStyle = 7  # minimized console
    $sc.Description = "MikuOS - Hatsune Miku desktop companion"
    $sc.Save()
    Write-Host "Shortcut created: $lnk" -ForegroundColor Green
}

Ensure-Venv
if ($Shortcut) { New-DesktopShortcut; exit 0 }
if ($Test) {
    & $py -m pytest tests -p no:warnings -v
    exit $LASTEXITCODE
}
Ensure-LmStudio
Ensure-Model3D

if ($BackendOnly) {
    & $py -m mikuos
} elseif ($Cli) {
    $backend = Start-Process -FilePath $py -ArgumentList "-m", "mikuos" -WindowStyle Hidden -PassThru
    for ($i = 0; $i -lt 30; $i++) {
        try { Invoke-RestMethod "http://127.0.0.1:8765/health" -TimeoutSec 1 | Out-Null; break } catch { Start-Sleep 1 }
    }
    try { & $py -m mikuos.cli } finally { Stop-Process -Id $backend.Id -ErrorAction SilentlyContinue }
} else {
    Start-Process -FilePath $pyw -ArgumentList "-m", "mikuos_client" -WorkingDirectory $PSScriptRoot
    Write-Host "MikuOS started: Miku is on your desktop, tray icon for chat/settings." -ForegroundColor Green
}
