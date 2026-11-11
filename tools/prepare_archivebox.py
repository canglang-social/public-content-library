"""Optional, explicit compatibility fixes for an isolated ArchiveBox 0.9.77 venv."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys


def main():
    if sys.prefix == sys.base_prefix:
        raise SystemExit('Run with the isolated ArchiveBox virtual environment Python.')
    distribution = importlib.metadata.distribution('archivebox')
    if distribution.version != '0.9.77':
        raise SystemExit('Only the tested ArchiveBox 0.9.77 source is supported.')
    changes = [
        ('archivebox/machine/detect.py',
         '                mac_address = next(mac_addrs(ips)).address\n',
         '                # Missing interface metadata is not an invented MAC address.\n'
         '                mac_address = next((address.address for address in mac_addrs(ips)), "Unknown")\n'),
        ('archivebox/services/resource_admission.py',
         '            host_swap_free = 0 if sys.platform.startswith("linux") else psutil.swap_memory().free\n',
         '            # Unreadable swap adds no assumed capacity; actual RAM is still checked.\n'
         '            host_swap_free = 0\n'
         '            if not sys.platform.startswith("linux"):\n'
         '                try:\n'
         '                    host_swap_free = psutil.swap_memory().free\n'
         '                except (OSError, psutil.Error):\n'
         '                    pass\n'),
    ]
    plan = []
    for name, before, after in changes:
        path = Path(distribution.locate_file(name))
        source = path.read_text()
        if after in source:
            continue
        if source.count(before) != 1:
            raise SystemExit('Unexpected source; no patches applied: ' + name)
        target = source.replace(before, after, 1)
        compile(target, str(path), 'exec')
        saved = path.with_name(path.name + '.pcl-original')
        if saved.exists():
            raise SystemExit('Original backup already exists; inspect before retrying: ' + name)
        plan.append((name, path, saved, source, target))
    report = []
    for name, path, saved, source, target in plan:
        saved.write_text(source)
        path.write_text(target)
        report.append({'file': name, 'before_sha256': hashlib.sha256(source.encode()).hexdigest(),
                       'after_sha256': hashlib.sha256(target.encode()).hexdigest()})
    print(json.dumps({'status': 'ok', 'version': distribution.version, 'changes': report}))


if __name__ == '__main__':
    main()
