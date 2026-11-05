#!/usr/bin/env python3
"""
Wielowątkowy serwer autoryzacji kart RFID obsługujący wiele bram i domofonów.
Odpowiada za:
  - Nasłuchiwanie połączeń TCP od jednostek bramowych w osobnych wątkach.
  - Automatyczne uwzględnianie zmian w pliku konfiguracyjnym (hot-reload kart i kluczy .pem).
  - Deszyfrowanie ramek AES-256-GCM kluczem przypisanym do danej bramy (lub domyślnym).
  - Weryfikację znacznika czasu (ochrona przed replay attack) oraz uprawnień karty RFID.
  - Odsyłanie zaszyfrowanego werdyktu wraz z odebranym numerem losowym (seed).
"""

import atexit
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
from datetime import datetime

import psutil

from config import auth_manager
from common.crypto import (
    ACCESS_DENIED_CODE,
    ACCESS_GRANTED_CODE,
    ENCRYPTED_PACKET_SIZE,
    parse_and_validate_frame
)

# Konfiguracja loggera na podstawie wczytanych ustawień
logging.basicConfig(
    format="%(asctime)-15s %(levelname)s: %(message)s",
    filename=auth_manager.config.log_path,
    level=logging.INFO
)
logger = logging.getLogger("acsrv_log")


class GateAuthRequestHandler(socketserver.BaseRequestHandler):
    """
    Obsługuje pojedyncze połączenie TCP z bramy lub domofonu w osobnym wątku.
    Każde zapytanie przechodzi przez szybką weryfikację aktualności konfiguracji (st_mtime),
    dzięki czemu nowe karty w pliku JSON działają natychmiast bez restartu serwera.
    """

    def handle(self) -> None:
        client_ip = self.client_address[0]

        # Pobranie konfiguracji bramy i jej szyfratora (z automatycznym sprawdzeniem hot-reload)
        try:
            gate_cfg, cipher = auth_manager.get_gate_and_cipher(client_ip)
        except Exception as err:
            logger.error(f"Błąd przeładowania konfiguracji lub klucza .pem podczas żądania: {err}")
            return

        if not gate_cfg or not gate_cfg.enabled or cipher is None:
            logger.warning(f"Odrzucono połączenie z nieautoryzowanego lub wyłączonego IP: {client_ip}")
            return

        # Ustawienie timeoutu na gnieździe klienta, aby nie blokować wątku
        self.request.settimeout(auth_manager.config.client_timeout_sec)

        try:
            data = self.request.recv(1024)
            if not data:
                return

            # 1. Walidacja rozmiaru zaszyfrowanego pakietu (62 bajty dla AES-256-GCM)
            if len(data) != ENCRYPTED_PACKET_SIZE:
                logger.warning(
                    f"[{gate_cfg.name} ({client_ip})] Odrzucono pakiet o nieprawidłowej długości: "
                    f"{len(data)} B (oczekiwano {ENCRYPTED_PACKET_SIZE} B)."
                )
                return

            # 2. Deszyfrowanie i weryfikacja sumy kontrolnej GCM
            decrypted = cipher.decrypt_payload(data)
            if not decrypted:
                logger.warning(
                    f"[{gate_cfg.name} ({client_ip})] Błąd deszyfrowania AES-256-GCM "
                    f"(niezgodny klucz .pem lub naruszona integralność danych)!"
                )
                return

            # 3. Walidacja struktury ramki i różnicy czasu między bramą a serwerem
            parsed = parse_and_validate_frame(
                decrypted,
                max_time_drift_sec=auth_manager.config.max_time_drift_sec
            )
            if not parsed:
                logger.warning(
                    f"[{gate_cfg.name} ({client_ip})] Odrzucono ramkę: przekroczona różnica czasu "
                    f"lub uszkodzony format znacznika czasu."
                )
                return

            seed_hex, tag_hex = parsed

            # 4. Sprawdzenie uprawnień karty RFID (w pamięci RAM, czas O(1))
            if auth_manager.is_tag_authorized(tag_hex, gate_cfg):
                logger.info(f"[{gate_cfg.name} ({client_ip})] Przyznano dostęp dla karty: {tag_hex}")
                status_code = ACCESS_GRANTED_CODE
            else:
                logger.info(f"[{gate_cfg.name} ({client_ip})] Odmowa dostępu dla karty: {tag_hex}")
                status_code = ACCESS_DENIED_CODE

            # 5. Budowa, zaszyfrowanie i odesłanie odpowiedzi: [8B SEED][12B STATUS][14B CZAS]
            now_str = datetime.now().strftime("%Y%m%d%H%M%S")
            response_plain = f"{seed_hex}{status_code}{now_str}"
            encrypted_response = cipher.encrypt_payload(response_plain)

            self.request.sendall(encrypted_response)

        except socket.timeout:
            logger.warning(f"[{gate_cfg.name} ({client_ip})] Przekroczono czas oczekiwania (timeout).")
        except OSError as err:
            logger.error(f"[{gate_cfg.name} ({client_ip})] Błąd gniazda sieciowego: {err}")
        except Exception as err:
            logger.error(f"[{gate_cfg.name} ({client_ip})] Nieoczekiwany błąd obsługi żądania: {err}")


class ThreadedAuthTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """
    Wielowątkowy serwer TCP.
    Flaga allow_reuse_address pozwala na natychmiastowe ponowne zbindowanie portu po restarcie,
    a daemon_threads gwarantuje zamknięcie wątków roboczych przy wyjściu z programu.
    """
    allow_reuse_address = True
    daemon_threads = True


def is_already_running(script_name: str) -> bool:
    """Sprawdza, czy inna instancja serwera autoryzacji jest już uruchomiona."""
    current_pid = os.getpid()
    for proc in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if len(cmdline) > 1 and script_name in cmdline[1] and proc.info["pid"] != current_pid:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def cleanup() -> None:
    """Zamyka system logowania przy zakończeniu procesu."""
    logger.info("Serwer autoryzacji zakończył pracę.")
    logging.shutdown()


def main() -> None:
    """Główna funkcja inicjalizująca i uruchamiająca serwer TCP."""
    if is_already_running(os.path.basename(__file__)):
        print("Błąd: Inna instancja serwera autoryzacji już działa. Zamykanie.")
        sys.exit(1)

    atexit.register(cleanup)

    cfg = auth_manager.config
    bind_address = (cfg.bind_host, cfg.bind_port)

    try:
        # noinspection bad-argument-type
        server = ThreadedAuthTCPServer(bind_address, GateAuthRequestHandler)
    except OSError as err:
        logger.error(f"Nie można otworzyć portu TCP {bind_address[0]}:{bind_address[1]}: {err}")
        sys.exit(1)

    def shutdown_handler(signum, frame) -> None:
        """Bezpiecznie zatrzymuje pętlę serve_forever po otrzymaniu SIGINT/SIGTERM."""
        logger.info(f"Odebrano sygnał zakończenia ({signum}). Zatrzymywanie serwera...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    logger.info(
        f"Serwer autoryzacji RFID wystartował na {bind_address[0]}:{bind_address[1]} | "
        f"Zarejestrowanych bram: {len(cfg.gates)} | "
        f"Kart globalnych w bazie: {len(cfg.allowed_tags)}"
    )

    # Blokująca pętla główna serwera (przerwana dopiero przez server.shutdown())
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
