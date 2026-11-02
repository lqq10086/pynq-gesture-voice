"""Retain DMA ownership until every hardware reader/writer is confirmed idle."""


def shutdown_once(camera, executor, display):
    display_status=None
    display_error=None
    if display is not None:
        try:
            display_status=display.stop()
        except Exception as error:
            display_error=error
    camera.halt_video()
    if executor is not None:
        executor.wait_idle()
    elif camera.overlay is not None:
        from hardware_idle import read_idle_kernel
        read_idle_kernel(camera)
    if display_error is not None:
        # Both DMA engines are now safe, but allocations stay owned until the
        # sole MMIO writer acknowledges exit and has actually joined.
        display_status=display.stop_after_video_halt()
        display_status['initial_stop_error']=repr(display_error)
    camera.stop_video()
    if executor is not None:
        executor.close()
    return display_status


def shutdown_retaining(camera, executor, display, result, write_status, wake,
                       retry_s=5.0):
    final_status=result['status']
    attempts=0
    while True:
        attempts+=1
        try:
            display_status=shutdown_once(camera,executor,display)
        except Exception as error:
            result.update(status='RETAINED_OWNER_RETRY',retained_ownership=True,
                          shutdown_attempts=attempts,shutdown_error=repr(error))
            print('Shutdown incomplete; retaining buffers and retrying: '+repr(error),flush=True)
            # Even an unwritable diagnostics file must not discard DMA ownership.
            try: write_status(result)
            except Exception as status_error: print(repr(status_error),flush=True)
            wake.wait(retry_s)
            wake.clear()
            continue
        result.update(status=final_status,retained_ownership=False,
                      shutdown_attempts=attempts,shutdown_verified=True,
                      display_shutdown=display_status)
        write_status(result)
        return
