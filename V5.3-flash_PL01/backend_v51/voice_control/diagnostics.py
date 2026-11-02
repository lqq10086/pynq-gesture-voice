"""Read-only Linux board metrics; no additional runtime dependency."""
from pathlib import Path
import platform


def memory_snapshot():
    result = {}
    for filename, wanted, prefix in (
        ('/proc/self/status', ('VmRSS', 'VmHWM', 'VmSize', 'Threads'), 'process_'),
        ('/proc/meminfo', ('MemAvailable', 'SwapTotal', 'SwapFree'), 'system_')):
        try:
            for line in Path(filename).read_text(encoding='utf-8').splitlines():
                name, _, value = line.partition(':')
                if name in wanted:
                    parts = value.split()
                    result[prefix+name+('_bytes' if name != 'Threads' else '')] = int(parts[0]) * (1024 if name != 'Threads' else 1)
        except OSError:
            pass
    return result or {'available': False, 'note': 'Linux /proc only; use component benchmark for Windows memory'}


def host_info():
    result = dict(system=platform.system(), machine=platform.machine(),
                  python=platform.python_version(), processor=platform.processor())
    for name in ('scaling_cur_freq', 'cpuinfo_max_freq'):
        try:
            result[name+'_khz'] = int(Path('/sys/devices/system/cpu/cpu0/cpufreq', name).read_text().strip())
        except (OSError, ValueError):
            pass
    return result
