"""Host-only password recovery and the HTTPS gateway entry point."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import ssl
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .store import AuthError, AuthStore


def create_certificate(directory: Path, hostname: str) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    certificate, key_path = directory / "tls.crt", directory / "tls.key"
    if certificate.exists() or key_path.exists():
        if not certificate.is_file() or not key_path.is_file():
            raise ValueError(
                "TLS material is incomplete. Restore both tls.crt and tls.key before starting."
            )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key_path)
        value = x509.load_pem_x509_certificate(certificate.read_bytes())
        if not value.not_valid_before_utc <= datetime.now(UTC) < value.not_valid_after_utc:
            raise ValueError(
                "TLS certificate is outside its validity dates. Check the host clock or run "
                "conker auth renew-certificate, then restart the gateway "
                "and trust its new certificate."
            )
        return certificate, key_path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    names = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    if hostname != "localhost":
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(hostname)))
        except ValueError:
            names.append(x509.DNSName(hostname))
    now = datetime.now(UTC)
    certificate_value = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    # Exclusive creation refuses a concurrent initializer instead of replacing its key.
    with key_path.open("xb") as stream:
        if os.name == "posix":
            os.chmod(key_path, 0o600)
        stream.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    with certificate.open("xb") as stream:
        stream.write(certificate_value.public_bytes(serialization.Encoding.PEM))
    return certificate, key_path


def renew_certificate(directory: Path, hostname: str) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".tls-renew-", dir=directory))
    certificate, key = create_certificate(staging, hostname)
    # An interruption between these replacements leaves a mismatched pair, which
    # startup rejects. Retrying this host command repairs it without touching auth.
    key.replace(directory / "tls.key")
    certificate.replace(directory / "tls.crt")
    staging.rmdir()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Conker browser gateway and host password recovery"
    )
    parser.add_argument(
        "command",
        choices=[
            "serve",
            "setup",
            "reset-password",
            "revoke-all",
            "certificate",
            "health",
            "renew-certificate",
        ],
    )
    parser.add_argument("--db", default=os.environ.get("GATEWAY_DB_PATH", "/auth/auth.db"))
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args()
    # The database's sidecars must have the same protection as the password verifier.
    if os.name == "posix":
        os.umask(0o077)
    if args.command == "serve":
        import uvicorn

        from .api import Config, create_app

        config = Config.environment()
        config.validate()
        certificate, key = create_certificate(
            Path(config.database).parent, urlsplit(config.origin).hostname
        )
        uvicorn.run(
            create_app(config),
            host="0.0.0.0",
            port=args.port,
            ssl_certfile=str(certificate),
            ssl_keyfile=str(key),
            proxy_headers=False,
        )
    elif args.command == "health":
        import httpx

        from .api import Config

        config = Config.environment()
        config.validate()
        # Reach the local listener while retaining the configured TLS name and Host.
        context = ssl.create_default_context(cafile=str(Path(config.database).parent / "tls.crt"))
        with httpx.Client(verify=context, trust_env=False, timeout=10) as client:
            response = client.get(
                "https://localhost:8050/health", headers={"Host": urlsplit(config.origin).netloc}
            )
            response.raise_for_status()
            print(response.text)
    elif args.command == "renew-certificate":
        from .api import Config

        config = Config.environment()
        config.validate()
        renew_certificate(Path(args.db).parent, urlsplit(config.origin).hostname)
        print("Certificate renewed. Restart the gateway and trust the newly exported certificate.")
    elif args.command == "certificate":
        print((Path(args.db).parent / "tls.crt").read_text(), end="")
    else:
        auth = AuthStore(args.db)
        if args.command == "revoke-all":
            auth.revoke()
            print(
                "Every browser session was revoked. Already dispatched actions cannot be recalled."
            )
            return
        print(
            "This password protects browser access to your conversations and approval decisions.\n"
            "Only a salted verifier is kept in this machine's gateway volume; "
            "the password is not saved.\n"
            "Anyone controlling this host can reset it. "
            "Resetting cannot restore lost data, vault keys\n"
            "or forgotten content, and cannot undo an action already sent to another service."
        )
        password = getpass.getpass("Choose a passphrase (at least 15 characters): ")
        if password != getpass.getpass("Repeat it: "):
            raise AuthError("Passwords differ. Run the command again.", 422)
        auth.set_password(password, initial=args.command == "setup")
        print(
            "Password saved. Every previous browser session was revoked. "
            "Sign in with the new password."
        )


if __name__ == "__main__":
    try:
        main()
    except (AuthError, ValueError, OSError) as exc:
        print(f"Gateway did not complete: {exc}", file=sys.stderr)
        sys.exit(1)
