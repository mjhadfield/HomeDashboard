"""Publish a friendly `<name>.local` address for this machine's LAN IP over mDNS (Avahi).

    python -m app.mdns dash        # -> dash.local

Wi-Fi reconnects can change the IP, so we re-check periodically and republish. Needs `avahi-publish`
(package avahi / avahi-utils) and a running avahi-daemon.
"""
from __future__ import annotations

import subprocess
import sys
import time

from . import config


def main(name: str) -> None:
    host = name if name.endswith(".local") else f"{name}.local"
    proc, ip = None, None
    try:
        while True:
            current = config.lan_ip()
            if proc is None or proc.poll() is not None or current != ip:
                if proc and proc.poll() is None:
                    proc.terminate()
                    proc.wait(5)
                ip = current
                print(f"publishing {host} -> {ip}", flush=True)
                proc = subprocess.Popen(["avahi-publish", "-a", "-R", host, ip])
            time.sleep(30)
    finally:
        if proc and proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dash")
