# 在项目目录中启动已经安装和构建的本地工作台。
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
$frontendIndex = Join-Path $projectRoot 'frontend/dist/index.html'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python environment missing. Follow docs/development.md installation steps first.'
}
if (-not (Test-Path -LiteralPath $frontendIndex)) {
    throw 'Frontend build missing. Run npm.cmd run build in frontend first.'
}
Push-Location -LiteralPath $projectRoot
try {
    # 尊重配置的服务地址；本机服务随工作台启动，远程服务由部署环境管理。
    $qdrantUrl = [string](& $pythonPath -c 'from stock_research.config import Settings; print(Settings().qdrant_url)')
    $qdrantUrl = $qdrantUrl.Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Failed to read Qdrant settings.' }
    if ([string]::IsNullOrWhiteSpace($qdrantUrl)) { $qdrantUrl = 'http://127.0.0.1:6333' }
    $qdrantAddress = [Uri]$qdrantUrl
    if ($qdrantAddress.IsLoopback -and $qdrantAddress.Port -eq 6333 -and $qdrantAddress.Scheme -eq 'http') {
        & $pythonPath -m stock_research.qdrant_service ensure
        if ($LASTEXITCODE -ne 0) { throw 'Qdrant startup failed. Check the daily Qdrant log.' }
        & $pythonPath -m stock_research.qdrant_migration
        if ($LASTEXITCODE -ne 0) { throw 'Vector migration failed. Stop the backend and retry.' }
    }
    $env:STOCK_QDRANT_URL = $qdrantUrl
    Write-Host "Qdrant dashboard: $qdrantUrl/dashboard"
    Write-Host 'Research workspace: http://127.0.0.1:8765'
    & $pythonPath -m stock_research
    if ($LASTEXITCODE -ne 0) { throw "Backend exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
