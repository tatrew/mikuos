$ErrorActionPreference = "Stop"

$project = Join-Path $HOME "miku"
$adapter = Join-Path $project "miku_gemma4_E4B_LoRA_v4"
$output = Join-Path $project "GGUF_Q4"

Write-Host "=== Miku GGUF Export ===" -ForegroundColor Cyan

$weights = Join-Path $adapter "adapter_model.safetensors"
$configPath = Join-Path $adapter "adapter_config.json"

if (-not (Test-Path $weights)) {
    throw "LoRA weights not found: $weights"
}

if (-not (Test-Path $configPath)) {
    throw "LoRA config not found: $configPath"
}

$config = Get-Content $configPath -Raw | ConvertFrom-Json
$base = $config.base_model_name_or_path

Write-Host "Base model: $base"

if ($base -notmatch "gemma-4-E4B-it") {
    throw "Unexpected base model: $base"
}

if (-not (Get-Command unsloth -ErrorAction SilentlyContinue)) {
    throw "Unsloth CLI not found. Install Unsloth Studio first."
}

New-Item -ItemType Directory -Path $output -Force | Out-Null

Write-Host "Exporting Miku to Q4_K_M GGUF..." -ForegroundColor Green

& unsloth export $adapter $output --format gguf --quantization q4_k_m --max-seq-length 2048 --load-in-4bit

if ($LASTEXITCODE -ne 0) {
    throw "Unsloth export failed with exit code $LASTEXITCODE"
}

$files = @(Get-ChildItem $output -Recurse |
    Where-Object { $_.Extension -eq ".gguf" })

if ($files.Count -eq 0) {
    throw "No GGUF file was created."
}

Write-Host "EXPORT COMPLETED!" -ForegroundColor Green
$files | Select-Object FullName, Length
