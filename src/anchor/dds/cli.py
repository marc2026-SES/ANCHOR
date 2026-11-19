import argparse
import json
from pathlib import Path
import signal
import threading

from ..config import Settings
from .config import Config


def main(argv=None):
    parser = argparse.ArgumentParser(description='ANCHOR native DDS vibration publisher / IPE')
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['check', 'run', 'provision'])
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    settings = Settings.load(cfg.host_config)
    expected = 'ship' if cfg.mode == 'publisher' else 'edge'
    if settings.role != expected or len(settings.vessel_id.encode()) > 64:
        raise ValueError('DDS mode does not match host role or vessel ID')
    marker = Path(cfg.host_config).parent.parent.parent / '.anchor-role'
    if marker.exists() and marker.read_text().strip() != expected:
        raise ValueError('DDS role does not match the deployed source package')
    if cfg.mode == 'ipe' and not settings.dds_ipe_enabled:
        raise ValueError('Set dds_ipe_enabled=true before provisioning the IPE')
    if args.command == 'provision':
        if cfg.mode != 'ipe':
            raise ValueError('Only the coastal IPE provisions DDS resources')
        from ..client import Client
        from .resources import provision
        print(json.dumps(provision(Client(settings.nodes['edge'], 'CAdmin'))))
        return
    if args.command == 'check':
        print(json.dumps({'role': expected, 'mode': cfg.mode, 'domain': cfg.domain_id,
                          'topic': cfg.topic, 'qos': cfg.qos.describe(),
                          'interface': cfg.interface, 'peers': cfg.peers,
                          'validation': 'configuration only; no network test'}, indent=2))
        return
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    from .runtime import run
    try:
        run(cfg, settings, stop)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith('cyclonedds'):
            raise SystemExit('Install the optional native DDS dependency: python -m pip install -e ".[dds]"') from exc
        raise


if __name__ == '__main__':
    main()
