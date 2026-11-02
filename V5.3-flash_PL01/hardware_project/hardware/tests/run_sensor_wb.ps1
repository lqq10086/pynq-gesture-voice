param([switch]$Red)
$ErrorActionPreference = 'Stop'
$candidateRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$runName = if ($Red) { 'wb_control_red' } else { 'wb_control_green' }
$runRoot = Join-Path $candidateRoot "reports/$runName"
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
$vivadoBin = 'D:/Tools/FPGA/Vivado/2023.1/bin'
Push-Location $runRoot
try {
  $compileArgs = @('--sv', (Join-Path $PSScriptRoot 'tb_sensor_wb_control.sv'))
  if ($Red) { $compileArgs += @('-d','WB_RED_STUB') }
  else { $compileArgs += (Join-Path $candidateRoot 'hardware/rtl/sensor_wb_control.v') }
  & "$vivadoBin/xvlog.bat" @compileArgs 2>&1 | Tee-Object 'compile.log'
  if ($LASTEXITCODE -ne 0) { throw 'xvlog failed' }
  & "$vivadoBin/xelab.bat" tb_sensor_wb_control -s wb_control_sim 2>&1 | Tee-Object 'elaborate.log'
  if ($LASTEXITCODE -ne 0) { throw 'xelab failed' }
  & "$vivadoBin/xsim.bat" wb_control_sim -runall 2>&1 | Tee-Object 'simulation.log'
  $passed = Select-String -LiteralPath 'simulation.log' -SimpleMatch 'PASS tb_sensor_wb_control' -Quiet
  if (-not $passed) { throw 'behavioral simulation did not pass (see simulation.log)' }
} finally { Pop-Location }
