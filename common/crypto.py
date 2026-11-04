"""
Moduł kryptograficzny obsługujący szyfrowanie AES-256-GCM z kluczami
wczytywanymi z plików .pem oraz formatowanie
i weryfikację 34-bajtowych ramek protokołu kontroli dostępu.
"""

import base64
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
from Cryptodome.Cipher import AES
from Cryptodome.Random import get_random_bytes

# Stałe protokołu (zgodne ze starszą wersją)
ACCESS_GRANTED_CODE = "677265674147"
ACCESS_DENIED_CODE = "C7F2E5E7C1C4"

NONCE_SIZE = 12
MAC_TAG_SIZE = 16
PLAINTEXT_LENGTH = 34
ENCRYPTED_PACKET_SIZE = NONCE_SIZE + PLAINTEXT_LENGTH + MAC_TAG_SIZE  # 62 bajty


def load_aes_key_from_pem(pem_path: Path) -> bytes:
    """
    Wczytuje 32-bajtowy klucz AES-256 z pliku .pem.
    Obsługuje pliki z nagłówkami PEM (Base64), czysty ciąg HEX (64 znaki) lub surowe 32 bajty.
    """
    if not pem_path.is_file():
        raise FileNotFoundError(f"Brak pliku klucza AES PEM: {pem_path}")

    raw_bytes = pem_path.read_bytes().strip()
    if len(raw_bytes) == 32:
        return raw_bytes

    text = raw_bytes.decode("utf-8", errors="ignore")
    # Odfiltrowanie nagłówków -----BEGIN ...----- i -----END ...-----
    lines = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("-----")
    ]
    payload = "".join(lines)

    # Próba 1: 64-znakowy zapis szesnastkowy
    if len(payload) == 64:
        try:
            return bytes.fromhex(payload)
        except ValueError:
            pass

    # Próba 2: standardowe kodowanie Base64 wewnątrz PEM
    try:
        decoded = base64.b64decode(payload)
        if len(decoded) == 32:
            return decoded
    except Exception as err:
        raise ValueError(f"Nieprawidłowy format pliku PEM ({pem_path}): {err}") from err

    raise ValueError(
        f"Klucz w pliku {pem_path} po zdekodowaniu nie ma wymaganych 32 bajtów (256 bitów)."
    )


class AESCipher:
    """Klasa realizująca uwierzytelnione szyfrowanie symetryczne AES-256-GCM."""

    def __init__(self, key_bytes: bytes) -> None:
        if len(key_bytes) != 32:
            raise ValueError("Klucz AES-256 musi mieć dokładnie 32 bajty.")
        self._key = key_bytes

    @classmethod
    def from_pem_file(cls, pem_path: str | Path) -> "AESCipher":
        """Tworzy instancję szyfratora na podstawie ścieżki do pliku .pem."""
        return cls(load_aes_key_from_pem(Path(pem_path)))

    def encrypt_payload(self, plaintext: str) -> bytes:
        """Szyfruje 34-znakowy ciąg do 62-bajtowej ramki [12B nonce][34B cipher][16B tag]."""
        raw_data = plaintext.encode("ascii")
        nonce = get_random_bytes(NONCE_SIZE)
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce, mac_len=MAC_TAG_SIZE)
        ciphertext, tag = cipher.encrypt_and_digest(raw_data)
        return nonce + ciphertext + tag

    def decrypt_payload(self, packet: bytes) -> Optional[str]:
        """Weryfikuje autentyczność GCM i deszyfruje 62-bajtowy pakiet."""
        if len(packet) != ENCRYPTED_PACKET_SIZE:
            return None

        nonce = packet[:NONCE_SIZE]
        ciphertext = packet[NONCE_SIZE:-MAC_TAG_SIZE]
        tag = packet[-MAC_TAG_SIZE:]

        try:
            cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce, mac_len=MAC_TAG_SIZE)
            decrypted = cipher.decrypt_and_verify(ciphertext, tag)
            return decrypted.decode("ascii")
        except (ValueError, KeyError, UnicodeDecodeError):
            return None


def parse_and_validate_frame(
    frame: str,
    max_time_drift_sec: float = 12.0
) -> Optional[Tuple[str, str]]:
    """
    Rozbija 34-znakową ramkę [8B SEED][12B DATA][14B TIMESTAMP] i weryfikuje czas.
    """
    if len(frame) != PLAINTEXT_LENGTH:
        return None

    seed_hex = frame[0:8]
    middle_field = frame[8:20]
    timestamp_str = frame[20:34]

    if not timestamp_str.isdigit():
        return None

    try:
        packet_time = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
    except ValueError:
        return None

    time_diff = abs((datetime.now() - packet_time).total_seconds())
    if time_diff > max_time_drift_sec:
        return None

    return seed_hex, middle_field
