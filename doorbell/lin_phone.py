"""
Główny moduł kontrolera domofonu.
Odpowiada za:
  - Odpytywanie statusu i sterowanie sesjami SIP w linphonecsh.
  - Śledzenie logów linphone w tle z ekstrakcją DTMF przez Regex.
  - Cykliczne sprawdzanie stanu fizycznego przycisku dzwonka (z debouncingiem).
"""

import logging
import os
import re
import subprocess
import threading
import time
from collections import deque

# noinspection unused-imports
from config import (
    DEFAULT_DIAL_NUMBER,
    SOUND_CONFIRM,
    SOUND_GATE_OPEN,
    SOUND_SILENCE
)
from hardware import (
    open_gate,
    play_audio,
    read_button_state,
    set_led_state
)
from notifier import send_doorbell_notification

logger = logging.getLogger('vbram_log')

# Wzorce Regex
DTMF_RECEIVE_PATTERN = re.compile(r"Receiving dtmf\s+([0-9A-D*#])", re.IGNORECASE)
MAGIC_SEQUENCE_REGEX = re.compile(r"9#0$")

# Ograniczenia czasowe DTMF
MAX_INTER_DIGIT_INTERVAL_SEC = 3.0

# Stan DTMF chroniony blokadą wątkową
_dtmf_lock = threading.Lock()
_dtmf_history = deque(maxlen=8)
_last_dtmf_timestamp = 0.0


# =====================================================================
# Integracja z linphonecsh CLI
# =====================================================================

def is_linphone_on_hook() -> bool:
    """Sprawdza, czy linphone jest w stanie spoczynku (brak aktywnego połączenia)."""
    try:
        result = subprocess.run(
            ['/usr/bin/linphonecsh', 'status', 'hook'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
        return 'hook=on-hook' in result.stdout
    except Exception as err:
        logger.error(f"Błąd odpytania linphonecsh: {err}")
        return False


def set_linphone_mute(mute: bool) -> None:
    """Wycisza (True) lub odcisza (False) mikrofon w linphonecsh."""
    command = 'mute' if mute else 'unmute'
    subprocess.run(['/usr/bin/linphonecsh', 'generic', command], check=False)


def dial_sip_number(number: str) -> None:
    """Inicjuje wychodzące połączenie SIP pod wskazany numer."""
    logger.info(f"Inicjowanie połączenia SIP na numer: {number}")
    subprocess.run(['/usr/bin/linphonecsh', 'dial', number], check=False)


# =====================================================================
# Bufor i analiza kodów DTMF
# =====================================================================

def process_dtmf_line(line: str) -> None:
    """Parsuje linię logu za pomocą regexa i dodaje znak do bufora."""
    global _last_dtmf_timestamp
    match = DTMF_RECEIVE_PATTERN.search(line)
    if not match:
        return

    char = match.group(1)
    now = time.time()

    with _dtmf_lock:
        if (now - _last_dtmf_timestamp) > MAX_INTER_DIGIT_INTERVAL_SEC:
            _dtmf_history.clear()

        _last_dtmf_timestamp = now
        _dtmf_history.append(char)
        logger.info(f"Zarejestrowano ton DTMF: {char} (bufor: {''.join(_dtmf_history)})")


def check_magic_dtmf_sequence() -> bool:
    """Weryfikuje regexem, czy bufor kończy się sekwencją otwarcia (9#0)."""
    with _dtmf_lock:
        if not _dtmf_history:
            return False

        if (time.time() - _last_dtmf_timestamp) > MAX_INTER_DIGIT_INTERVAL_SEC:
            _dtmf_history.clear()
            return False

        current_sequence = "".join(_dtmf_history)
        return bool(MAGIC_SEQUENCE_REGEX.search(current_sequence))


def clear_dtmf_buffer() -> None:
    """Czyści bufor znaków DTMF po skutecznym otwarciu bramy."""
    with _dtmf_lock:
        _dtmf_history.clear()


def follow_linphone_log(file_handle) -> None:
    """Wątek w tle śledzący plik logów linphonec (odpowiednik tail -f)."""
    interval = 0.1
    while True:
        current_pos = file_handle.tell()
        end_pos = file_handle.seek(0, os.SEEK_END)

        if (end_pos + 1) < current_pos:
            current_pos = end_pos
        else:
            file_handle.seek(current_pos, os.SEEK_SET)

        line = file_handle.readline()
        if not line:
            time.sleep(interval)
            file_handle.seek(current_pos, os.SEEK_SET)
            continue

        if "ortp-message-Receiving dtmf" in line:
            clean_line = str(line).lstrip("b'").replace("'", "")
            process_dtmf_line(clean_line)


# =====================================================================
# Pętla główna kontrolera
# =====================================================================

def run_doorbell_controller(log_file_handle) -> None:
    """
    Pętla główna zdarzeń:
      - Monitoruje stan GPIO przycisku dzwonka (z programowym debouncingiem).
      - Zapobiega powtórnemu wysyłaniu powiadomień przy trzymanym przycisku (zatrzask).
      - Weryfikuje sekwencje DTMF podczas aktywnej sesji SIP.
    """
    # Start wątku czytającego DTMF
    tail_thread = threading.Thread(
        target=follow_linphone_log,
        args=(log_file_handle,),
        name='LinphoneLogTail',
        daemon=True
    )
    tail_thread.start()

    previous_button_state = '0'
    # Flaga zatrzasku: True, gdy powiadomienie zostało już wysłane dla bieżącego wciśnięcia
    is_button_latched = False
    last_loop_time = time.time()

    set_linphone_mute(False)

    while True:
        current_button_state = read_button_state()

        # Detekcja stabilnego wciśnięcia przycisku dzwonka (stan wysoki)
        if current_button_state == '1' and previous_button_state == '1':
            if not is_button_latched:
                logger.info("Fizyczny przycisk dzwonka wciśnięty.")
                notification_sent = send_doorbell_notification()

                # Zatrzaskujemy stan, tylko jeśli powiadomienie przeszło,
                # aby w razie błędu sieci ponowić próbę przy kolejnym cyklu
                if notification_sent:
                    is_button_latched = True

        # Zwolnienie przycisku (stan niski) – resetujemy zatrzask
        elif current_button_state == '0' and previous_button_state == '0':
            if is_button_latched:
                logger.info("Przycisk dzwonka został zwolniony.")
                is_button_latched = False

        # Weryfikacja kodu DTMF podczas trwającej rozmowy telefonicznej
        if check_magic_dtmf_sequence():
            clear_dtmf_buffer()
            if not is_linphone_on_hook():
                logger.info("Odebrano poprawny kod DTMF podczas rozmowy. Otwieranie bramy...")
                play_audio(SOUND_SILENCE)
                play_audio(SOUND_CONFIRM)

                set_linphone_mute(True)
                open_gate()
                play_audio(SOUND_SILENCE)
                play_audio(SOUND_GATE_OPEN)
                set_linphone_mute(False)
            else:
                logger.warning("Wykryto kod DTMF, ale brak aktywnej sesji SIP (on-hook).")

        # Programowy filtr drgań styków (debouncing ~60 ms)
        while (last_loop_time + 0.06) > time.time():
            time.sleep(0.015)
        last_loop_time = time.time()

        # Dioda kontrolna LED — krótkie mignięcie co sekundę
        if (last_loop_time - int(last_loop_time)) < 0.07:
            set_led_state(True)
        else:
            set_led_state(False)

        previous_button_state = current_button_state
