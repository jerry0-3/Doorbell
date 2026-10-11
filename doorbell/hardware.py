"""
Obsługa wejść/wyjść GPIO, przekaźnika bramy, diody LED oraz dźwięków.
"""

import logging
import subprocess
import threading
from typing import Optional
import gpiod
from gpiod.line import Bias, Direction, Value

from config import (
    GPIO_CHIP_PATH,
    GPIO_CONSUMER_NAME,
    LINE_BUTTON,
    LINE_LED,
    LINE_GATE,
    GATE_HOLD_TIME_SEC
)

logger = logging.getLogger('vbram_log')

_gpio_lock = threading.Lock()
_gpio_request: Optional[gpiod.LineRequest] = None


def init_gpio() -> None:
    """
    Inicjalizuje linie GPIO przy użyciu biblioteki gpiod (API v2).
    Konfiguruje:
      - LINE_BUTTON jako wejście (INPUT),
      - LINE_GATE jako wyjście (OUTPUT, domyślnie INACTIVE),
      - LINE_LED jako wyjście (OUTPUT, active_low=True, aby Value.ACTIVE oznaczało zaświecenie).
    """
    global _gpio_request
    with _gpio_lock:
        if _gpio_request is not None:
            return

        try:
            _gpio_request = gpiod.request_lines(
                GPIO_CHIP_PATH,
                consumer=GPIO_CONSUMER_NAME,
                config={
                    LINE_BUTTON: gpiod.LineSettings(
                        direction=Direction.INPUT,
                        bias=Bias.PULL_UP,
                        active_low=True
                    ),
                    LINE_GATE: gpiod.LineSettings(
                        direction=Direction.OUTPUT,
                        output_value=Value.INACTIVE
                    ),
                    # active_low=True sprzętowo odwraca logikę (fizyczne 0 zapala diodę)
                    LINE_LED: gpiod.LineSettings(
                        direction=Direction.OUTPUT,
                        active_low=True,
                        output_value=Value.ACTIVE  # domyślnie dioda włączona w spoczynku
                    )
                }
            )
            logger.info(f"Pomyślnie zainicjalizowano GPIO przez gpiod ({GPIO_CHIP_PATH}).")
        except Exception as err:
            logger.error(f"Krytyczny błąd inicjalizacji gpiod: {err}")
            raise


def cleanup_gpio() -> None:
    """Zwalnia zarezerwowane linie GPIO i gasi wyjścia."""
    global _gpio_request
    with _gpio_lock:
        if _gpio_request is not None:
            try:
                _gpio_request.set_value(LINE_GATE, Value.INACTIVE)
                _gpio_request.set_value(LINE_LED, Value.INACTIVE)
                _gpio_request.release()
            except Exception as err:
                logger.error(f"Błąd podczas zwalniania linii GPIO: {err}")
            finally:
                _gpio_request = None


def set_led_state(turn_on: bool) -> None:
    """Włącza lub wyłącza diodę LED."""
    with _gpio_lock:
        if _gpio_request:
            _gpio_request.set_value(LINE_LED, Value.ACTIVE if turn_on else Value.INACTIVE)


def set_gate_relay(turn_on: bool) -> None:
    """Włącza lub wyłącza przekaźnik otwierania bramy."""
    with _gpio_lock:
        if _gpio_request:
            _gpio_request.set_value(LINE_GATE, Value.ACTIVE if turn_on else Value.INACTIVE)


def lock_gate() -> None:
    """Timer callback: rozłącza przekaźnik bramy i włącza stale diodę LED."""
    set_gate_relay(False)
    set_led_state(True)


def open_gate() -> None:
    """Uruchamia impuls otwarcia bramy i planuje automatyczne rozłączenie."""
    set_gate_relay(True)
    set_led_state(False)
    timer = threading.Timer(GATE_HOLD_TIME_SEC, lock_gate)
    timer.daemon = True
    timer.start()


def read_button_state() -> str:
    """
    Odczytuje aktualny stan przycisku dzwonka za pomocą gpiod.
    Zwraca '1' (wciśnięty/ACTIVE) lub '0' (zwolniony/INACTIVE).
    """
    with _gpio_lock:
        if not _gpio_request:
            return '0'
        try:
            val = _gpio_request.get_value(LINE_BUTTON)
            return '1' if val == Value.ACTIVE else '0'
        except Exception as err:
            logger.error(f"Błąd odczytu stanu przycisku z gpiod: {err}")
            return '0'


def play_audio(sound_file: str) -> None:
    """Odtwarza plik dźwiękowy w tle (nieblokująco)."""
    try:
        subprocess.Popen(
            ['/usr/bin/aplay', '-q', sound_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as err:
        logger.error(f"Błąd odtwarzania audio ({sound_file}): {err}")
