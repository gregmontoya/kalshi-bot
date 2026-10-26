"""Unit tests for Phase 1 RSA-PSS Authentication Signer."""

from cryptography.hazmat.primitives.asymmetric import rsa
from src.core.auth import KalshiAuthSigner


def test_rsa_key_generation_and_export():
    private_key, public_key = KalshiAuthSigner.generate_key_pair(2048)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert isinstance(public_key, rsa.RSAPublicKey)

    pem_private = KalshiAuthSigner.export_private_key_pem(private_key)
    pem_public = KalshiAuthSigner.export_public_key_pem(public_key)

    assert "-----BEGIN PRIVATE KEY-----" in pem_private
    assert "-----BEGIN PUBLIC KEY-----" in pem_public


def test_rsa_pss_signing_and_verification():
    private_key, public_key = KalshiAuthSigner.generate_key_pair(2048)
    signer = KalshiAuthSigner(key_id="test-key-phase1", private_key=private_key)

    timestamp_ms = 1714521600000
    method = "GET"
    path = "/trade-api/v2/portfolio/balance"

    timestamp_str, signature = signer.sign_request(
        method=method,
        path_or_url=path,
        timestamp_ms=timestamp_ms,
    )

    assert timestamp_str == "1714521600000"
    assert len(signature) > 64

    # Verify signature
    valid = KalshiAuthSigner.verify_signature(
        public_key=public_key,
        signature_b64=signature,
        timestamp_str=timestamp_str,
        method=method,
        path_without_query=path,
    )
    assert valid is True


def test_path_without_query_extraction():
    url = "https://external-api.kalshi.com/trade-api/v2/markets?limit=100&status=open"
    extracted = KalshiAuthSigner.extract_path_without_query(url)
    assert extracted == "/trade-api/v2/markets"

    url2 = "/trade-api/v2/markets/DEMO/orderbook?depth=10"
    extracted2 = KalshiAuthSigner.extract_path_without_query(url2)
    assert extracted2 == "/trade-api/v2/markets/DEMO/orderbook"


def test_auth_headers_generation():
    private_key, _ = KalshiAuthSigner.generate_key_pair(2048)
    signer = KalshiAuthSigner(key_id="phase1-demo-key", private_key=private_key)

    headers = signer.get_auth_headers(
        method="POST",
        path_or_url="/trade-api/v2/portfolio/orders",
    )

    assert headers["KALSHI-ACCESS-KEY"] == "phase1-demo-key"
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert len(headers["KALSHI-ACCESS-SIGNATURE"]) > 32
