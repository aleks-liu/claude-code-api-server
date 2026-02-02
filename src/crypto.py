"""
Cryptographic utilities for Claude Code API Server.

Provides RSA encryption for admin bootstrap token protection.
"""

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def encrypt_token(token: str, b64_public_key: str) -> str:
    """
    Encrypt a token with an RSA public key.

    Uses RSA OAEP with SHA-256 for secure asymmetric encryption.
    The encrypted token can only be decrypted by the holder of
    the corresponding private key.

    Args:
        token: The plaintext token to encrypt
        b64_public_key: Base64-encoded RSA public key in PEM format

    Returns:
        Base64-encoded ciphertext

    Raises:
        ValueError: If the public key is invalid or encryption fails
    """
    try:
        pem_bytes = base64.b64decode(b64_public_key)
    except Exception as e:
        raise ValueError(f"Invalid base64 encoding for public key: {e}") from e

    try:
        public_key = serialization.load_pem_public_key(pem_bytes)
    except Exception as e:
        raise ValueError(f"Invalid RSA public key format: {e}") from e

    try:
        ciphertext = public_key.encrypt(
            token.encode("utf-8"),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
    except Exception as e:
        raise ValueError(f"Encryption failed: {e}") from e

    return base64.b64encode(ciphertext).decode("ascii")
