"""Lightweight memory admission checks for one-browser-at-a-time execution."""
from pathlib import Path

MIB = 1024 * 1024


def memory_status():
    available = headroom = None
    try:
        values = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()}
        available = values.get('MemAvailable')
    except (OSError, ValueError, IndexError):
        pass
    try:
        limit = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if limit != 'max':
            current = int(Path('/sys/fs/cgroup/memory.current').read_text())
            # Linux can reclaim inactive file cache without terminating a
            # process; counting it as busy could leave an idle queue stuck.
            try:
                stats = dict(line.split() for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines())
                reclaimable = min(current, int(stats.get('inactive_file', 0)))
            except (OSError, ValueError):
                reclaimable = 0
            headroom = int(limit) - current + reclaimable
    except (OSError, ValueError):
        pass
    ready = (available is None or available >= 256 * MIB) and (headroom is None or headroom >= 192 * MIB)
    return {'ready': ready, 'host_available_mb': round(available / MIB) if available is not None else None,
            'container_headroom_mb': round(headroom / MIB) if headroom is not None else None}
