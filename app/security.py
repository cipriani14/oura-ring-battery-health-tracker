import os
import base64
import hashlib
import logging
from typing import Optional

logger = logging.getLogger("oura_tracker.security")

# Retrieve or generate secret key for database token encryption
_SECRET = os.getenv("SECRET_KEY", os.getenv("ENCRYPTION_KEY", "oura_tracker_default_secret_key_2026"))

def _get_key_bytes() -> bytes:
    return hashlib.sha256(_SECRET.encode("utf-8")).digest()

def mask_token(token: Optional[str]) -> str:
    """Safely masks API token for logging and UI display (e.g. 'oura_...4a2b')."""
    if not token:
        return "Not Configured"
    token_str = str(token).strip()
    if len(token_str) <= 8:
        return "********"
    return f"{token_str[:4]}...{token_str[-4:]}"

def encrypt_token(token: Optional[str]) -> str:
    """Encrypts Personal Access Token for secure storage at rest in SQLite."""
    if not token:
        return ""
    token_str = str(token).strip()
    if token_str.startswith("enc_v1:"):
        return token_str  # Already encrypted
        
    try:
        # Simple & portable XOR+Base64 cipher using SHA256 key stream
        key = _get_key_bytes()
        token_bytes = token_str.encode("utf-8")
        encrypted = bytearray()
        for i, b in enumerate(token_bytes):
            encrypted.append(b ^ key[i % len(key)])
        encoded = base64.urlsafe_b64encode(bytes(encrypted)).decode("utf-8")
        return f"enc_v1:{encoded}"
    except Exception as e:
        logger.error(f"Error encrypting token: {e}")
        return token_str

def decrypt_token(encrypted_token: Optional[str]) -> str:
    """Decrypts stored Personal Access Token for active Oura API calls."""
    if not encrypted_token:
        return ""
    token_str = str(encrypted_token).strip()
    if not token_str.startswith("enc_v1:"):
        return token_str  # Unencrypted legacy token
        
    try:
        raw_b64 = token_str.replace("enc_v1:", "")
        encrypted_bytes = base64.urlsafe_b64decode(raw_b64.encode("utf-8"))
        key = _get_key_bytes()
        decrypted = bytearray()
        for i, b in enumerate(encrypted_bytes):
            decrypted.append(b ^ key[i % len(key)])
        return decrypted.decode("utf-8")
    except Exception as e:
        logger.error(f"Error decrypting token: {e}")
        return token_str
