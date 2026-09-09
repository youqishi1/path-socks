#!/usr/bin/env python3
"""Choose a high TCP port. Never stop processes or alter network settings."""
import argparse
import random
import socket
from pathlib import Path


def available(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", port))
        # Reject IPv6-only listeners too, so a dual-stack app cannot surprise us.
        if socket.has_ipv6:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(("::", port))
        return True
    except OSError as error:
        if error.errno in (97, 99):  # Linux: IPv6 disabled / no IPv6 address.
            return True
        return False


def owned_listener(port, pid):
    if pid <= 1:
        return False
    try:
        # Reuse an occupied saved port only when the actual gateway owns it.
        if Path(f"/proc/{pid}/exe").resolve() not in (Path("/opt/path-socks/path-socks"), Path("/opt/sbb-reality/xray")):
            return False
        sockets = set()
        for entry in Path(f"/proc/{pid}/fd").iterdir():
            try: sockets.add(entry.readlink().name)
            except OSError: pass  # A connection may close during inspection.
        listeners = []
        for name in ("tcp", "tcp6"):
            for line in Path(f"/proc/net/{name}").read_text().splitlines()[1:]:
                columns = line.split()
                if columns[3] == "0A" and int(columns[1].split(":")[1], 16) == port:
                    listeners.append(f"socket:[{columns[9]}]")
        return bool(listeners) and all(item in sockets for item in listeners)
    except (OSError, ValueError, IndexError):
        return False


def choose(preferred, pid=0, explicit=False):
    if not 10240 <= preferred <= 65535:
        raise ValueError("端口必须是10240到65535之间的整数")
    if available(preferred) or owned_listener(preferred, pid):
        return preferred
    if explicit:
        raise ValueError(f"TCP {preferred} 已被其他程序占用，请选择其他端口")
    # Below the usual Linux ephemeral range; exclude the actual range as well.
    low, high = 32768, 60999
    try:
        low, high = map(int, Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split())
    except OSError:
        pass
    candidates = [p for p in range(20000, 30000) if not low <= p <= high]
    random.SystemRandom().shuffle(candidates)
    for port in candidates:
        if available(port):
            return port
    raise ValueError("未找到空闲高位端口，请手动指定")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--explicit", action="store_true")
    args = parser.parse_args()
    try:
        print(choose(args.port, args.pid, args.explicit))
    except ValueError as error:
        parser.exit(1, f"错误：{error}\n")
