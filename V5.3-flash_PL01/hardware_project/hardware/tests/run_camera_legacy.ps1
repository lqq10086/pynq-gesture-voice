$ErrorActionPreference='Stop'
$candidateRoot=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$runRoot=Join-Path $candidateRoot 'reports/wb_legacy_regression'
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $candidateRoot 'hardware/rtl/ov5640_720p_rgb565.mem'),(Join-Path $candidateRoot 'hardware/rtl/af_firmware.mem') -Destination $runRoot
$vivadoBin='D:/Tools/FPGA/Vivado/2023.1/bin'
Push-Location $runRoot
try {
  $compileArgs=@('--sv',(Join-Path $PSScriptRoot 'tb_camera_config_legacy_regression.sv'))
  foreach($name in @('camera_config_v02.v','ov5640_af.v','sccb_write.v')) {
    $compileArgs+=(Join-Path $candidateRoot "hardware/rtl/$name")
  }
  & "$vivadoBin/xvlog.bat" @compileArgs 2>&1 | Tee-Object 'compile.log'
  if($LASTEXITCODE -ne 0){throw 'xvlog failed'}
  & "$vivadoBin/xelab.bat" tb_camera_config_legacy_regression -s wb_legacy_sim 2>&1 | Tee-Object 'elaborate.log'
  if($LASTEXITCODE -ne 0){throw 'xelab failed'}
  & "$vivadoBin/xsim.bat" wb_legacy_sim -runall 2>&1 | Tee-Object 'simulation.log'
  if(-not(Select-String -LiteralPath 'simulation.log' -SimpleMatch 'PASS camera_config_v02 all sensor-control cases' -Quiet)) {
    throw 'legacy regression did not pass (see simulation.log)'
  }
}finally{Pop-Location}
