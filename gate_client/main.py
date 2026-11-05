#!/usr/bin/env python3
"""
Główny punkt wejścia klienta bramy.
Uruchamia w osobnych wątkach:
  1. Serwer HTTP (Flask) do obsługi decyzji z telefonu.
  2. Czytnik kart RFID (UART + klient TCP AES-256-GCM).
  3. Pętlę główną (przycisk dzwonka + nasłuch DTMF Linphone).
"""

import atexit
import logging
import os
import signal
import sys
import threading

import psutil

from config import settings
from hardware import cleanup_gpio, init_gpio
from lin_phone import run_doorbell_controller
from rfid_reader import run_rfid_reader_loop
from mailbox_monitor import run_mailbox_monitor_loop
from server import run_http_server

logging.basicConfig(
    format="%(asctime)-15s %(levelname)s: %(message)s",
    filename=settings.app_log_path,
    level=logging.INFO
)
logger = logging.getLogger("vbram_log")

log_file_handle = None


def is_already_running(script_name: str) -> bool:
    """Weryfikuje, czy inna kopia procesu już działa."""
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
    """Zwalnia linie GPIO i zamyka plik logów przy wyjściu."""
    logger.info("Zamykanie systemu bramy - zwalnianie zasobów...")
    cleanup_gpio()
    if log_file_handle and not log_file_handle.closed:
        log_file_handle.close()
    logging.shutdown()


# noinspection unused-parameter
def signal_handler(signum, frame) -> None:
    sys.exit(0)


def main() -> None:
    global log_file_handle

    if is_already_running(os.path.basename(__file__)):
        print("Inna instancja programu już działa. Zamykanie.")
        sys.exit(1)

    atexit.register(cleanup)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    init_gpio()

    if not os.path.isfile(settings.sip.linphone_log_path):
        logger.error(f"Brak pliku logu Linphone: {settings.sip.linphone_log_path}")
        sys.exit(1)

    log_file_handle = open(settings.sip.linphone_log_path, "r")
    log_file_handle.seek(0, os.SEEK_END)

    # 1. Wątek serwera HTTP (decyzje z powiadomień i aplikacji)
    threading.Thread(
        target=run_http_server,
        name="HTTPServerThread",
        daemon=True
    ).start()

    # 2. Wątek czytnika kart RFID
    threading.Thread(
        target=run_rfid_reader_loop,
        name="RFIDReaderThread",
        daemon=True
    ).start()

    # 3. Wątek skrzynki na listy
    threading.Thread(
        target=run_mailbox_monitor_loop,
        name="MailboxThread", daemon=True
    ).start()

    logger.info("Wszystkie moduły bramy zostały uruchomione.")

    # 3. Pętla główna przycisku i DTMF
    run_doorbell_controller(log_file_handle)


if __name__ == "__main__":
    main()
