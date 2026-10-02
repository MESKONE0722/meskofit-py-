"""Port of internal/certs/certs_test.go, plus an `openssl verify` cross-check."""
from __future__ import annotations

import datetime as dt
import ipaddress
import shutil
import socket
import ssl
import subprocess
import threading
import xml.etree.ElementTree as ET

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store

from meskofit import certs


def _verify(m: certs.Manager, leaf: x509.Certificate, name: str) -> None:
    """Chain + name verification against only our CA (enforces name constraints). Raises on failure."""
    subject = x509.IPAddress(ipaddress.ip_address(name)) if _is_ip(name) else x509.DNSName(name)
    verifier = PolicyBuilder().store(Store([m._ca_cert])).build_server_verifier(subject)
    verifier.verify(leaf, [])


def _is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _sign_evil(m: certs.Manager, dns: str, ip: str) -> x509.Certificate:
    k = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")]))
        .issuer_name(m._ca_cert.subject)
        .public_key(k.public_key())
        .serial_number(certs._rand_serial())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns), x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
        .sign(m._ca_key, hashes.SHA256())
    )


def test_manager(tmp_path):
    m = certs.Manager.open(tmp_path, ["fit.lan", "192.168.50.5"], False, False)
    assert m.constrained, "CA should be name constrained by default"
    leaf = m.leaf
    assert leaf is not None
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc <= dt.timedelta(days=825)
    names = m.leaf_names()
    assert "fit.lan" in names and "192.168.50.5" in names and "127.0.0.1" in names
    for name in names + ["192.168.50.5", "127.0.0.1"]:
        _verify(m, leaf, name)

    # A certificate for a real website signed by this CA must be rejected.
    evil = _sign_evil(m, "bank.com", "8.8.8.8")
    with pytest.raises(Exception):
        _verify(m, evil, "bank.com")
    with pytest.raises(Exception):
        _verify(m, evil, "8.8.8.8")

    # Reopening reuses the same CA and certificate.
    fp = m.fingerprint()
    m2 = certs.Manager.open(tmp_path, ["fit.lan", "192.168.50.5"], False, False)
    assert m2.fingerprint() == fp, "CA should persist across restarts"
    assert m2.leaf.serial_number == leaf.serial_number, "unchanged SANs should reuse the server certificate"
    assert m2.refresh() is False

    # The profile must be well-formed XML containing the certificate.
    mc = m.mobileconfig()
    ET.fromstring(mc)
    assert b"com.apple.security.root" in mc
    assert m.ca_pem().startswith(b"-----BEGIN CERTIFICATE-----")
    assert x509.load_der_x509_certificate(m.ca_der()) == m._ca_cert
    assert len(m.fingerprint().split(" ")) == 32


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_openssl_verify(tmp_path):
    m = certs.Manager.open(tmp_path, ["fit.lan", "192.168.50.5"], False, False)
    crt, key = m.leaf_paths()
    ca = str(tmp_path / "ca.crt")

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["openssl", "verify", "-CAfile", ca, *args], capture_output=True, text=True)

    for flag in (["-verify_hostname", "localhost"], ["-verify_hostname", "fit.lan"], ["-verify_ip", "192.168.50.5"],
                 ["-verify_ip", "127.0.0.1"]):
        r = run("-purpose", "sslserver", *flag, crt)
        assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr

    # name-constrained: a cert for a public name/IP from the same CA must fail openssl too
    evil = _sign_evil(m, "bank.com", "8.8.8.8")
    p = tmp_path / "evil.crt"
    p.write_bytes(evil.public_bytes(serialization.Encoding.PEM))
    r = run("-purpose", "sslserver", str(p))
    assert r.returncode != 0, "openssl accepted a certificate outside the name constraints"
    # and the CA itself carries the critical constraint
    out = subprocess.run(["openssl", "x509", "-in", ca, "-noout", "-text"], capture_output=True, text=True).stdout
    assert "X509v3 Name Constraints: critical" in out and "IP:100.64.0.0/255.192.0.0" in out


def test_tls_handshake(tmp_path):
    """Real TLS handshake with a client that trusts only our CA."""
    m = certs.Manager.open(tmp_path, [], False, False)
    crt, key = m.leaf_paths()
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(crt, key)
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    port = lsock.getsockname()[1]

    def serve():
        c, _ = lsock.accept()
        with sctx.wrap_socket(c, server_side=True) as s:
            s.recv(10)
            s.sendall(b"ok")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cctx = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with cctx.wrap_socket(raw, server_hostname="localhost") as s:
            s.sendall(b"hi")
            assert s.recv(10) == b"ok"
    t.join(5)
    lsock.close()


def test_public_host_warns(tmp_path):
    d = tmp_path / "a"
    certs.Manager.open(d, None, False, False)
    # Added after the CA exists -> outside its constraints -> warned and skipped.
    m = certs.Manager.open(d, ["fit.example.com"], False, False)
    assert any("fit.example.com" in w for w in m.warnings)
    assert "fit.example.com" not in m.leaf_names()
    # Given at first start, it becomes part of the CA's permitted names.
    first = certs.Manager.open(tmp_path / "b", ["fit.example.com"], False, False)
    assert not any("fit.example.com" in w for w in first.warnings)
    assert "fit.example.com" in first.leaf_names()
    u = certs.Manager.open(tmp_path / "c", ["fit.example.com"], True, False)
    assert not u.constrained and "fit.example.com" in u.leaf_names()


def test_refresh_on_address_change(tmp_path, monkeypatch):
    m = certs.Manager.open(tmp_path, [], False, False)
    serial = m.leaf.serial_number
    assert m.refresh() is False
    ip = ipaddress.IPv4Address("192.168.77.9")
    monkeypatch.setattr(certs, "local_ips", lambda: [ip])
    assert m.refresh() is True, "new address must re-issue the leaf"
    assert m.leaf.serial_number != serial and "192.168.77.9" in m.leaf_names()
    assert m.refresh() is False
    _verify(m, m.leaf, "192.168.77.9")
    # a public address is dropped with a warning
    monkeypatch.setattr(certs, "local_ips", lambda: [ipaddress.IPv4Address("8.8.4.4")])
    m.refresh()
    assert "8.8.4.4" not in m.leaf_names() and any("public address" in w for w in m.warnings)


def test_reset_and_corrupt(tmp_path):
    m = certs.Manager.open(tmp_path, [], False, False)
    fp = m.fingerprint()
    assert certs.Manager.open(tmp_path, [], False, True).fingerprint() != fp
    (tmp_path / "ca.crt").write_text("garbage")
    with pytest.raises(RuntimeError, match="-reset-ca"):
        certs.Manager.open(tmp_path, [], False, False)
    assert (tmp_path / "ca.key").stat().st_mode & 0o077 == 0


def test_helpers():
    assert certs.is_tailscale("100.64.0.1") and certs.is_tailscale("100.127.255.255")
    assert not certs.is_tailscale("100.128.0.1") and not certs.is_tailscale("192.168.1.1") and not certs.is_tailscale("::1")
    ranks = [certs.ip_rank(a) for a in ("192.168.1.2", "10.0.0.1", "100.100.1.1", "172.17.0.1", "8.8.8.8")]
    assert ranks == [0, 1, 2, 3, 4]
    assert certs.valid_dns("fit.lan") and not certs.valid_dns("bad_name") and not certs.valid_dns("-a.b") and not certs.valid_dns("")
    ips = certs.local_ips()
    assert all(not ip.is_loopback and not ip.is_link_local for ip in ips)
    assert [certs.ip_rank(i) for i in ips] == sorted(certs.ip_rank(i) for i in ips)
