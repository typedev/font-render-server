"""Addresses clients can use to reach this server (install summary, web admin).

With DHCP the IP can change; the computer name usually keeps working on a LAN (Windows
clients resolve it by NetBIOS/LLMNR, macOS and Linux as <name>.local by mDNS). A fixed IP
is a DHCP reservation in the router (docs/deployment.md#finding-the-server).
"""

import ipaddress
import socket

import psutil


def lan_ipv4() -> list[dict]:
    """IPv4 addresses of interfaces that are up: [{"interface", "address"}], LAN ones first."""
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        if not stats.get(name) or not stats[name].isup:
            continue
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = ipaddress.ip_address(a.address)
            if ip.is_loopback or ip.is_link_local:  # 127.x, 169.254.x (no DHCP answer)
                continue
            out.append({"interface": name, "address": a.address, "private": ip.is_private})
    return sorted(out, key=lambda x: (not x["private"], x["interface"]))


def addresses(port: int) -> dict:
    """Ways to reach the server: by name (stable under DHCP) and by the current IPs."""
    name = socket.gethostname()
    return {
        "hostname": name,
        "by_name": [f"http://{name}:{port}", f"http://{name.lower()}.local:{port}"],
        "by_ip": [{"interface": x["interface"], "url": f"http://{x['address']}:{port}"}
                  for x in lan_ipv4()],
    }
