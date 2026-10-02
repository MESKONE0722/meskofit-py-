"""A tiny private certificate authority so the iPhone can reach MeskoFit over HTTPS on the home network.

Safari only allows live camera access (getUserMedia) on secure origins, and a LAN IP over plain HTTP is
not one. The CA is name-constrained to private IP ranges and local host names, so even if its key ever
leaked it could not be used to impersonate real websites to a phone that trusts it.

Port of internal/certs/certs.go. Unlike Go there is no per-handshake GetCertificate: the server
certificate is written to <dir>/server.crt + server.key (see ``Manager.leaf_paths``) and ``refresh()``
returns True when it was regenerated so the caller can reload it.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import os
import re
import secrets
import socket
import sys
import threading
from pathlib import Path
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

IP = ipaddress.IPv4Address | ipaddress.IPv6Address

# Stays under Apple's 825-day limit for TLS server certificates.
LEAF_VALIDITY = dt.timedelta(days=820)
_RENEW_WINDOW = dt.timedelta(days=30)

PRIVATE_NETS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network(c)
    for c in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # RFC 1918
        "100.64.0.0/10",  # CGNAT: Tailscale lives here
        "127.0.0.0/8", "169.254.0.0/16",
        "fc00::/7", "fe80::/10", "::1/128",
    )
]

# Local DNS suffixes the CA may sign for.
LOCAL_DOMAINS = ["localhost", "local", "lan", "home", "home.arpa", "internal"]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Manager:
    """Loads or creates the CA and keeps a server certificate for this machine's names and addresses."""

    def __init__(self, directory: str | Path, extra_hosts: Iterable[str] = ()):
        self.dir = Path(directory)
        self.extra = list(extra_hosts or ())
        self.constrained = False
        self.warnings: list[str] = []
        self._ca_cert: x509.Certificate | None = None
        self._ca_key: ec.EllipticCurvePrivateKey | None = None
        self._leaf: x509.Certificate | None = None
        self._san_key = ""
        self._permitted_dns: list[str] = []
        self._permitted_nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._lock = threading.Lock()

    # ── construction ──
    @classmethod
    def open(cls, directory: str | Path, extra_hosts: Iterable[str] = (), unconstrained: bool = False,
             reset: bool = False) -> "Manager":
        """Load or create the CA in ``directory`` and issue a server certificate for every local IP plus
        the machine's host names and any extra hosts. ``reset`` deletes the CA and certificates first."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        m = cls(d, extra_hosts)
        if reset:
            for f in ("ca.crt", "ca.key", "server.crt", "server.key"):
                try:
                    (d / f).unlink()
                except FileNotFoundError:
                    pass
        try:
            m._load_ca()
        except FileNotFoundError:
            m._create_ca(not unconstrained)
        except Exception as e:  # noqa: BLE001 - corrupt files etc.
            raise RuntimeError(f"loading CA: {e} (use -reset-ca to start over)") from e
        m.refresh()
        return m

    def _path(self, name: str) -> Path:
        return self.dir / name

    def _load_ca(self) -> None:
        cert_pem = self._path("ca.crt").read_bytes()
        key_pem = self._path("ca.key").read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        key = serialization.load_pem_private_key(key_pem, None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("CA key is not an EC key")
        self._ca_cert, self._ca_key = cert, key
        self._permitted_dns, self._permitted_nets = _permitted(cert)
        self.constrained = bool(self._permitted_dns or self._permitted_nets)

    def _create_ca(self, constrained: bool) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        host = host_label()
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, f"MeskoFit Local CA ({host})"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MeskoFit"),
        ])
        now = _utcnow()
        b = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(_rand_serial())
            .not_valid_before(now - dt.timedelta(hours=1))
            .not_valid_after(now + dt.timedelta(days=3652))
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False),
                critical=True)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        )
        if constrained:
            dns = list(LOCAL_DOMAINS)
            if host:
                dns.append(host)
            for h in self.extra:
                if _parse_ip(h) is None and valid_dns(h):
                    dns.append(h.lower())
            nets = list(PRIVATE_NETS)
            for h in self.extra:
                ip = _parse_ip(h)
                if ip is not None and not _in_nets(ip, PRIVATE_NETS):
                    nets.append(ipaddress.ip_network(ip))  # single host (/32 or /128)
            subtrees: list[x509.GeneralName] = [x509.DNSName(d) for d in _dedupe(dns)]
            subtrees += [x509.IPAddress(n) for n in nets]
            b = b.add_extension(x509.NameConstraints(permitted_subtrees=subtrees, excluded_subtrees=None), critical=True)
        cert = b.sign(key, hashes.SHA256())

        _write(self._path("ca.key"), key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()), 0o600)
        _write(self._path("ca.crt"), cert.public_bytes(serialization.Encoding.PEM), 0o644)
        for f in ("server.crt", "server.key"):
            try:
                self._path(f).unlink()
            except FileNotFoundError:
                pass
        self._ca_cert, self._ca_key, self.constrained = cert, key, constrained
        self._permitted_dns, self._permitted_nets = _permitted(cert)

    # ── server certificate ──
    def leaf_paths(self) -> tuple[str, str]:
        """(certificate file, key file) of the current server certificate, for uvicorn's ssl options."""
        return str(self._path("server.crt")), str(self._path("server.key"))

    @property
    def leaf(self) -> x509.Certificate | None:
        """The current server certificate."""
        return self._leaf

    def leaf_names(self) -> list[str]:
        """DNS names then IP addresses covered by the server certificate."""
        if self._leaf is None:
            return []
        return _cert_names(self._leaf)

    def leaf_not_after(self) -> dt.datetime | None:
        """Expiry of the server certificate (timezone-aware UTC)."""
        return self._leaf.not_valid_after_utc if self._leaf is not None else None

    def refresh(self) -> bool:
        """Re-issue the server certificate when the machine's addresses changed (new DHCP lease, VPN up)
        or it is close to expiring. Returns True when a new certificate was written to disk."""
        assert self._ca_cert is not None and self._ca_key is not None
        with self._lock:
            dns, ips = self.subjects()
            key = ",".join(dns) + "|" + ",".join(str(i) for i in ips)
            cur = self._leaf
            if cur is not None and key == self._san_key and cur.not_valid_after_utc - _utcnow() > _RENEW_WINDOW:
                return False
            # Reuse the certificate on disk if it still covers everything.
            leaf = self._load_disk_leaf()
            if leaf is not None and _covers(leaf, dns, ips) and leaf.not_valid_after_utc - _utcnow() > _RENEW_WINDOW:
                try:
                    leaf.verify_directly_issued_by(self._ca_cert)
                    ok = True
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    self._leaf, self._san_key = leaf, key
                    return False
            lk = ec.generate_private_key(ec.SECP256R1())
            now = _utcnow()
            san: list[x509.GeneralName] = [x509.DNSName(d) for d in dns] + [x509.IPAddress(i) for i in ips]
            b = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([
                    x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MeskoFit"),
                ]))
                .issuer_name(self._ca_cert.subject)
                .public_key(lk.public_key())
                .serial_number(_rand_serial())
                .not_valid_before(now - dt.timedelta(hours=1))
                .not_valid_after(now + LEAF_VALIDITY)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False),
                    critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_cert.public_key()), critical=False)
            )
            if san:
                b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
            cert = b.sign(self._ca_key, hashes.SHA256())
            _write(self._path("server.key"), lk.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()), 0o600)
            _write(self._path("server.crt"), cert.public_bytes(serialization.Encoding.PEM), 0o644)
            self._leaf, self._san_key = cert, key
            return True

    def _load_disk_leaf(self) -> x509.Certificate | None:
        """The certificate on disk if it parses and matches the key next to it."""
        try:
            cert = x509.load_pem_x509_certificate(self._path("server.crt").read_bytes())
            key = serialization.load_pem_private_key(self._path("server.key").read_bytes(), None)
            pub = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)  # noqa: E731
            if pub(cert.public_key()) != pub(key.public_key()):
                return None
            return cert
        except Exception:  # noqa: BLE001
            return None

    def subjects(self) -> tuple[list[str], list[IP]]:
        """Names the server certificate should cover, skipping any the CA's name constraints don't allow
        (a warning is recorded for each). Returns (dns names, sorted IPs)."""
        self.warnings = []
        dns = ["localhost"]
        h = host_label()
        if h:
            dns += [h, h + ".local"]
        ips: list[IP] = [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]
        ips += local_ips()
        for raw in self.extra:
            raw = raw.strip()
            if not raw:
                continue
            ip = _parse_ip(raw)
            if ip is not None:
                ips.append(ip)
            elif valid_dns(raw):
                dns.append(raw.lower())
        ok_dns: list[str] = []
        for d in _dedupe(dns):
            if self._dns_allowed(d):
                ok_dns.append(d)
            else:
                self.warnings.append(
                    f"\"{d}\" is outside the CA's local-only constraints; start with -reset-ca -ca-unconstrained to include it")
        ok_ips: list[IP] = []
        seen: set[str] = set()
        for ip in ips:
            if str(ip) in seen:
                continue
            seen.add(str(ip))
            if not self.constrained or _in_nets(ip, self._permitted_nets):
                ok_ips.append(ip)
            else:
                self.warnings.append(f"{ip} is a public address and is left out of the certificate")
        ok_ips.sort(key=str)  # same ordering as Go's string sort
        return ok_dns, ok_ips

    def _dns_allowed(self, d: str) -> bool:
        if not self.constrained:
            return True
        return any(d == p or d.endswith("." + p) for p in self._permitted_dns)

    # ── CA material ──
    def ca_pem(self) -> bytes:
        """The CA certificate in PEM form."""
        assert self._ca_cert is not None
        return self._ca_cert.public_bytes(serialization.Encoding.PEM)

    def ca_der(self) -> bytes:
        """The CA certificate in DER form."""
        assert self._ca_cert is not None
        return self._ca_cert.public_bytes(serialization.Encoding.DER)

    def fingerprint(self) -> str:
        """The CA's SHA-256 fingerprint, as iOS shows it in Settings (``AB CD ...``)."""
        h = hashlib.sha256(self.ca_der()).hexdigest().upper()
        return " ".join(h[i:i + 2] for i in range(0, len(h), 2))

    def mobileconfig(self) -> bytes:
        """The CA wrapped in an Apple configuration profile so iOS offers to install it from Safari."""
        assert self._ca_cert is not None
        der = self.ca_der()
        digest = hashlib.sha256(der).digest()

        def uuid(b: bytes) -> str:
            h = b[:16].hex().upper()
            return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-8{h[17:20]}-{h[20:32]}"

        inner, outer = uuid(digest[:16]), uuid(digest[16:])
        cn = self._ca_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        return _MOBILECONFIG.format(
            data=base64.b64encode(der).decode(), name=_xml_escape(str(cn)), inner=inner, outer=outer).encode()


_MOBILECONFIG = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>PayloadContent</key>
\t<array>
\t\t<dict>
\t\t\t<key>PayloadCertificateFileName</key>
\t\t\t<string>meskofit-ca.cer</string>
\t\t\t<key>PayloadContent</key>
\t\t\t<data>{data}</data>
\t\t\t<key>PayloadDescription</key>
\t\t\t<string>Lets this iPhone trust your own MeskoFit server on your home network.</string>
\t\t\t<key>PayloadDisplayName</key>
\t\t\t<string>{name}</string>
\t\t\t<key>PayloadIdentifier</key>
\t\t\t<string>app.meskofit.ca.{inner}</string>
\t\t\t<key>PayloadType</key>
\t\t\t<string>com.apple.security.root</string>
\t\t\t<key>PayloadUUID</key>
\t\t\t<string>{inner}</string>
\t\t\t<key>PayloadVersion</key>
\t\t\t<integer>1</integer>
\t\t</dict>
\t</array>
\t<key>PayloadDescription</key>
\t<string>Installs the MeskoFit local certificate authority (limited to home-network addresses).</string>
\t<key>PayloadDisplayName</key>
\t<string>MeskoFit HTTPS</string>
\t<key>PayloadIdentifier</key>
\t<string>app.meskofit.profile.{outer}</string>
\t<key>PayloadRemovalDisallowed</key>
\t<false/>
\t<key>PayloadType</key>
\t<string>Configuration</string>
\t<key>PayloadUUID</key>
\t<string>{outer}</string>
\t<key>PayloadVersion</key>
\t<integer>1</integer>
</dict>
</plist>
"""


# ───────────────────────── local addresses ─────────────────────────

def local_ips() -> list[ipaddress.IPv4Address]:
    """This machine's usable IPv4 addresses (interface up, not loopback, not link-local), home-network
    ones first (stable order within a rank)."""
    out: list[ipaddress.IPv4Address] = []
    seen: set[str] = set()
    for s in _interface_ipv4s():
        try:
            ip = ipaddress.IPv4Address(s)
        except ValueError:
            continue
        if ip.is_link_local or ip.is_loopback or ip.is_unspecified or str(ip) in seen:
            continue
        seen.add(str(ip))
        out.append(ip)
    out.sort(key=ip_rank)  # list.sort is stable
    return out


def _interface_ipv4s() -> list[str]:
    """IPv4 addresses of interfaces that are up and not loopback. Linux uses per-interface ioctls (exact
    flags); other platforms fall back to the host name and the default route's source address."""
    found: list[str] = []
    if sys.platform.startswith("linux"):
        try:
            import fcntl
            import struct

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                for _, name in socket.if_nameindex():
                    ifr = struct.pack("256s", name.encode()[:15])
                    try:
                        flags = struct.unpack("16sH", fcntl.ioctl(s.fileno(), 0x8913, ifr)[:18])[1]  # SIOCGIFFLAGS
                        if not flags & 0x1 or flags & 0x8:  # IFF_UP / IFF_LOOPBACK
                            continue
                        addr = fcntl.ioctl(s.fileno(), 0x8915, ifr)[20:24]  # SIOCGIFADDR
                    except OSError:
                        continue  # no IPv4 address on this interface
                    found.append(socket.inet_ntoa(addr))
            finally:
                s.close()
        except Exception:  # noqa: BLE001
            pass
        if found:
            return found
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    for target in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):  # UDP connect sends no packets
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((target, 9))
                found.append(s.getsockname()[0])
            finally:
                s.close()
        except OSError:
            continue
    return found


def ip_rank(ip: IP | str) -> int:
    """Sort key putting the most likely phone-reachable address first."""
    ip = ipaddress.ip_address(str(ip))
    if ip.version != 4:
        return 4
    a, b = ip.packed[0], ip.packed[1]
    if a == 192 and b == 168:
        return 0
    if a == 10:
        return 1
    if a == 100 and b & 0xC0 == 64:
        return 2  # Tailscale / CGNAT
    if a == 172 and b & 0xF0 == 16:
        return 3  # often Docker / WSL / Hyper-V virtual switches
    return 4


def is_tailscale(ip: IP | str) -> bool:
    """True if ip is in Tailscale's 100.64.0.0/10 range."""
    try:
        ip = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    return ip.version == 4 and ip.packed[0] == 100 and ip.packed[1] & 0xC0 == 64


# ───────────────────────── helpers ─────────────────────────

_label_re = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def valid_dns(h: str) -> bool:
    """True for a syntactically valid host name (labels of letters, digits and hyphens)."""
    h = h.removesuffix(".").lower()
    if not h or len(h) > 253:
        return False
    return all(_label_re.match(label) for label in h.split("."))


def host_label() -> str:
    """This machine's short host name, lower-cased, or '' if it isn't a valid DNS label."""
    try:
        h = socket.gethostname()
    except OSError:
        return ""
    h = h.split(".")[0].lower()
    return h if valid_dns(h) else ""


def _parse_ip(h: str) -> IP | None:
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        return None


def _permitted(cert: x509.Certificate):
    try:
        nc = cert.extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        return [], []
    subtrees = nc.permitted_subtrees or []
    dns = [g.value for g in subtrees if isinstance(g, x509.DNSName)]
    nets = [g.value for g in subtrees if isinstance(g, x509.IPAddress)]
    return dns, nets


def _cert_names(c: x509.Certificate) -> list[str]:
    try:
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return list(san.get_values_for_type(x509.DNSName)) + [str(i) for i in san.get_values_for_type(x509.IPAddress)]


def _covers(c: x509.Certificate, dns: list[str], ips: list[IP]) -> bool:
    has = set(_cert_names(c))
    return all(d in has for d in dns) and all(str(i) in has for i in ips)


def _in_nets(ip: IP, nets) -> bool:
    return any(ip.version == n.version and ip in n for n in nets)


def _rand_serial() -> int:
    return secrets.randbelow(1 << 126) + 1


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in items:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _write(path: Path, data: bytes, mode: int) -> None:
    """Write a file with the given permissions (the key is never world-readable, even briefly)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    finally:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


Manager.mobile_config = Manager.mobileconfig  # type: ignore[attr-defined]  # Go-style alias
