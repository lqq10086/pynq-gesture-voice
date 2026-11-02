# Vivado injects bundled Python variables that conflict with the host verifier's venv.
unset -nocomplain ::env(PYTHONHOME) ::env(PYTHONPATH)
source [file join [file dirname [info script]] publish_hardware_flash_pl01.tcl]
