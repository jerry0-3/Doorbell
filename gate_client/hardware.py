"""
Obsługa wejść/wyjść przez libgpiod v2:
  - Dioda niebieska: mruga w stanie normalnym, świeci ciągle przy odczycie karty,
    gaśnie na koniec 5-sekundowego cyklu i wraca do mrugania.
  - Dioda zielona + brama + brzęczyk (5s ciągły) przy poprawnej karcie.
  - Dioda czerwona (5s) + brzęczyk (3 krótkie sygnały) przy odrzuconej karcie.
  - Opcjonalne piny czujnika i diody skrzynki na listy.
"""

import logging
import subprocess
import threading
import time
from typing import Optional, cast

import gpiod
from gpiod.line import Bias, Direction, Value

from gate_client.config import settings

logger = logging.getLogger("vbram_log")

_gpio_lock = threading.Lock()
_gpio_request: Optional[gpiod.LineRequest] = None

# Flaga blokująca mruganie diody niebieskiej podczas operacji RFID
_blue_led_override = False
_feedback_timer: Optional[threading.Timer] = None


def init_gpio() -> None:
    """
    Rezerwuje linie GPIO w libgpiod v2.
    Dynamicznie dołącza piny diody zielonej oraz modułu skrzynki na listy,
    jeśli są włączone w konfiguracji.
    """
    global _gpio_request
    cfg = settings.gpio
    mb_cfg = settings.mailbox

    with _gpio_lock:
        if _gpio_request is not None:
            return

        line_config = {
            cfg.line_button: gpiod.LineSettings(
                direction=Direction.INPUT,
                bias=Bias.PULL_UP,
                active_low=True
            ),
            cfg.line_led_red: gpiod.LineSettings(
                direction=Direction.OUTPUT,
                active_low=True,
                output_value=Value.INACTIVE
            ),
            cfg.line_led_blue: gpiod.LineSettings(
                direction=Direction.OUTPUT,
                active_low=True,
                output_value=Value.INACTIVE
            ),
            cfg.line_gate: gpiod.LineSettings(
                direction=Direction.OUTPUT,
                output_value=Value.INACTIVE
            ),
            cfg.line_buzzer: gpiod.LineSettings(
                direction=Direction.OUTPUT,
                active_low=cfg.buzzer_active_low,
                output_value=Value.INACTIVE
            ),
        }

        # Jeśli dioda zielona ma osobny pin (inny niż przekaźnik bramy)
        if cfg.line_led_green is not None and cfg.line_led_green != cfg.line_gate:
            line_config[cast(int, cfg.line_led_green)] = gpiod.LineSettings(
                direction=Direction.OUTPUT,
                active_low=True,
                output_value=Value.INACTIVE
            )

        # Inicjalizacja pinów skrzynki, tylko jeśli moduł jest włączony
        if mb_cfg.enabled:
            line_config[mb_cfg.line_sensor] = gpiod.LineSettings(
                direction=Direction.INPUT,
                bias=Bias.PULL_UP,
                active_low=mb_cfg.sensor_active_low
            )
            line_config[mb_cfg.line_led] = gpiod.LineSettings(
                direction=Direction.OUTPUT,
                active_low=mb_cfg.led_active_low,
                output_value=Value.INACTIVE
            )

        _gpio_request = gpiod.request_lines(
            cfg.chip_path,
            consumer=cfg.consumer_name,
            config=line_config
        )
        logger.info(
            f"Zainicjalizowano GPIO ({cfg.chip_path}), "
            f"moduł skrzynki: {'AKTYWNY' if mb_cfg.enabled else 'WYŁĄCZONY'}."
        )


def _set_line(line: Optional[int], active: bool) -> None:
    """Wewnętrzna funkcja pomocnicza ustawiająca stan pojedynczej linii."""
    if line is None or _gpio_request is None:
        return
    _gpio_request.set_value(line, Value.ACTIVE if active else Value.INACTIVE)


def cleanup_gpio() -> None:
    """Zwalnia zarezerwowane linie GPIO i wyłącza wyjścia."""
    global _gpio_request
    cfg = settings.gpio
    mb_cfg = settings.mailbox

    with _gpio_lock:
        if _gpio_request is not None:
            try:
                _set_line(cfg.line_gate, False)
                _set_line(cfg.line_led_green, False)
                _set_line(cfg.line_led_red, False)
                _set_line(cfg.line_led_blue, False)
                _set_line(cfg.line_buzzer, False)
                if mb_cfg.enabled:
                    _set_line(mb_cfg.line_led, False)
                _gpio_request.release()
            except Exception as err:
                logger.error(f"Błąd podczas zwalniania GPIO: {err}")
            finally:
                _gpio_request = None


# =====================================================================
# Sterowanie diodami statusu i brzęczykiem (Cykl RFID oraz Brama)
# =====================================================================

def update_normal_blue_blink(should_turn_on: bool) -> None:
    """
    Wywoływane z pętli głównej do mrugania niebieską diodą w stanie normalnym.
    Jeśli trwa obsługa karty RFID (_blue_led_override == True), mruganie jest ignorowane.
    """
    with _gpio_lock:
        if not _blue_led_override:
            _set_line(settings.gpio.line_led_blue, should_turn_on)


def set_rfid_waiting_state() -> None:
    """
    Wywoływane w momencie przyłożenia karty RFID:
    Dioda niebieska przechodzi w stan ciągłego świecenia w oczekiwaniu na odpowiedź serwera.
    """
    global _blue_led_override, _feedback_timer
    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
        _blue_led_override = True
        _set_line(settings.gpio.line_led_blue, True)
        _set_line(settings.gpio.line_led_red, False)
        _set_line(settings.gpio.line_led_green, False)


def _reset_after_feedback() -> None:
    """
    Callback po 5 sekundach od autoryzacji/odrzucenia:
    Wyłącza diody wynikowe (zieloną/czerwoną), wyłącza brzęczyk, zamyka bramę,
    gasi diodę niebieską i przywraca ją do trybu normalnego mrugania.
    """
    global _blue_led_override
    cfg = settings.gpio
    with _gpio_lock:
        _set_line(cfg.line_gate, False)
        _set_line(cfg.line_led_green, False)
        _set_line(cfg.line_led_red, False)
        _set_line(cfg.line_buzzer, False)
        _set_line(cfg.line_led_blue, False)
        _blue_led_override = False


def open_gate() -> None:
    """
    Otwiera bramę (np. z aplikacji mobilnej lub kodem DTMF) na 5 sekund.
    Włącza przekaźnik bramy i diodę zieloną, po czym przywraca stan normalny.
    """
    global _blue_led_override, _feedback_timer
    cfg = settings.gpio
    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
        _blue_led_override = True
        _set_line(cfg.line_gate, True)
        _set_line(cfg.line_led_green, True)
        _set_line(cfg.line_led_blue, True)

        _feedback_timer = threading.Timer(settings.gate_hold_time_sec, _reset_after_feedback)
        _feedback_timer.daemon = True
        # noinspection unresolved-references
        _feedback_timer.start()


def signal_rfid_accepted() -> None:
    """
    Poprawna karta RFID:
      - Niebieska dioda świeci dalej,
      - Włącza się zielona dioda, przekaźnik bramy oraz ciągły brzęczyk na 5 sekund,
      - Po 5 sekundach obie diody i brzęczyk gasną, a niebieska wraca do mrugania.
    """
    global _blue_led_override, _feedback_timer
    cfg = settings.gpio
    hold_time = settings.gate_hold_time_sec

    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
        _blue_led_override = True
        _set_line(cfg.line_led_blue, True)
        _set_line(cfg.line_led_green, True)
        _set_line(cfg.line_gate, True)
        _set_line(cfg.line_buzzer, True)

        _feedback_timer = threading.Timer(hold_time, _reset_after_feedback)
        _feedback_timer.daemon = True
        # noinspection unresolved-references
        _feedback_timer.start()

    play_audio(settings.sounds.silence)
    play_audio(settings.sounds.confirm)
    time.sleep(0.3)
    play_audio(settings.sounds.gate_open)


def _beep_three_times() -> None:
    """Zadanie w tle generujące 3 krótkie sygnały brzęczyka przy złej karcie."""
    buzzer_pin = settings.gpio.line_buzzer
    for _ in range(3):
        with _gpio_lock:
            _set_line(buzzer_pin, True)
        time.sleep(0.15)
        with _gpio_lock:
            _set_line(buzzer_pin, False)
        time.sleep(0.15)


def signal_rfid_rejected() -> None:
    """
    Nieprawidłowa karta RFID:
      - Niebieska dioda świeci dalej,
      - Włącza się czerwona dioda na 5 sekund,
      - Brzęczyk wydaje 3 krótkie sygnały,
      - Po 5 sekundach obie diody gasną, a niebieska wraca do mrugania.
    """
    global _blue_led_override, _feedback_timer
    cfg = settings.gpio
    hold_time = settings.gate_hold_time_sec

    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
        _blue_led_override = True
        _set_line(cfg.line_led_blue, True)
        _set_line(cfg.line_led_red, True)

        _feedback_timer = threading.Timer(hold_time, _reset_after_feedback)
        _feedback_timer.daemon = True
        # noinspection unresolved-references
        _feedback_timer.start()

    threading.Thread(target=_beep_three_times, daemon=True).start()
    play_audio(settings.sounds.silence)
    play_audio(settings.sounds.reject)


# =====================================================================
# Odczyt przycisku dzwonka, skrzynki na listy oraz audio
# =====================================================================

def read_button_state() -> str:
    """Zwraca '1' gdy przycisk dzwonka jest wciśnięty, '0' gdy zwolniony."""
    with _gpio_lock:
        if not _gpio_request:
            return "0"
        try:
            val = _gpio_request.get_value(settings.gpio.line_button)
            return "1" if val == Value.ACTIVE else "0"
        except Exception as err:
            logger.error(f"Błąd odczytu przycisku dzwonka: {err}")
            return "0"


def read_mailbox_sensor() -> bool:
    """
    Odczytuje stan czujnika skrzynki na listy.
    Zwraca True, jeśli w skrzynce wykryto korespondencję (Value.ACTIVE).
    """
    if not settings.mailbox.enabled:
        return False
    with _gpio_lock:
        if not _gpio_request:
            return False
        try:
            val = _gpio_request.get_value(settings.mailbox.line_sensor)
            return val == Value.ACTIVE
        except Exception as err:
            logger.error(f"Błąd odczytu czujnika skrzynki: {err}")
            return False


def set_mailbox_led(has_mail: bool) -> None:
    """Włącza lub gasi dedykowaną diodę powiadomienia o listach w skrzynce."""
    if not settings.mailbox.enabled:
        return
    with _gpio_lock:
        _set_line(settings.mailbox.line_led, has_mail)


def play_audio(sound_file: str) -> None:
    """Odtwarza plik WAV w tle bez blokowania wątku."""
    try:
        subprocess.Popen(
            ["/usr/bin/aplay", "-q", sound_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as err:
        logger.error(f"Błąd odtwarzania pliku audio ({sound_file}): {err}")
