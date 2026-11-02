##############################################################################
# 用法：Windows 侧统一入口，提供 Verify、Check、Flash、Jtag、Status、Build、Open 七个动作。
# 命令：pwsh delivery.ps1 -Action Status
# 来源：AX7020 工程 scripts/delivery.ps1，原样复制。
# 版本依赖：运行时通过 XILINX_VIVADO 环境变量与四个安装根探测 vivado、xsct、bootgen、program_flash，不写死单个版本路径。
# 使用前必改：工程名与产物路径；动作里引用的 tcl 文件名；烧写参数。
# 通用性：环境探测与动作分发的写法可跨版本复用；具体动作绑定了该工程的文件名。
# 未验证：未在本机重跑，本机未安装 Vivado。
##############################################################################
[CmdletBinding()]
param(
    [ValidateSet('Verify','Check','Flash','Jtag','Status','Build','Open')]
    [string]$Action = 'Check',
    [switch]$UseRebuilt
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root 'logs'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Find-XilinxTool([string]$Name) {
    # 统一交给 tools/eda_env.py 探测：它扫盘、读环境变量、按版本降序，
    # 避免像旧版那样只认 2023.1 的四条路径、换机器就 throw。
    $envScript = Join-Path $PSScriptRoot '..\tools\eda_env.py'
    if (Test-Path $envScript) {
        try {
            $json = & python $envScript --json 2>$null | ConvertFrom-Json
            $map = @{ 'vivado' = 'vivado'; 'vitis' = 'vitis'; 'xsct' = 'xsct'; 'vitis_hls' = 'vitis_hls' }
            $key = $map[$Name]
            if ($key -and $json.tools.$key.path) {
                return $json.tools.$key.path
            }
        } catch {
            Write-Verbose "eda_env.py 探测失败，退回 PATH: $_"
        }
    }
    $command = Get-Command "$Name.bat" -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw "MISSING_INPUT: $Name 未找到。先运行 python tools\eda_env.py 看它扫到了什么；装好 Xilinx 工具或把 bin 目录加入 PATH 后重试。"
}

function Assert-SafeRelativePath([string]$Relative) {
    if ([IO.Path]::IsPathRooted($Relative)) { throw "Absolute manifest path: $Relative" }
    $full = [IO.Path]::GetFullPath((Join-Path $root $Relative))
    if (-not $full.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path outside package: $Relative"
    }
    return $full
}

function Test-PackageIntegrity([switch]$FirmwareOnly) {
    $manifest = Join-Path $root 'SHA256SUMS.txt'
    if (-not (Test-Path -LiteralPath $manifest)) { throw 'SHA256SUMS.txt missing. Extract the complete delivery ZIP.' }
    $seen = @{}
    $count = 0
    foreach ($line in Get-Content -LiteralPath $manifest -Encoding UTF8) {
        if ($line -notmatch '^([0-9a-fA-F]{64})  (.+)$') { throw "Invalid checksum line: $line" }
        $expected = $Matches[1]
        $relative = $Matches[2].Replace('\','/')
        if ($seen.ContainsKey($relative)) { throw "Duplicate checksum entry: $relative" }
        $seen[$relative] = $true
        $full = Assert-SafeRelativePath $relative
        if ($FirmwareOnly -and -not $relative.StartsWith('firmware/')) { continue }
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "Missing file: $relative" }
        if ((Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash -ne $expected) {
            throw "CHECKSUM FAILED: $relative. Do not flash; re-extract the original ZIP."
        }
        $count++
    }
    foreach ($required in @('firmware/BOOT.BIN','firmware/fsbl.elf','firmware/system.bit',
            'firmware/camera_hdmi.elf','firmware/ps7_init.tcl','firmware/system.xsa','firmware/boot.bif')) {
        if (-not $seen.ContainsKey($required)) { throw "Required checksum missing: $required" }
    }
    $bootSize = (Get-Item -LiteralPath (Join-Path $root 'firmware/BOOT.BIN')).Length
    if ($bootSize -le 0 -or $bootSize -gt 33554432) { throw 'BOOT.BIN does not fit the supported 32 MiB QSPI.' }
    Write-Host "PACKAGE_INTEGRITY_OK files=$count"
}

function Invoke-LoggedTool([string]$Tool, [string[]]$ToolArgs, [string]$Label, [string]$Marker = '') {
    # Keep wrapper stdout distinct from Vivado's own -log file (Windows lock).
    $log = Join-Path $logs "${stamp}_${Label}_console.log"
    Write-Host "Tool: $Tool"
    Write-Host "Log:  $log"
    # Some Xilinx tools write normal diagnostics to stderr in Windows PS 5.1.
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Tool @ToolArgs 2>&1 | Tee-Object -FilePath $log | ForEach-Object { Write-Host $_ }
        $result = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    if ($result -ne 0) { throw "$Label failed (exit $result). See $log" }
    if ($Marker -and -not (Select-String -LiteralPath $log -SimpleMatch $Marker -Quiet)) {
        throw "$Label did not report $Marker. See $log"
    }
}

function Test-ServerPort {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $attempt = $client.BeginConnect('127.0.0.1',3121,$null,$null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($attempt)
        return $true
    } catch { return $false } finally { $client.Close() }
}

function Start-HardwareServer {
    if (Test-ServerPort) { return }
    $server = Find-XilinxTool 'hw_server'
    Start-Process -FilePath $server -ArgumentList '-s tcp::3121' -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "${stamp}_hw_server.log") `
        -RedirectStandardError (Join-Path $logs "${stamp}_hw_server.err.log") | Out-Null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (Test-ServerPort) { return }
        Start-Sleep -Milliseconds 500
    }
    throw 'hw_server did not start on port 3121. Close conflicting hardware tools and check logs.'
}

function Test-Board {
    Start-HardwareServer
    Invoke-LoggedTool (Find-XilinxTool 'xsct') `
        @((Join-Path $PSScriptRoot 'delivery_board.tcl'), 'probe') 'board_probe' 'BOARD_PROBE_OK'
}

function Get-FirmwareDirectory {
    if ($UseRebuilt) { return (Join-Path $root 'build/rebuilt_firmware') }
    return (Join-Path $root 'firmware')
}

function Test-SelectedFirmware {
    if (-not $UseRebuilt) { Test-PackageIntegrity -FirmwareOnly; return }
    $dir = Get-FirmwareDirectory
    $sums = Join-Path $dir 'SHA256SUMS.txt'
    if (-not (Test-Path -LiteralPath $sums)) { throw 'Rebuild first with 04_REBUILD_ALL.bat.' }
    $seen = @{}
    foreach ($line in Get-Content -LiteralPath $sums -Encoding UTF8) {
        if ($line -notmatch '^([0-9a-fA-F]{64})  ([A-Za-z0-9_.-]+)$') { throw "Invalid rebuilt checksum line: $line" }
        $hash = $Matches[1]; $name = $Matches[2]
        if ($seen.ContainsKey($name)) { throw "Duplicate rebuilt file: $name" }
        $seen[$name] = $true
        if ((Get-FileHash -LiteralPath (Join-Path $dir $name) -Algorithm SHA256).Hash -ne $hash) {
            throw "Rebuilt checksum failed: $name"
        }
    }
    foreach ($name in @('BOOT.BIN','fsbl.elf','system.bit','camera_hdmi.elf','ps7_init.tcl','system.xsa','boot.bif')) {
        if (-not $seen.ContainsKey($name)) { throw "Rebuilt file absent from manifest: $name" }
    }
    $size = (Get-Item -LiteralPath (Join-Path $dir 'BOOT.BIN')).Length
    if ($size -le 0 -or $size -gt 33554432) { throw 'Rebuilt image does not fit 32 MiB QSPI.' }
    Write-Host "REBUILT_FIRMWARE_INTEGRITY_OK: $dir"
}

try {
    Push-Location $root
    Write-Host "AX7020 OV5640 | V1.5 PL Run05 color | Action: $Action"
    $firmware = Get-FirmwareDirectory
    switch ($Action) {
        'Verify' { Test-PackageIntegrity }
        'Check' {
            Test-PackageIntegrity
            foreach ($name in @('vivado','xsct','bootgen','program_flash','hw_server')) {
                Write-Host "$name = $(Find-XilinxTool $name)"
            }
            Write-Host 'ENVIRONMENT_CHECK_OK (offline)'
        }
        'Jtag' {
            Test-SelectedFirmware
            Test-Board
            Write-Host "Loading volatile RAM/PL from $firmware"
            Invoke-LoggedTool (Find-XilinxTool 'xsct') `
                @((Join-Path $PSScriptRoot 'delivery_board.tcl'),'jtag',$firmware) 'jtag_run' 'V1_5_RUNTIME_CHECK_OK'
        }
        'Flash' {
            Test-SelectedFirmware
            $flashTool = Find-XilinxTool 'program_flash'
            Test-Board
            Write-Host "Firmware: $firmware"
            Write-Host 'This action replaces the connected board QSPI boot image.' -ForegroundColor Yellow
            Write-Host 'Hardware: AX7020 / XC7Z020-2CLG400 / OV5640 on J11 / 32 MiB QSPI.'
            Write-Host 'Connect only one board. Select QSPI boot according to the board markings/manual.'
            if ((Read-Host 'Type PROGRAM to write and verify QSPI') -cne 'PROGRAM') { throw 'Cancelled; QSPI was not written.' }
            Invoke-LoggedTool $flashTool @('-f',(Join-Path $firmware 'BOOT.BIN'),
                '-offset','0','-flash_type','qspi-x4-single','-fsbl',(Join-Path $firmware 'fsbl.elf'),
                '-blank_check','-verify','-url','tcp:127.0.0.1:3121') 'qspi_program' 'Flash Operation Successful'
            Write-Host 'QSPI_PROGRAM_AND_VERIFY_OK'
            try {
                Invoke-LoggedTool (Find-XilinxTool 'xsct') `
                    @((Join-Path $PSScriptRoot 'delivery_board.tcl'),'reboot') 'qspi_reboot' 'V1_5_RUNTIME_CHECK_OK'
            } catch {
                Write-Warning 'Flash verified; automatic boot/runtime check did not complete.'
                Write-Warning $_
                Write-Warning 'Power OFF, select QSPI boot, wait 5 seconds, power ON, then run 03_CHECK_RUNNING.bat.'
                exit 2
            }
            Write-Host 'QSPI_PROGRAM_REBOOT_AND_RUN_OK'
            Write-Host 'Confirm independent boot: power OFF for 5 seconds, power ON, and inspect live HDMI.'
        }
        'Status' {
            Test-Board
            Invoke-LoggedTool (Find-XilinxTool 'xsct') `
                @((Join-Path $PSScriptRoot 'delivery_board.tcl'),'status') 'runtime_health' 'V1_5_RUNTIME_CHECK_OK'
        }
        'Build' {
            $vivado = Find-XilinxTool 'vivado'
            $xsct = Find-XilinxTool 'xsct'
            $bootgen = Find-XilinxTool 'bootgen'
            if ($root -match '[^\x00-\x7F]|\s') { throw 'Extract to a short ASCII path without spaces, e.g. D:\AX7020_V15.' }
            $generated = [IO.Path]::GetFullPath((Join-Path $root 'hardware/vivado_project'))
            $expectedParent = [IO.Path]::GetFullPath((Join-Path $root 'hardware'))
            if ((Split-Path $generated) -ne $expectedParent) { throw 'Unexpected generated project path.' }
            if (Test-Path -LiteralPath $generated) {
                $previous = "${generated}_previous_$stamp"
                if (Test-Path -LiteralPath $previous) { throw "Backup already exists: $previous" }
                Move-Item -LiteralPath $generated -Destination $previous
                Write-Host "Previous project preserved: $previous"
            }
            Invoke-LoggedTool $vivado @('-mode','batch','-source',
                (Join-Path $PSScriptRoot 'build_stage1_hardware.tcl'),'-log',
                (Join-Path $logs "${stamp}_hardware.log"),'-journal',
                (Join-Path $logs "${stamp}_hardware.jou")) 'hardware' 'XSA='
            Invoke-LoggedTool $xsct @((Join-Path $PSScriptRoot 'build_software.tcl')) 'software' 'V1_5_NO_LENS_SYMBOLS_AND_WFI_VERIFIED'
            $rebuilt = Join-Path $root 'build/rebuilt_firmware'
            New-Item -ItemType Directory -Force -Path $rebuilt | Out-Null
            foreach ($file in @('build/bitstream/system.bit','build/elf/fsbl.elf','build/elf/camera_hdmi.elf',
                    'build/xsa_or_hdf/system.xsa','software/vitis_workspace/platform_ax7020/hw/ps7_init.tcl')) {
                Copy-Item -LiteralPath (Join-Path $root $file) -Destination $rebuilt -Force
            }
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'production_boot.bif') -Destination (Join-Path $rebuilt 'boot.bif') -Force
            Push-Location $rebuilt
            try {
                Invoke-LoggedTool $bootgen @('-arch','zynq','-image','boot.bif','-o','BOOT.BIN','-w','on') 'bootgen' 'Bootimage generated successfully'
            } finally { Pop-Location }
            if (-not (Test-Path -LiteralPath (Join-Path $rebuilt 'BOOT.BIN'))) { throw 'Rebuilt BOOT.BIN missing.' }
            Get-ChildItem -LiteralPath $rebuilt -File | Where-Object Name -ne 'SHA256SUMS.txt' | Sort-Object Name | ForEach-Object {
                '{0}  {1}' -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash, $_.Name
            } | Set-Content -LiteralPath (Join-Path $rebuilt 'SHA256SUMS.txt') -Encoding ASCII
            $UseRebuilt = $true
            Test-SelectedFirmware
            Write-Host "REBUILD_COMPLETE: $rebuilt"
            Write-Host 'Use 06_JTAG_REBUILT.bat or 07_FLASH_REBUILT.bat for these new artifacts.'
        }
        'Open' {
            $vivado = Find-XilinxTool 'vivado'
            if ($root -match '[^\x00-\x7F]|\s') { throw 'Extract to a short ASCII path without spaces before opening Vivado.' }
            $project = Join-Path $root 'hardware/vivado_project/ax7020_ov5640_ddr_vdma.xpr'
            if (Test-Path -LiteralPath $project) { & $vivado -mode gui $project }
            else { & $vivado -mode gui -source (Join-Path $PSScriptRoot 'create_hardware_project.tcl') }
            if ($LASTEXITCODE -ne 0) { throw 'Vivado GUI exited with an error.' }
        }
    }
    Write-Host 'ACTION_COMPLETED' -ForegroundColor Green
} catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
    exit 1
} finally { Pop-Location }
