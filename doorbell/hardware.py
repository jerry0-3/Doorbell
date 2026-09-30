"""
Obsługa wejść/wyjść GPIO, przekaźnika bramy, diody LED oraz dźwięków.
"""

import logging
import subprocess
import threading

from config import (
    BUTTON_GPIO_PATH,
    LED_GPIO_PATH,
    GATE_GPIO_PATH,
    GATE_HOLD_TIME_SEC
)

logger = logging.getLogger('vbram_log')


def write_gpio(path: str, value: str) -> None:
    """Zapisuje wartość ('0' lub '1') do sysfs GPIO."""
    try:
        with open(path, 'w') as f:
            f.write(value)
    except IOError as err:
        logger.error(f"Błąd zapisu do {path}: {err}")


def set_led_state(turn_on: bool) -> None:
    """Steruje diodą (odwrócona logika: 0 = włączona, 1 = wyłączona)."""
    write_gpio(LED_GPIO_PATH, '0' if turn_on else '1')


def set_gate_relay(turn_on: bool) -> None:
    """Steruje przekaźnikiem bramy (1 = zwarty, 0 = rozwarty)."""
    write_gpio(GATE_GPIO_PATH, '1' if turn_on else '0')


def lock_gate() -> None:
    """Wyłącza przekaźnik i przywraca diodę do stanu podstawowego."""
    set_gate_relay(False)
    set_led_state(True)


def open_gate() -> None:
    """Załącza przekaźnik otwarcia bramy na określony czas."""
    set_gate_relay(True)
    set_led_state(False)
    timer = threading.Timer(GATE_HOLD_TIME_SEC, lock_gate)
    timer.daemon = True
    timer.start()


def read_button_state() -> str:
    """Odczytuje stan przycisku dzwonka (0 lub 1)."""
    try:
        with open(BUTTON_GPIO_PATH, 'r') as f:
            state = f.read(1).strip()
            return state if state in ('0', '1') else '0'
    except IOError:
        return '0'


def play_audio(sound_file: str) -> None:
    """Odtwarza dźwięk w tle bez blokowania wątku głównego."""
    try:
        subprocess.Popen(
            ['/usr/bin/aplay', '-q', sound_file],  # -q wycisza zbędne wyjście aplay
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as err:
        logger.error(f"Błąd odtwarzania {sound_file}: {err}")
