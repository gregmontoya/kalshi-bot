"""Kalshi RSA-PSS Authentication Signer.

Non-Negotiable Exchange Law:
Every authenticated request requires:
- KALSHI-ACCESS-KEY: Key ID
- KALSHI-ACCESS-TIMESTAMP: Timestamp in milliseconds (str)
- KALSHI-ACCESS-SIGNATURE: Base64 RSA-PSS with SHA256 over (timestamp + METHOD + path_without_query)
"""

import base64
import time
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

PrivateKeyType = Union[RSAPrivateKey, Ed25519PrivateKey]
PublicKeyType = Union[RSAPublicKey, Ed25519PublicKey]


class KalshiAuthSigner:
    """Handles RSA-PSS & Ed25519 signing and header generation according to Kalshi v2 API requirements."""

    def __init__(
        self,
        key_id: str,
        private_key: Optional[Union[PrivateKeyType, str, Path]] = None,
    ):
        self.key_id = key_id
        self._private_key: Optional[PrivateKeyType] = None

        if private_key is not None:
            self.load_private_key(private_key)

    @property
    def has_key(self) -> bool:
        return self._private_key is not None

    def load_private_key(self, key_source: Union[PrivateKeyType, str, Path]) -> None:
        """Loads RSA or Ed25519 private key from an existing key object, PEM file path, or PEM string."""
        if isinstance(key_source, (RSAPrivateKey, Ed25519PrivateKey)):
            self._private_key = key_source
            return

        pem_bytes: bytes
        if isinstance(key_source, Path) or (
            isinstance(key_source, str)
            and (Path(key_source).exists() or "/" in key_source or "\\" in key_source)
        ):
            path = Path(key_source)
            if path.exists():
                pem_bytes = path.read_bytes()
            else:
                pem_bytes = str(key_source).encode("utf-8")
        else:
            pem_bytes = str(key_source).encode("utf-8")

        loaded = serialization.load_pem_private_key(
            pem_bytes,
            password=None,
        )
        if isinstance(loaded, (RSAPrivateKey, Ed25519PrivateKey)):
            self._private_key = loaded
        else:
            raise ValueError(f"Unsupported key type: {type(loaded)}")

    @classmethod
    def generate_key_pair(cls, key_size: int = 2048) -> tuple[RSAPrivateKey, RSAPublicKey]:
        """Generates a new RSA key pair for testing or new API credential setup."""
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
        )
        return private_key, private_key.public_key()

    @staticmethod
    def export_private_key_pem(private_key: RSAPrivateKey) -> str:
        """Exports private key to PKCS8 PEM format."""
        return private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

    @staticmethod
    def export_public_key_pem(public_key: RSAPublicKey) -> str:
        """Exports public key to SubjectPublicKeyInfo PEM format."""
        return public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

    @staticmethod
    def extract_path_without_query(path_or_url: str) -> str:
        """Strips query parameters and protocol/host to return path without query."""
        parsed = urlparse(path_or_url)
        return parsed.path if parsed.path else path_or_url.split("?")[0]

    def sign_request(
        self,
        method: str,
        path_or_url: str,
        timestamp_ms: Optional[int] = None,
    ) -> tuple[str, str]:
        """
        Signs the request message: timestamp + METHOD + path_without_query
        Returns:
            (timestamp_str, base64_signature)
        """
        if not self._private_key:
            raise ValueError("RSA private key not loaded into KalshiAuthSigner")

        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)

        timestamp_str = str(timestamp_ms)
        path = self.extract_path_without_query(path_or_url)
        method_clean = method.upper().strip()

        # Non-negotiable Kalshi law: timestamp + METHOD + path_without_query
        message_to_sign = f"{timestamp_str}{method_clean}{path}".encode("utf-8")

        # Base64 RSA-PSS or Ed25519 signature
        if isinstance(self._private_key, Ed25519PrivateKey):
            raw_signature = self._private_key.sign(message_to_sign)
        else:
            raw_signature = self._private_key.sign(
                message_to_sign,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )

        signature_b64 = base64.b64encode(raw_signature).decode("utf-8")
        return timestamp_str, signature_b64

    def get_auth_headers(
        self,
        method: str,
        path_or_url: str,
        timestamp_ms: Optional[int] = None,
    ) -> dict[str, str]:
        """Returns the dictionary of headers required by Kalshi v2 API."""
        timestamp_str, signature = self.sign_request(
            method=method,
            path_or_url=path_or_url,
            timestamp_ms=timestamp_ms,
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_str,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    @staticmethod
    def verify_signature(
        public_key: PublicKeyType,
        signature_b64: str,
        timestamp_str: str,
        method: str,
        path_without_query: str,
    ) -> bool:
        """Verifies an RSA-PSS or Ed25519 signature against the expected message."""
        try:
            raw_signature = base64.b64decode(signature_b64)
            message = f"{timestamp_str}{method.upper().strip()}{path_without_query}".encode("utf-8")
            if isinstance(public_key, Ed25519PublicKey):
                public_key.verify(raw_signature, message)
            else:
                public_key.verify(
                    raw_signature,
                    message,
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH,
                    ),
                    hashes.SHA256(),
                )
            return True
        except Exception:
            return False
