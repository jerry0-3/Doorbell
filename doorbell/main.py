#!/usr/bin/env python3
"""
Główny punkt wejścia do systemu domofonu.
Uruchamia serwer HTTP (Waitress) w tle oraz główną pętlę zdarzeń Linphone/GPIO.
"""

import atexit
import logging
import os
import signal
import sys
import threading

import psutil

from config import APP_LOG_PATH, LINPHONE_LOG_PATH
from hardware import set_gate_relay, set_led_state
from lin_phone import run_doorbell_controller
from server import run_http_server

# Konfiguracja głównego loggera aplikacji
logging.basicConfig(
    format='%(asctime)-15s %(levelname)s: %(message)s',
    filename=APP_LOG_PATH,
    level=logging.INFO
)
logger = logging.getLogger('vbram_log')

log_file_handle = None


def is_already_running(script_name: str) -> bool:
    """Sprawdza, czy inna instancja programu jest już uruchomiona w systemie."""
    current_pid = os.getpid()
    for proc in psutil.process_iter(['name', 'cmdline', 'pid']):
        try:
            cmdline = proc.info.get('cmdline') or []
            if len(cmdline) > 1 and script_name in cmdline[1] and proc.info['pid'] != current_pid:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def cleanup() -> None:
    """Przywraca bezpieczny stan przekaźników i zamyka otwarte deskryptory."""
    logger.info("Zamykanie aplikacji - czyszczenie zasobów...")
    # noinspection unresolved-references
    if log_file_handle and not log_file_handle.closed:
        # noinspection unresolved-references
        log_file_handle.close()
    set_gate_relay(False)
    set_led_state(False)
    logging.shutdown()


# noinspection unused-parameter
def signal_handler(signum, frame) -> None:
    """Obsługuje przechwycenie sygnałów SIGINT i SIGTERM."""
    sys.exit(0)


def main() -> None:
    global log_file_handle

    if is_already_running(os.path.basename(__file__)):
        print("Błąd: Inna instancja programu już działa. Zamykanie.")
        sys.exit(1)

    atexit.register(cleanup)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Inicjalizacja stanu sprzętu
    set_gate_relay(False)
    set_led_state(True)

    # Otwarcie pliku logów linphone
    if not os.path.isfile(LINPHONE_LOG_PATH):
        logger.error(f"Plik logu '{LINPHONE_LOG_PATH}' nie istnieje. Włącz logowanie w linphonec.")
        sys.exit(1)

    try:
        log_file_handle = open(LINPHONE_LOG_PATH, 'r')
        log_file_handle.seek(0, os.SEEK_END)
    except IOError as err:
        logger.error(f"Nie udało się otworzyć logu Linphone: {err}")
        sys.exit(1)

    # Uruchomienie serwera HTTP (Waitress) w dedykowanym wątku demona
    http_thread = threading.Thread(
        target=run_http_server,
        name='HTTPServerThread',
        daemon=True
    )
    http_thread.start()

    logger.info("System domofonu został uruchomiony pomyślnie.")

    # Uruchomienie pętli głównej w wątku głównym
    run_doorbell_controller(log_file_handle)


if __name__ == '__main__':
    main()
