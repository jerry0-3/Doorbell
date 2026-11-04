"""
Moduł monitorujący stan opcjonalnej skrzynki na listy.
Wykrywa przejście ze stanu PUSTA -> COŚ W NIEJ JEST, zapala diodę skrzynki
i wysyła powiadomienie. Po opróżnieniu skrzynki gasi diodę.
"""

import logging
import time

from gate_client.config import settings
from gate_client.hardware import read_mailbox_sensor, set_mailbox_led
from gate_client.notifier import send_mailbox_notification

logger = logging.getLogger("vbram_log")


def run_mailbox_monitor_loop() -> None:
    """
    Pętla wątku monitorującego czujnik skrzynki na listy.
    Jeśli moduł jest wyłączony w konfiguracji, wątek natychmiast kończy pracę.
    """
    mb_cfg = settings.mailbox
    if not mb_cfg.enabled:
        logger.info("Moduł skrzynki na listy jest wyłączony – pomijanie wątku.")
        return

    logger.info(
        f"Uruchomiono monitor skrzynki na listy (GPIO czujnika={mb_cfg.line_sensor}, "
        f"GPIO diody={mb_cfg.line_led})."
    )

    # Stan początkowy przy starcie systemu
    has_mail_confirmed = read_mailbox_sensor()
    set_mailbox_led(has_mail_confirmed)

    candidate_state = has_mail_confirmed
    state_change_timestamp = time.time()
    poll_interval = 0.25  # Odpytywanie 4 razy na sekundę (zerowe obciążenie CPU)

    while True:
        current_reading = read_mailbox_sensor()
        now = time.time()

        # Jeśli odczyt różni się od ostatniego kandydata, resetujemy licznik stabilności
        if current_reading != candidate_state:
            candidate_state = current_reading
            state_change_timestamp = now

        # Jeśli nowy stan utrzymuje się stabilnie przez czas debounce_sec (np. 2.0 s)
        elif (now - state_change_timestamp) >= mb_cfg.debounce_sec:
            if candidate_state != has_mail_confirmed:
                has_mail_confirmed = candidate_state
                set_mailbox_led(has_mail_confirmed)

                if has_mail_confirmed:
                    logger.info("Wykryto nową korespondencję w skrzynce! Wysyłanie powiadomienia.")
                    send_mailbox_notification()
                else:
                    logger.info("Skrzynka na listy została opróżniona. Wyłączono diodę.")

        time.sleep(poll_interval)
