"""Who this PC is talking to: live connections, the program behind each, and where.

Connections come from the OS (psutil). Locations come from DB-IP's free "IP to Country
Lite" database (CC BY 4.0), downloaded once (~4 MB) and refreshed monthly. Lookups are
done locally: no IP address is sent anywhere.
"""

from __future__ import annotations

import gzip
import ipaddress
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import psutil

log = logging.getLogger(__name__)

DBIP_URL = "https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz"
DB_NAME = "dbip-country-lite.mmdb"
MAX_DB_BYTES = 40 * 1024 * 1024
REFRESH_AFTER_S = 45 * 86400
# Remote ports ordinary apps use; anything else is worth a second look.
COMMON_PORTS = {53, 80, 443, 465, 587, 853, 993, 995, 3478, 5222, 5223, 5228, 8080, 8443}


def _centroids() -> dict[str, list[Any]]:
    text = resources.files("jarvis.maps").joinpath("country_centroids.json").read_text("utf-8")
    data: dict[str, list[Any]] = json.loads(text)["countries"]
    return data


CENTROIDS = _centroids()


class GeoDB:
    """Local IP → country lookups."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / DB_NAME
        self._reader: Any = None
        self._opened_mtime = 0.0

    @property
    def ready(self) -> bool:
        return self.path.exists()

    @property
    def stale(self) -> bool:
        return not self.ready or time.time() - self.path.stat().st_mtime > REFRESH_AFTER_S

    def download(self, fetch: Callable[[str], bytes]) -> None:
        """Fetch this month's file (or last month's, early in a month) and verify it."""
        import maxminddb

        now = datetime.now(UTC)
        prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        errors = []
        for month in (now.strftime("%Y-%m"), prev):
            try:
                raw = fetch(DBIP_URL.format(month=month))
            except OSError as exc:
                errors.append(f"{month}: {exc}")
                continue
            try:
                data = gzip.decompress(raw)
            except (OSError, EOFError) as exc:
                raise OSError(f"geolocation download is corrupt: {exc}") from exc
            if len(data) > MAX_DB_BYTES:
                raise OSError("geolocation database is unexpectedly large")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(data)
            # Verify before use: it must open as a MaxMind DB and know a well-known address.
            try:
                with maxminddb.open_database(str(tmp)) as reader:
                    probe = reader.get("8.8.8.8")
            except (ValueError, RuntimeError, OSError) as exc:  # InvalidDatabaseError et al.
                tmp.unlink(missing_ok=True)
                raise OSError(f"downloaded geolocation database is invalid: {exc}") from exc
            country = probe.get("country") if isinstance(probe, dict) else None
            if not isinstance(country, dict) or country.get("iso_code") != "US":
                tmp.unlink(missing_ok=True)
                raise OSError("downloaded geolocation database failed verification")
            self.close()
            tmp.replace(self.path)
            return
        raise OSError("couldn't download the geolocation database: " + "; ".join(errors))

    def lookup(self, ip: str) -> dict[str, Any] | None:
        if not self.ready:
            return None
        import maxminddb

        mtime = self.path.stat().st_mtime
        if self._reader is None or mtime != self._opened_mtime:
            self.close()
            self._reader = maxminddb.open_database(str(self.path))
            self._opened_mtime = mtime
        try:
            rec = self._reader.get(ip)
        except ValueError:  # not an IP address
            return None
        country = rec.get("country") if isinstance(rec, dict) else None
        code = country.get("iso_code") if isinstance(country, dict) else None
        if not code or code not in CENTROIDS:
            return None
        lat, lon, name = CENTROIDS[code]
        return {"code": code, "name": name, "lat": lat, "lon": lon}

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None


@dataclass
class Peer:
    ip: str
    ports: list[int]
    process: str
    pid: int | None
    connections: int
    country: dict[str, Any] | None
    flags: list[str] = field(default_factory=list)


@dataclass
class Exposure:
    port: int
    address: str
    process: str
    pid: int | None


@dataclass
class Snapshot:
    taken_at: float
    peers: list[Peer]
    listening: list[Exposure]
    geo_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self, limit: int = 15) -> str:
        if not self.peers:
            text = "No outside connections right now."
        else:
            countries: dict[str, int] = {}
            for p in self.peers:
                key = p.country["name"] if p.country else "unknown location"
                countries[key] = countries.get(key, 0) + p.connections
            top = ", ".join(f"{k} ({v})" for k, v in sorted(countries.items(), key=lambda x: -x[1]))
            lines = [
                f"{sum(p.connections for p in self.peers)} outside connections to "
                f"{len(self.peers)} addresses. By country: {top}."
            ]
            for p in self.peers[:limit]:
                where = p.country["name"] if p.country else "?"
                flag = f" [{', '.join(p.flags)}]" if p.flags else ""
                ports = ",".join(map(str, p.ports))
                who = p.process or "unknown program"
                lines.append(f"- {who} -> {p.ip}:{ports} ({where}){flag}")
            text = "\n".join(lines)
        if self.listening:
            ports = ", ".join(f"{e.port} ({e.process or '?'})" for e in self.listening[:20])
            text += f"\nAccepting connections from the network on: {ports}."
        if not self.geo_ready:
            text += "\n(Locations unavailable: the geolocation database isn't downloaded yet.)"
        return text


_names: dict[int, str] = {}


def _process_name(pid: int | None) -> str:
    if not pid:
        return ""  # no owner reported (or the idle process)
    if pid == 4:
        return "System"  # the Windows kernel
    if pid not in _names:
        try:
            _names[pid] = psutil.Process(pid).name()
        except (psutil.Error, OSError):
            _names[pid] = ""
    return _names[pid]


def _is_outside(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global


def snapshot(geo: GeoDB, connections: list[Any] | None = None) -> Snapshot:
    """Group current connections by remote address (blocking; run in a thread)."""
    conns = connections if connections is not None else psutil.net_connections(kind="inet")
    peers: dict[tuple[str, int | None], Peer] = {}
    listening: dict[tuple[int, int | None], Exposure] = {}
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.laddr:
            if c.laddr.ip in ("0.0.0.0", "::"):  # noqa: S104 - detecting, not binding
                listening.setdefault(
                    (int(c.laddr.port), c.pid),
                    Exposure(int(c.laddr.port), str(c.laddr.ip), _process_name(c.pid), c.pid),
                )
            continue
        if not c.raddr or c.status not in (psutil.CONN_ESTABLISHED, psutil.CONN_SYN_SENT):
            continue
        ip = str(c.raddr.ip).removeprefix("::ffff:")
        if not _is_outside(ip):
            continue
        key = (ip, c.pid)
        peer = peers.get(key)
        if peer is None:
            peer = peers[key] = Peer(ip, [], _process_name(c.pid), c.pid, 0, geo.lookup(ip))
        peer.connections += 1
        if int(c.raddr.port) not in peer.ports:
            peer.ports.append(c.raddr.port)
    for peer in peers.values():
        peer.ports.sort()
        if any(p not in COMMON_PORTS for p in peer.ports):
            peer.flags.append("unusual port")
        if not peer.process:
            peer.flags.append("unknown program")
    ordered = sorted(peers.values(), key=lambda p: (-len(p.flags), -p.connections, p.ip))
    exposed = sorted(listening.values(), key=lambda e: e.port)
    return Snapshot(time.time(), ordered, exposed, geo.ready)
