# 一键起 API + 独立 worker(配置统一来自仓库根 .env,不需要在终端里 $env: 设置)
# 用法:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev_up.ps1 [-Port 8100]
param([int]$Port = 8100)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path "$root\.env")) {
    Write-Host "缺少 .env:请先 Copy-Item .env.example .env 并检查 TEST_DATABASE_URL 与 provider 配置" -ForegroundColor Red
    exit 1
}
New-Item -ItemType Directory -Force -Path "$root\.run" | Out-Null

# 子进程 stdout 重定向到文件时,Python 默认用本地代码页(cp936)编码,
# 中文横幅会变成 GBK 字节,交接方用 UTF-8 工具查看就是乱码、无法核对。
$env:PYTHONUTF8 = "1"

# 停掉本项目此前的 API / worker(按命令行匹配,不误伤其他 python 进程)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'app\.worker' -or $_.CommandLine -match 'uvicorn app\.main'
} | ForEach-Object {
    Write-Host "停止旧进程 PID $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2

$py = "$root\.venv\Scripts\python.exe"
$api = Start-Process -FilePath $py -ArgumentList "-m","uvicorn","app.main:app",
        "--host","127.0.0.1","--port","$Port" -WindowStyle Hidden -PassThru `
        -RedirectStandardError "$root\.run\api.err" -RedirectStandardOutput "$root\.run\api.out"
$worker = Start-Process -FilePath $py -ArgumentList "-m","app.worker" -WindowStyle Hidden -PassThru `
        -RedirectStandardError "$root\.run\worker.err" -RedirectStandardOutput "$root\.run\worker.out"

Write-Host "API pid=$($api.Id)  worker pid=$($worker.Id)  → http://127.0.0.1:$Port/"

# 等就绪
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 1
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:$Port/health/ready" -TimeoutSec 3
        if ($h.status -eq "ok") { break }
    } catch { }
}
Write-Host "readiness: $($h.status) / database: $($h.database)"

# 核对实际生效配置(不输出任何密钥):worker 启动横幅 + 登录后的 /meta
Start-Sleep -Seconds 2
$banner = Select-String -Path "$root\.run\worker.out" -Pattern "生效模型" | Select-Object -Last 1
if ($banner) { Write-Host "worker 侧: $($banner.Line)" }
try {
    $login = Invoke-RestMethod "http://127.0.0.1:$Port/auth/login" -Method Post `
        -ContentType "application/json" `
        -Body (@{ username = "demo_admin_a"; password = "demo-pass-123" } | ConvertTo-Json)
    $meta = Invoke-RestMethod "http://127.0.0.1:$Port/meta" `
        -Headers @{ Authorization = "Bearer $($login.access_token)" }
    $p = $meta.providers
    Write-Host ("API  侧: asr={0}(mock={1}) llm={2}(mock={3}) embed={4}(mock={5}) 阈值={6}" -f `
        $p.asr.model, $p.asr.is_mock, $p.llm.model, $p.llm.is_mock,
        $p.embed.model, $p.embed.is_mock, $meta.retrieval.evidence_threshold)
} catch {
    Write-Host "未能登录核对 /meta(演示账号不存在时可手动执行 GET /meta)" -ForegroundColor Yellow
}
