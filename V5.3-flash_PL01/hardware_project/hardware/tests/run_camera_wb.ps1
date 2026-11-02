param([switch]$Red)
$ErrorActionPreference='Stop'
$candidateRoot=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$runName=if($Red){'wb_camera_red'}else{'wb_camera_green'}
$runRoot=Join-Path $candidateRoot "reports/$runName"
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'wb_test_init.mem'),(Join-Path $PSScriptRoot 'wb_test_af.mem') -Destination $runRoot
$vivadoBin='D:/Tools/FPGA/Vivado/2023.1/bin'
Push-Location $runRoot
try {
  $compileArgs=@('--sv',(Join-Path $PSScriptRoot 'tb_camera_wb_integration.sv'))
  foreach($name in @('camera_config_v02.v','sensor_wb_control.v','ov5640_af.v','sccb_write.v')) {
    $compileArgs+=(Join-Path $candidateRoot "hardware/rtl/$name")
  }
  if($Red){$compileArgs+=@('-d','WB_ARBITRATION_RED')}
  & "$vivadoBin/xvlog.bat" @compileArgs 2>&1 | Tee-Object 'compile.log'
  if($LASTEXITCODE -ne 0){throw 'xvlog failed'}
  & "$vivadoBin/xelab.bat" tb_camera_wb_integration -s wb_camera_sim 2>&1 | Tee-Object 'elaborate.log'
  if($LASTEXITCODE -ne 0){throw 'xelab failed'}
  & "$vivadoBin/xsim.bat" wb_camera_sim -runall 2>&1 | Tee-Object 'simulation.log'
  if(-not(Select-String -LiteralPath 'simulation.log' -SimpleMatch 'PASS tb_camera_wb_integration' -Quiet)) {
    throw 'behavioral simulation did not pass (see simulation.log)'
  }
}finally{Pop-Location}
