param(
    [string]$Action = 'all',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# 启动器的实际逻辑写在 PowerShell 里：
#   - PowerShell 原生支持 UTF-8 中文，不受 cmd.exe 解析 GBK/UTF-8 的影响；
#   - 启动窗口、等待端口、打开浏览器都比纯 bat 更可靠。
# dev.bat 只是双击入口，负责设置 UTF-8 代码页并调用这个脚本。

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$BackendUrl = 'http://127.0.0.1:18080'
$FrontendUrl = 'http://127.0.0.1:5173'

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "    $Text" -ForegroundColor Green
}

function Write-Warn([string]$Text) {
    Write-Host "    $Text" -ForegroundColor Yellow
}

function Write-Err([string]$Text) {
    Write-Host "    $Text" -ForegroundColor Red
}

function Test-Tool([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Require-Tool([string]$Name, [string]$InstallHint) {
    if (Test-Tool $Name) { return $true }
    Write-Err "没有找到 $Name。"
    Write-Host "    安装方法：$InstallHint"
    return $false
}

function Get-ToolVersion([string]$Name) {
    if (-not (Test-Tool $Name)) { return '未安装' }
    try {
        $line = & $Name --version 2>$null | Select-Object -First 1
        if ($line) { return [string]$line }
    } catch { }
    return '已安装'
}

function Ensure-EnvFile {
    $envFile = Join-Path $BackendDir '.env'
    $exampleFile = Join-Path $BackendDir '.env.example'
    if (Test-Path $envFile) { return }
    if (-not (Test-Path $exampleFile)) {
        Write-Warn 'backend\.env 和 .env.example 都不存在，后端可能无法启动。'
        return
    }
    Copy-Item $exampleFile $envFile
    Write-Warn 'backend\.env 不存在，已从 .env.example 复制一份。'
    Write-Warn '请填写 DEEPSEEK_API_KEY 和 WP_SITE_URL；没有 Key 时会进入 mock 模式。'
}

function Get-PortOwner([int]$Port) {
    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($connection) { return [int]$connection.OwningProcess }
    } catch { }
    return $null
}

function Start-Backend {
    $owner = Get-PortOwner 18080
    if ($owner) {
        Write-Step "端口 18080 已被 PID $owner 占用，跳过后端启动。"
        return
    }

    Write-Step '启动后端 FastAPI（新窗口）...'
    $command = 'uv run uvicorn app.main:app --host 127.0.0.1 --port 18080 --reload'
    Start-Process -FilePath 'cmd.exe' -WorkingDirectory $BackendDir `
        -ArgumentList @('/k', "`"$command`"") | Out-Null
}

function Start-Frontend {
    $owner = Get-PortOwner 5173
    if ($owner) {
        Write-Step "端口 5173 已被 PID $owner 占用，跳过前端启动。"
        return
    }

    Write-Step '启动前端 Vite（新窗口）...'
    $command = 'pnpm dev --host 127.0.0.1 --strictPort'
    Start-Process -FilePath 'cmd.exe' -WorkingDirectory $FrontendDir `
        -ArgumentList @('/k', "`"$command`"") | Out-Null
}

function Wait-Http([string]$Url, [string]$Label, [int]$Seconds = 30) {
    Write-Host "    等待 $Label"
    for ($i = 0; $i -lt $Seconds; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                Write-Ok "$Label 已就绪：$Url"
                return $true
            }
        } catch { }
        Start-Sleep -Seconds 1
    }
    Write-Warn "$Label 在 $Seconds 秒内没有响应，请查看对应窗口的日志。"
    return $false
}

function Invoke-StartAll {
    if (-not (Require-Tool 'uv' 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"')) { return }
    if (-not (Require-Tool 'pnpm' '先安装 Node 20+，再运行 npm install -g pnpm')) { return }
    Ensure-EnvFile

    Start-Backend
    [void](Wait-Http "$BackendUrl/api/health" '后端')
    Start-Frontend
    [void](Wait-Http "$FrontendUrl/" '前端')

    Write-Step "打开浏览器：$FrontendUrl/"
    Start-Process $FrontendUrl | Out-Null

    Write-Host ''
    Write-Host '启动完成。两个服务分别在新的命令行窗口中运行；' -ForegroundColor Green
    Write-Host '关闭那两个窗口即可停止服务。' -ForegroundColor Green
}

function Invoke-StartBackend {
    if (-not (Require-Tool 'uv' 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"')) { return }
    Ensure-EnvFile
    Start-Backend
    [void](Wait-Http "$BackendUrl/api/health" '后端')
}

function Invoke-StartFrontend {
    if (-not (Require-Tool 'pnpm' '先安装 Node 20+，再运行 npm install -g pnpm')) { return }
    Start-Frontend
    [void](Wait-Http "$FrontendUrl/" '前端')
    Write-Step "打开浏览器：$FrontendUrl/"
    Start-Process $FrontendUrl | Out-Null
}

function Invoke-SyncKb {
    if (-not (Require-Tool 'uv' 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"')) { return }
    Ensure-EnvFile
    Write-Step '同步站点知识库（首次或图片较多时会比较慢）...'
    $arguments = @('run', 'python', '-m', 'app.kb.sync')
    if ($Rest.Count -gt 0) { $arguments += $Rest }

    Push-Location $BackendDir
    try {
        & uv @arguments
        if ($LASTEXITCODE -ne 0) {
            Write-Err "知识库同步失败，退出码 $LASTEXITCODE。"
        } else {
            Write-Ok '知识库已更新。'
        }
    } finally {
        Pop-Location
    }
}

function Invoke-BuildPlugin {
    if (-not (Require-Tool 'pnpm' '先安装 Node 20+，再运行 npm install -g pnpm')) { return }
    Write-Step '构建前端和 WordPress 插件资源...'
    Push-Location $FrontendDir
    try {
        & pnpm build
        if ($LASTEXITCODE -ne 0) {
            Write-Err "构建失败，退出码 $LASTEXITCODE。"
        } else {
            Write-Ok '产物在 wordpress-plugin\wp-live2d-ai\assets\app\'
        }
    } finally {
        Pop-Location
    }
}

function Invoke-InstallDeps {
    $uvOk = Require-Tool 'uv' 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    $pnpmOk = Require-Tool 'pnpm' '先安装 Node 20+，再运行 npm install -g pnpm'
    if (-not ($uvOk -and $pnpmOk)) { return }
    Ensure-EnvFile

    Write-Step '安装后端依赖...'
    Push-Location $BackendDir
    try { & uv sync } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Write-Err "后端依赖安装失败，退出码 $LASTEXITCODE。"
        return
    }

    Write-Step '安装前端依赖...'
    Push-Location $FrontendDir
    try { & pnpm install } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Write-Err "前端依赖安装失败，退出码 $LASTEXITCODE。"
        return
    }
    Write-Ok '依赖已经装好。'
}

function Invoke-Doctor {
    Write-Step '本地环境检查'

    Write-Host "    uv                      $(Get-ToolVersion 'uv')"
    Write-Host "    pnpm                    $(Get-ToolVersion 'pnpm')"
    Write-Host "    node                    $(Get-ToolVersion 'node')"
    Write-Host "    git                     $(Get-ToolVersion 'git')"
    Write-Host ''

    $envFile = Join-Path $BackendDir '.env'
    if (Test-Path $envFile) {
        Write-Host '    backend\.env            存在'
    } else {
        Write-Warn 'backend\.env            缺失（启动时会从 .env.example 复制）'
    }

    $dbFile = Join-Path $BackendDir 'data\kb.sqlite3'
    if (Test-Path $dbFile) {
        Write-Host '    知识库索引              已建立'
    } else {
        Write-Warn '知识库索引              尚未建立（菜单选 4 同步）'
    }

    $backendOwner = Get-PortOwner 18080
    if ($backendOwner) {
        Write-Host "    端口 18080               被占用，PID $backendOwner（后端可能已在运行）"
    } else {
        Write-Host '    端口 18080               空闲'
    }

    $frontendOwner = Get-PortOwner 5173
    if ($frontendOwner) {
        Write-Host "    端口 5173               被占用，PID $frontendOwner（前端可能已在运行）"
    } else {
        Write-Host '    端口 5173               空闲'
    }

    $buildFile = Join-Path $Root 'wordpress-plugin\wp-live2d-ai\assets\app\app.js'
    if (Test-Path $buildFile) {
        Write-Host '    插件构建产物            存在'
    } else {
        Write-Warn '插件构建产物            不存在（菜单选 5 构建）'
    }
}

function Show-Help {
    Write-Host ''
    Write-Host 'WordPress Live2D AI 助手 - 开发启动器' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  dev.bat              启动后端 + 前端并打开浏览器'
    Write-Host '  dev.bat menu         打开菜单'
    Write-Host '  dev.bat backend      只启动后端'
    Write-Host '  dev.bat frontend     只启动前端'
    Write-Host '  dev.bat sync         同步知识库（可加 --no-vision）'
    Write-Host '  dev.bat build        构建 WordPress 插件资源'
    Write-Host '  dev.bat install      安装 / 更新依赖'
    Write-Host '  dev.bat doctor       检查环境、端口和索引状态'
}

function Show-Menu {
    while ($true) {
        Clear-Host
        Write-Host ''
        Write-Host '  ============================================================' -ForegroundColor Cyan
        Write-Host '    WordPress Live2D AI 助手 - 本地开发' -ForegroundColor Cyan
        Write-Host '  ============================================================' -ForegroundColor Cyan
        Write-Host ''
        Write-Host '    [1] 启动后端 + 前端，并打开浏览器'
        Write-Host '    [2] 只启动后端（FastAPI，18080）'
        Write-Host '    [3] 只启动前端（Vite，5173）'
        Write-Host '    [4] 同步站点知识库'
        Write-Host '    [5] 构建 WordPress 插件资源'
        Write-Host '    [6] 检查本地环境'
        Write-Host '    [7] 安装 / 更新依赖'
        Write-Host '    [0] 退出'
        Write-Host ''

        $choice = (Read-Host '请输入选项').Trim()
        switch ($choice) {
            '1' { Invoke-StartAll; return }
            '2' { Invoke-StartBackend; return }
            '3' { Invoke-StartFrontend; return }
            '4' { Invoke-SyncKb; return }
            '5' { Invoke-BuildPlugin; return }
            '6' { Invoke-Doctor; return }
            '7' { Invoke-InstallDeps; return }
            '0' { return }
            default { }
        }
    }
}

switch ($Action.ToLowerInvariant()) {
    'all' { Invoke-StartAll }
    'backend' { Invoke-StartBackend }
    'frontend' { Invoke-StartFrontend }
    'sync' { Invoke-SyncKb }
    'build' { Invoke-BuildPlugin }
    'install' { Invoke-InstallDeps }
    'doctor' { Invoke-Doctor }
    'menu' { Show-Menu }
    'help' { Show-Help }
    default { Show-Help }
}

Write-Host ''
Read-Host '按 Enter 关闭此窗口（已启动的服务会在各自窗口中继续运行）' | Out-Null
