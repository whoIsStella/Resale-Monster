"""Authenticated encryption for marketplace session state at rest.

Uses Fernet (AES-128-CBC + HMAC) from the ``cryptography`` package. The plaintext
is opaque session bytes (Playwright storage state or profile archive). Only the
session broker/adapter boundary ever decrypts; agents never receive plaintext.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SessionCipherError(ValueError):
    pass


class SessionCipher:
    def __init__(self, key: str, *, key_id: str = "primary") -> None:
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as error:
            raise SessionCipherError("invalid session encryption key") from error
        self.key_id = key_id

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: bytes) -> str:
        return self._fernet.encrypt(plaintext).decode()

    def decrypt(self, ciphertext: str) -> bytes:
        try:
            return self._fernet.decrypt(ciphertext.encode())
        except InvalidToken as error:
            raise SessionCipherError("session ciphertext failed authentication") from error
