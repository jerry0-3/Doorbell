"""
Moduł obsługi czytnika kart RFID na porcie szeregowym (UART)
oraz weryfikacji kart na serwerze TCP z użyciem klucza AES-256-GCM z pliku .pem.
"""

import logging
import secrets
import socket
import time
from datetime import datetime
from typing import Optional

import serial

from common.crypto import (
    ACCESS_GRANTED_CODE,
    AESCipher,
    ENCRYPTED_PACKET_SIZE,
    parse_and_validate_frame
)
from gate_client.config import settings
from gate_client.hardware import (
    set_rfid_waiting_state,
    signal_rfid_accepted,
    signal_rfid_rejected
)
from gate_client.lin_phone import set_linphone_mute

logger = logging.getLogger("vbram_log")

STX_BYTE = 0x02
ETX_BYTE = 0x03
RFID_FRAME_LENGTH = 14


def extract_tag_from_frame(raw_frame: bytes) -> Optional[str]:
    """Waliduje 14-bajtową ramkę UART (0x02 + 12 znaków ASCII HEX + 0x03)."""
    if len(raw_frame) < RFID_FRAME_LENGTH:
        return None

    start_idx = raw_frame.find(bytes([STX_BYTE]))
    if start_idx == -1 or len(raw_frame) < start_idx + RFID_FRAME_LENGTH:
        return None

    frame = raw_frame[start_idx:start_idx + RFID_FRAME_LENGTH]
    if frame[0] != STX_BYTE or frame[13] != ETX_BYTE:
        return None

    try:
        tag_hex = frame[1:13].decode("ascii").upper()
        int(tag_hex, 16)
        return tag_hex
    except ValueError:
        return None


def verify_tag_with_server(tag_hex: str, cipher: AESCipher) -> bool:
    """Wysyła zaszyfrowane zapytanie z numerem karty do serwera i sprawdza werdykt."""
    rfid_cfg = settings.rfid
    seed_hex = f"{secrets.randbits(24):08X}"
    timestamp_str = datetime.now().strftime("%Y%m%d%H%M%S")

    plaintext = f"{seed_hex}{tag_hex}{timestamp_str}"
    encrypted_request = cipher.encrypt_payload(plaintext)

    try:
        with socket.create_connection(
                (rfid_cfg.auth_server_ip, rfid_cfg.auth_server_port),
                timeout=rfid_cfg.socket_timeout_sec
        ) as sock:
            sock.sendall(encrypted_request)
            response_bytes = sock.recv(1024)
    except OSError as err:
        logger.error(f"Błąd połączenia z serwerem RFID ({rfid_cfg.auth_server_ip}): {err}")
        return False

    if len(response_bytes) != ENCRYPTED_PACKET_SIZE:
        logger.warning(f"Nieprawidłowa długość odpowiedzi z serwera ({len(response_bytes)} B).")
        return False

    decrypted_response = cipher.decrypt_payload(response_bytes)
    if not decrypted_response:
        logger.warning("Błąd deszyfrowania odpowiedzi z serwera (sprawdź plik .pem).")
        return False

    parsed = parse_and_validate_frame(
        decrypted_response,
        max_time_drift_sec=rfid_cfg.max_time_drift_sec
    )
    if not parsed:
        logger.warning("Odrzucono odpowiedź serwera: przekroczona różnica czasu lub zła ramka.")
        return False

    resp_seed, status_code = parsed
    if resp_seed != seed_hex:
        logger.warning("Odrzucono odpowiedź serwera: niezgodny seed.")
        return False

    return status_code == ACCESS_GRANTED_CODE


def run_rfid_reader_loop() -> None:
    """Pętla główna wątku czytnika kart RFID."""
    rfid_cfg = settings.rfid
    if not rfid_cfg.enabled:
        logger.info("Moduł czytnika RFID jest wyłączony w konfiguracji.")
        return

    try:
        cipher = AESCipher.from_pem_file(rfid_cfg.aes_key_pem_path)
    except Exception as err:
        logger.error(f"Nie udało się wczytać klucza AES z pliku PEM: {err}")
        return

    while True:
        try:
            with serial.Serial(
                    port=rfid_cfg.serial_port,
                    baudrate=rfid_cfg.baudrate,
                    timeout=1.0
            ) as ser:
                logger.info(f"Czytnik RFID gotowy na porcie {ser.name}.")

                while True:
                    first_byte = ser.read(1)
                    if not first_byte:
                        continue

                    time.sleep(0.04)
                    raw_data = first_byte + ser.read(ser.in_waiting)
                    tag_hex = extract_tag_from_frame(raw_data)

                    if tag_hex:
                        logger.info(f"Odczytano kartę RFID: {tag_hex}")
                        # 1. Dioda niebieska przechodzi w stan ciągłego świecenia
                        set_rfid_waiting_state()

                        # 2. Odpytanie serwera i odpowiednia sygnalizacja (5s)
                        if verify_tag_with_server(tag_hex, cipher):
                            logger.info(f"Karta {tag_hex} zaakceptowana. Otwieranie bramy.")
                            set_linphone_mute(True)
                            signal_rfid_accepted()
                            set_linphone_mute(False)
                        else:
                            logger.info(f"Karta {tag_hex} odrzucona.")
                            signal_rfid_rejected()

                    ser.reset_input_buffer()
                    time.sleep(0.3)

        except serial.SerialException as err:
            logger.error(f"Błąd portu szeregowego ({rfid_cfg.serial_port}): {err}. Ponowienie za 10s...")
            time.sleep(10.0)
        except Exception as err:
            logger.error(f"Nieoczekiwany błąd w wątku RFID: {err}")
            time.sleep(5.0)
