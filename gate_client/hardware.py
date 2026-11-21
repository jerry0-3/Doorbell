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
import queue
from typing import Optional, Dict

import gpiod
from gpiod.line import Bias, Direction, Value, Drive

from config import GateAppConfig, GpioInputConfig, GpioOutputConfig, config_manager

logger = logging.getLogger("vbram_log")

_gpio_lock = threading.Lock()
_gpio_request: Optional[gpiod.LineRequest] = None

# Flaga blokująca mruganie diody niebieskiej podczas operacji RFID
_blue_led_override = False
_feedback_timer: Optional[threading.Timer] = None

_BIAS_MAP = {
    "pull_up": Bias.PULL_UP,
    "pull_down": Bias.PULL_DOWN,
    "disabled": Bias.DISABLED,
}

# Konfiguracja dla odtwarzania dźwięków
_audio_queue: queue.Queue = queue.Queue(maxsize=10)
_audio_worker_lock = threading.Lock()
_audio_worker_started = False


def _make_input_settings(pin_cfg: GpioInputConfig) -> gpiod.LineSettings:
    """Buduje obiekt LineSettings dla wejścia GPIO."""
    return gpiod.LineSettings(
        direction=Direction.INPUT,
        bias=_BIAS_MAP[pin_cfg.bias],
        active_low=pin_cfg.active_low
    )


def _make_output_settings(pin_cfg: GpioOutputConfig, initial_active: bool = False) -> gpiod.LineSettings:
    """
    Buduje obiekt LineSettings dla wyjścia GPIO w trybie PUSH_PULL.
    active_low=True oznacza, że stan aktywny (Value.ACTIVE) wymusza fizyczne LOW (0V),
    a stan nieaktywny (Value.INACTIVE) wymusza fizyczne HIGH (3.3V).
    """
    return gpiod.LineSettings(
        direction=Direction.OUTPUT,
        drive=Drive.PUSH_PULL,
        active_low=pin_cfg.active_low,
        output_value=Value.ACTIVE if initial_active else Value.INACTIVE
    )


def _set_line_unlocked(pin_cfg: Optional[GpioOutputConfig], active: bool) -> None:
    """Ustawia stan linii (do wywołania wyłącznie wewnątrz sekcji chronionej _gpio_lock)."""
    if pin_cfg is None or _gpio_request is None:
        return
    # noinspection unresolved-references
    _gpio_request.set_value(pin_cfg.line, Value.ACTIVE if active else Value.INACTIVE)


def _open_gpio_lines_unlocked(cfg: GateAppConfig) -> None:
    """Rezerwuje skonfigurowane linie w kernelu przez libgpiod v2."""
    global _gpio_request
    gpio_cfg = cfg.gpio
    mb_cfg = cfg.mailbox

    line_config: Dict[int, gpiod.LineSettings] = {
        gpio_cfg.button.line: _make_input_settings(gpio_cfg.button),
        gpio_cfg.gate.line: _make_output_settings(gpio_cfg.gate, initial_active=False),
    }

    for out_pin in (gpio_cfg.led_red, gpio_cfg.led_blue, gpio_cfg.led_green, gpio_cfg.buzzer):
        if out_pin is not None:
            line_config[out_pin.line] = _make_output_settings(out_pin, initial_active=False)

    if mb_cfg.enabled:
        line_config[mb_cfg.sensor.line] = _make_input_settings(mb_cfg.sensor)
        if mb_cfg.led is not None:
            line_config[mb_cfg.led.line] = _make_output_settings(mb_cfg.led, initial_active=False)

    _gpio_request = gpiod.request_lines(
        gpio_cfg.chip_path,
        consumer=gpio_cfg.consumer_name,
        config=line_config
    )
    logger.info(
        f"Zainicjalizowano GPIO ({gpio_cfg.chip_path}, linie: {sorted(line_config.keys())})."
    )


def _release_gpio_unlocked(cfg: GateAppConfig) -> None:
    """Gasi wyjścia i zwalnia bieżący uchwyt gpiod."""
    global _gpio_request
    if _gpio_request is None:
        return
    try:
        gpio_cfg = cfg.gpio
        for out_pin in (
            gpio_cfg.gate,
            gpio_cfg.led_green,
            gpio_cfg.led_red,
            gpio_cfg.led_blue,
            gpio_cfg.buzzer,
        ):
            _set_line_unlocked(out_pin, False)
        if cfg.mailbox.enabled:
            _set_line_unlocked(cfg.mailbox.led, False)
        # noinspection unresolved-references
        _gpio_request.release()
    except Exception as err:
        logger.error(f"Błąd podczas zwalniania GPIO: {err}")
    finally:
        _gpio_request = None


def init_gpio() -> None:
    """Inicjalizuje linie GPIO przy starcie aplikacji."""
    with _gpio_lock:
        if _gpio_request is None:
            _open_gpio_lines_unlocked(config_manager.settings)


def cleanup_gpio() -> None:
    """Zwalnia zasoby GPIO przy zamykaniu programu."""
    global _feedback_timer
    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
            _feedback_timer = None
        _release_gpio_unlocked(config_manager.settings)


def handle_gpio_config_reload(old_cfg: GateAppConfig, new_cfg: GateAppConfig) -> None:
    """
    Callback dla HotReloadGateConfigManager.
    Przeładowuje linie w kernelu, jeśli zmieniły się numery pinów, polaryzacja active_low
    lub konfiguracja sprzętowa skrzynki na listy.
    """
    gpio_changed = old_cfg.gpio != new_cfg.gpio
    mailbox_hw_changed = (
        old_cfg.mailbox.enabled != new_cfg.mailbox.enabled
        or old_cfg.mailbox.sensor != new_cfg.mailbox.sensor
        or old_cfg.mailbox.led != new_cfg.mailbox.led
    )
    if not (gpio_changed or mailbox_hw_changed):
        return

    global _blue_led_override, _feedback_timer
    logger.info("Wykryto zmianę konfiguracji GPIO – ponowna inicjalizacja linii...")
    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
            _feedback_timer = None
        _blue_led_override = False
        _release_gpio_unlocked(old_cfg)
        _open_gpio_lines_unlocked(new_cfg)


# =====================================================================
# Sterowanie diodami statusu i brzęczykiem (Cykl RFID oraz Brama)
# =====================================================================

def update_normal_blue_blink(should_turn_on: bool) -> None:
    """Mruga diodą niebieską w stanie czuwania (chyba że trwa obsługa RFID/otwarcia)."""
    with _gpio_lock:
        if not _blue_led_override:
            _set_line_unlocked(config_manager.settings.gpio.led_blue, should_turn_on)


def set_rfid_waiting_state() -> None:
    """Zapala ciągle diodę niebieską podczas oczekiwania na odpowiedź serwera RFID."""
    global _blue_led_override, _feedback_timer
    gpio_cfg = config_manager.settings.gpio
    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
        _blue_led_override = True
        _set_line_unlocked(gpio_cfg.led_blue, True)
        _set_line_unlocked(gpio_cfg.led_red, False)
        _set_line_unlocked(gpio_cfg.led_green, False)


def _reset_after_feedback() -> None:
    """Po upływie czasu otwarcia/sygnalizacji gasi wszystkie wyjścia i wznawia mruganie niebieskiej."""
    global _blue_led_override
    gpio_cfg = config_manager.settings.gpio
    with _gpio_lock:
        _set_line_unlocked(gpio_cfg.gate, False)
        _set_line_unlocked(gpio_cfg.led_green, False)
        _set_line_unlocked(gpio_cfg.led_red, False)
        _set_line_unlocked(gpio_cfg.buzzer, False)
        _set_line_unlocked(gpio_cfg.led_blue, False)
        _blue_led_override = False


def open_gate() -> None:
    """Otwiera bramę (z aplikacji/DTMF) i uruchamia timer powrotu do stanu normalnego."""
    global _blue_led_override, _feedback_timer
    cfg = config_manager.settings
    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
        _blue_led_override = True
        _set_line_unlocked(cfg.gpio.gate, True)
        _set_line_unlocked(cfg.gpio.led_green, True)
        _set_line_unlocked(cfg.gpio.led_blue, True)

        _feedback_timer = threading.Timer(cfg.gate_hold_time_sec, _reset_after_feedback)
        _feedback_timer.daemon = True
        # noinspection unresolved-references
        _feedback_timer.start()

    play_audio_sequence(cfg.sounds.silence, cfg.sounds.confirm, cfg.sounds.gate_open)


def signal_rfid_accepted() -> None:
    """Poprawna karta RFID: świeci niebieska + zielona, otwiera bramę i włącza brzęczyk na 5s."""
    global _blue_led_override, _feedback_timer
    cfg = config_manager.settings
    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
        _blue_led_override = True
        _set_line_unlocked(cfg.gpio.led_blue, True)
        _set_line_unlocked(cfg.gpio.led_green, True)
        _set_line_unlocked(cfg.gpio.gate, True)
        _set_line_unlocked(cfg.gpio.buzzer, True)

        _feedback_timer = threading.Timer(cfg.gate_hold_time_sec, _reset_after_feedback)
        _feedback_timer.daemon = True
        # noinspection unresolved-references
        _feedback_timer.start()

    play_audio_sequence(cfg.sounds.silence, cfg.sounds.confirm, cfg.sounds.gate_open)


def _beep_three_times() -> None:
    """Generuje 3 krótkie sygnały na brzęczyku."""
    for _ in range(3):
        with _gpio_lock:
            _set_line_unlocked(config_manager.settings.gpio.buzzer, True)
        time.sleep(0.15)
        with _gpio_lock:
            _set_line_unlocked(config_manager.settings.gpio.buzzer, False)
        time.sleep(0.15)


def signal_rfid_rejected() -> None:
    """Nieprawidłowa karta RFID: świeci niebieska + czerwona przez 5s oraz 3 krótkie sygnały brzęczyka."""
    global _blue_led_override, _feedback_timer
    cfg = config_manager.settings
    with _gpio_lock:
        if _feedback_timer is not None:
            _feedback_timer.cancel()
        _blue_led_override = True
        _set_line_unlocked(cfg.gpio.led_blue, True)
        _set_line_unlocked(cfg.gpio.led_red, True)

        _feedback_timer = threading.Timer(cfg.gate_hold_time_sec, _reset_after_feedback)
        _feedback_timer.daemon = True
        # noinspection unresolved-references
        _feedback_timer.start()

    if cfg.gpio.buzzer is not None:
        threading.Thread(target=_beep_three_times, daemon=True).start()

    play_audio_sequence(cfg.sounds.silence, cfg.sounds.reject)


# =====================================================================
# Odczyt wejść i odtwarzanie audio
# =====================================================================

def read_button_state() -> str:
    """Zwraca '1' gdy przycisk jest wciśnięty, '0' gdy zwolniony."""
    with _gpio_lock:
        if _gpio_request is None:
            return "0"
        try:
            # noinspection unresolved-references
            val = _gpio_request.get_value(config_manager.settings.gpio.button.line)
            return "1" if val == Value.ACTIVE else "0"
        except Exception as err:
            logger.error(f"Błąd odczytu przycisku dzwonka: {err}")
            return "0"


def read_mailbox_sensor() -> bool:
    """Zwraca True, gdy czujnik skrzynki wykrywa obecność listów."""
    mb_cfg = config_manager.settings.mailbox
    if not mb_cfg.enabled:
        return False
    with _gpio_lock:
        if _gpio_request is None:
            return False
        try:
            # noinspection unresolved-references
            val = _gpio_request.get_value(mb_cfg.sensor.line)
            return val == Value.ACTIVE
        except Exception as err:
            logger.error(f"Błąd odczytu czujnika skrzynki: {err}")
            return False


def set_mailbox_led(has_mail: bool) -> None:
    """Steruje diodą powiadomienia o listach w skrzynce."""
    mb_cfg = config_manager.settings.mailbox
    if not mb_cfg.enabled:
        return
    with _gpio_lock:
        _set_line_unlocked(mb_cfg.led, has_mail)


def _audio_worker() -> None:
    """Wątek w tle odtwarzający kolejne sekwencje WAV ściśle sekwencyjnie."""
    while True:
        sound_files = _audio_queue.get()
        try:
            for sound_file in sound_files:
                result = subprocess.run(
                    ["/usr/bin/aplay", "-q", sound_file],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False
                )
                if result.returncode != 0:
                    err_msg = (result.stderr or "").strip()
                    logger.warning(
                        f"Błąd aplay (kod {result.returncode}) dla '{sound_file}': {err_msg}"
                    )
        except Exception as err:
            logger.error(f"Błąd wątku odtwarzania audio: {err}")
        finally:
            _audio_queue.task_done()


def _ensure_audio_worker() -> None:
    """Uruchamia wątek odtwarzania audio przy pierwszym użyciu."""
    global _audio_worker_started
    if _audio_worker_started:
        return
    with _audio_worker_lock:
        if not _audio_worker_started:
            threading.Thread(
                target=_audio_worker,
                name="AudioWorkerThread",
                daemon=True
            ).start()
            _audio_worker_started = True


def play_audio_sequence(*sound_files: str) -> None:
    """Kolejkuje sekwencję plików WAV do odtworzenia w tle bez kolizji w ALSA."""
    files = tuple(f for f in sound_files if f)
    if not files:
        return
    _ensure_audio_worker()
    try:
        _audio_queue.put_nowait(files)
    except queue.Full:
        logger.warning("Kolejka audio jest pełna – pominięto odtworzenie sekwencji.")


def play_audio(sound_file: str) -> None:
    """Zachowuje kompatybilność wsteczną dla pojedynczych wywołań (np. z lin_phone.py)."""
    play_audio_sequence(sound_file)
