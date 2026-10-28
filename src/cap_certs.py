"""
cap_certs.py — HTTPS certificates for the CAP dashboard (https://cap.lan:8421).

Clio only accepts https (or http://127.0.0.1) OAuth redirect URIs, so "Sign in
with Clio" needs the dashboard served over HTTPS. This makes a small private
certificate authority (the "CAP Root CA") and uses it to sign the dashboard's
server certificate. Every staff PC installs the root CA's public certificate
once (install-cap-cert.bat), after which browsers trust https://cap.lan with
no warning.

Files (all under data/certs/, gitignored by the blanket data/ rule):
  cap-root-ca.key   root CA private key — never leaves this machine
  cap-root-ca.crt   root CA public certificate — what staff PCs install
  cap-server.key    dashboard server private key (uvicorn --ssl-keyfile)
  cap-server.crt    dashboard server certificate (uvicorn --ssl-certfile)

Usage:
  uv run src/cap_certs.py            # create the root CA (if missing) + server cert
  uv run src/cap_certs.py --server   # re-issue only the server cert, same root CA

Rerunnable: an existing root CA is reused, never overwritten — replacing it
would silently break trust on every PC that already installed it. The server
cert is re-issued each run (it's cheap and nothing installs it).
"""

import argparse
import ipaddress
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CERT_DIR = Path(__file__).resolve().parent.parent / "data" / "certs"
ROOT_KEY = CERT_DIR / "cap-root-ca.key"
ROOT_CRT = CERT_DIR / "cap-root-ca.crt"
SERVER_KEY = CERT_DIR / "cap-server.key"
SERVER_CRT = CERT_DIR / "cap-server.crt"

ROOT_CA_NAME = "Collier Law CAP Root CA"
ROOT_CA_YEARS = 20
# Browsers reject server certs valid longer than ~398 days only for publicly
# trusted CAs; a private root installed by hand is exempt, but stay under it
# anyway so nothing has to special-case this cert. Re-run --server yearly.
SERVER_CERT_DAYS = 397

# Every name staff might type to reach the dashboard. cap.lan is the canonical
# one (it's what the Clio redirect URI and CAP Dashboard.url use); the machine
# hostname and localhost cover Ted at the console.
SERVER_DNS_NAMES = ["cap.lan", "localhost", socket.gethostname().lower()]
SERVER_IPS = ["127.0.0.1"]


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def create_root_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, ROOT_CA_NAME),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Collier Law"),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365 * ROOT_CA_YEARS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(ROOT_KEY, key)
    _write_cert(ROOT_CRT, cert)
    return key, cert


def load_root_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = serialization.load_pem_private_key(ROOT_KEY.read_bytes(), password=None)
    cert = x509.load_pem_x509_certificate(ROOT_CRT.read_bytes())
    return key, cert


def create_server_cert(root_key: rsa.RSAPrivateKey, root_cert: x509.Certificate) -> x509.Certificate:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    san = x509.SubjectAlternativeName(
        [x509.DNSName(n) for n in dict.fromkeys(SERVER_DNS_NAMES)]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in SERVER_IPS]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_DNS_NAMES[0])]))
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=SERVER_CERT_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(san, critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    _write_key(SERVER_KEY, key)
    _write_cert(SERVER_CRT, cert)
    return cert


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", action="store_true", help="Re-issue only the server cert (root CA must already exist)")
    args = ap.parse_args()

    CERT_DIR.mkdir(parents=True, exist_ok=True)

    if ROOT_KEY.exists() and ROOT_CRT.exists():
        root_key, root_cert = load_root_ca()
        print(f"Reusing existing root CA: {ROOT_CRT}")
    elif args.server:
        print("ERROR: --server given but no root CA exists yet. Run without --server first.")
        sys.exit(1)
    else:
        root_key, root_cert = create_root_ca()
        print(f"Created root CA: {ROOT_CRT}  (valid {ROOT_CA_YEARS} years)")

    cert = create_server_cert(root_key, root_cert)
    names = ", ".join(SERVER_DNS_NAMES + SERVER_IPS)
    print(f"Issued server cert: {SERVER_CRT}")
    print(f"  names : {names}")
    print(f"  until : {cert.not_valid_after_utc:%Y-%m-%d}")
    print()
    print("Staff PCs: run install-cap-cert.bat once (installs cap-root-ca.crt for the current user).")


if __name__ == "__main__":
    main()
