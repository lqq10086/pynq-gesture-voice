param([ValidateSet('baseline','final')][string]$Mode='final',[switch]$AbortBaseline)
$ErrorActionPreference='Stop'
$candidateRoot=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$vivadoBin='D:\Tools\FPGA\Vivado\2023.1\bin'
$xpmSource='D:\Tools\FPGA\Vivado\2023.1\data\ip\xpm\xpm_cdc\hdl\xpm_cdc.sv'
$runPath=Join-Path $candidateRoot "reports\wb_axi_$Mode"
if($AbortBaseline) { $runPath+='_abort_baseline' }
New-Item -ItemType Directory -Path $runPath -Force | Out-Null
$rtlPath=Join-Path $candidateRoot 'hardware\rtl'
$sources=@($xpmSource,'D:\Tools\FPGA\Vivado\2023.1\data\verilog\src\glbl.v')
foreach($name in @('video_axis_monitor.v','s2mm_write_observer.v','stats_snapshot_cdc.v','video_stats_snapshot.v','camera_control_axi.v')) { $sources+=Join-Path $rtlPath $name }
$sources+=Join-Path $PSScriptRoot 'tb_camera_control_wb.sv'
$options=@('-sv')
if($Mode -eq 'final') {
 $options+=@('-d','WB_IMPLEMENTED')
 if(!$AbortBaseline) { $options+=@('-d','WB_ABORT_IMPLEMENTED') }
 $sources+=Join-Path $rtlPath 'sensor_wb_bridge.v'
 $sources+=Join-Path $PSScriptRoot 'tb_sensor_wb_bridge.sv'
 $sources+=Join-Path $PSScriptRoot 'tb_wb_transport_abort.sv'
}
Push-Location $runPath
try {
 & "$vivadoBin\xvlog.bat" @options @sources *> compile.log
 if($LASTEXITCODE -ne 0) { Get-Content compile.log -Tail 30;throw 'xvlog failed' }
 $tops=@('tb_camera_control_wb')
 if($Mode -eq 'final') { $tops+=@('tb_sensor_wb_bridge','tb_wb_transport_abort','tb_wb_transport_abort_after_accept') }
 foreach($top in $tops) {
  & "$vivadoBin\xelab.bat" $top glbl -s $top *> "$top.elaborate.log"
  if($LASTEXITCODE -ne 0) { Get-Content "$top.elaborate.log" -Tail 30;throw "xelab $top failed" }
  & "$vivadoBin\xsim.bat" $top -runall *> "$top.run.log"
  Get-Content "$top.run.log" -Tail 14
  if($LASTEXITCODE -ne 0 -or !(Select-String -LiteralPath "$top.run.log" -Pattern 'PASS_' -Quiet)) { throw "Simulation $top did not pass ($Mode)" }
 }
} finally { Pop-Location }
