"""
Obsługa wejść/wyjść przez libgpiod v2:
  - Dioda niebieska: mruga w stanie normalnym, miga szybko po dzwonku,
    świeci ciągle przy odczycie karty RFID i otwarciu bramy.
  - Dioda zielona + brama + brzęczyk (5s ciągły) przy poprawnej karcie.
  - Dioda czerwona (5s) + brzęczyk (3 krótkie sygnały) przy odrzuconej karcie.
  - Opcjonalne piny czujnika i diody skrzynki na listy.
  - Sekwencyjna kolejka odtwarzania plików WAV w tle (bez kolizji ALSA).
"""

from dataclasses import dataclass, field
from enum import Enum, auto
import logging
import queue
import subprocess
import threading
import time
from typing import Dict, Optional, Tuple

import gpiod
from gpiod.line import Bias, Direction, Drive, Value

from config import GateAppConfig, GpioInputConfig, GpioOutputConfig, config_manager

logger = logging.getLogger("vbram_log")

# Mapowanie nazw rezystorów podciągających z pliku JSON na stałe libgpiod v2
_BIAS_MAP: Dict[str, Bias] = {
    "pull_up": Bias.PULL_UP,
    "pull_down": Bias.PULL_DOWN,
    "disabled": Bias.DISABLED,
}


class LedMode(Enum):
    """Dozwolone tryby pracy niebieskiej diody statusu."""

    # Wolne pulsowanie w czuwaniu (krótki błysk raz na sekundę)
    NORMAL = auto()
    # Szybkie miganie po wciśnięciu dzwonka (oczekiwanie na reakcję z telefonu)
    DOORBELL_WAITING = auto()
    # Pełne przejęcie kontroli nad diodą przez zdarzenie (RFID, otwarcie bramy, błąd)
    OVERRIDE = auto()


@dataclass
class _HardwareState:
    """
    Centralny kontener na cały stan runtime modułu sprzętowego.
    Zastępuje rozproszone zmienne globalne i grupuje obiekty synchronizacji.
    """

    # --- Stan GPIO i sygnalizacji ---

    # Blokada chroniąca dostęp do linii GPIO, trybu diody oraz timera między wątkami
    gpio_lock: threading.Lock = field(default_factory=threading.Lock)
    # Aktywny uchwyt zarezerwowanych linii w kernelu (None przed init_gpio lub po cleanup_gpio)
    gpio_request: Optional[gpiod.LineRequest] = None
    # Bieżący tryb pracy niebieskiej diody statusu
    led_mode: LedMode = LedMode.NORMAL
    # Aktywny timer przywracający stan spoczynkowy po zakończeniu sygnalizacji (otwarcie/błąd/dzwonek)
    feedback_timer: Optional[threading.Timer] = None

    # --- Stan podsystemu audio ---

    # Kolejka FIFO przechowująca krotki ścieżek WAV do odtworzenia jedna po drugiej
    audio_queue: "queue.Queue[Tuple[str, ...]]" = field(
        default_factory=lambda: queue.Queue(maxsize=10)
    )
    # Blokada zapobiegająca jednoczesnemu uruchomieniu dwóch wątków audio przy pierwszym wywołaniu
    audio_worker_lock: threading.Lock = field(default_factory=threading.Lock)
    # Flaga informująca, czy wątek roboczy odtwarzający dźwięki z kolejki już działa
    audio_worker_started: bool = False


# Jedyna instancja stanu modułu
_state = _HardwareState()


# =====================================================================
# Funkcje pomocnicze konfiguracji i cyklu życia GPIO
# =====================================================================

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
    """Ustawia stan pojedynczej linii (wywoływać wyłącznie pod blokadą _state.gpio_lock)."""
    req = _state.gpio_request
    if pin_cfg is None or req is None:
        return
    req.set_value(pin_cfg.line, Value.ACTIVE if active else Value.INACTIVE)


def _cancel_feedback_timer_unlocked() -> None:
    """Anuluje aktywny timer powrotu do stanu normalnego (wywoływać pod _state.gpio_lock)."""
    if _state.feedback_timer is not None:
        _state.feedback_timer.cancel()
        _state.feedback_timer = None


def _schedule_reset_unlocked(delay_sec: float) -> None:
    """Planuje powrót diod, przekaźnika i brzęczyka do stanu spoczynkowego po delay_sec."""
    _cancel_feedback_timer_unlocked()
    timer = threading.Timer(delay_sec, _reset_after_feedback)
    timer.daemon = True
    timer.start()
    _state.feedback_timer = timer


def _open_gpio_lines_unlocked(cfg: GateAppConfig) -> None:
    """Rezerwuje skonfigurowane linie w kernelu przez libgpiod v2."""
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

    _state.gpio_request = gpiod.request_lines(
        gpio_cfg.chip_path,
        consumer=gpio_cfg.consumer_name,
        config=line_config
    )
    logger.info(
        f"Zainicjalizowano GPIO ({gpio_cfg.chip_path}, linie: {sorted(line_config.keys())})."
    )


def _release_gpio_unlocked(cfg: GateAppConfig) -> None:
    """Gasi wszystkie wyjścia i zwalnia bieżący uchwyt gpiod."""
    req = _state.gpio_request
    if req is None:
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
        req.release()
    except Exception as err:
        logger.error(f"Błąd podczas zwalniania GPIO: {err}")
    finally:
        _state.gpio_request = None


def init_gpio() -> None:
    """Inicjalizuje linie GPIO przy starcie aplikacji."""
    with _state.gpio_lock:
        if _state.gpio_request is None:
            _open_gpio_lines_unlocked(config_manager.settings)


def cleanup_gpio() -> None:
    """Anuluje aktywne timery i zwalnia zasoby GPIO przy zamykaniu programu."""
    with _state.gpio_lock:
        _cancel_feedback_timer_unlocked()
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

    logger.info("Wykryto zmianę konfiguracji GPIO – ponowna inicjalizacja linii...")
    with _state.gpio_lock:
        _cancel_feedback_timer_unlocked()
        _state.led_mode = LedMode.NORMAL
        _release_gpio_unlocked(old_cfg)
        _open_gpio_lines_unlocked(new_cfg)


# =====================================================================
# Sterowanie diodami statusu i brzęczykiem (Dzwonek, RFID, Brama)
# =====================================================================

def update_status_led(now: float) -> None:
    """
    Aktualizuje stan diody niebieskiej w zależności od bieżącego trybu pracy:
      - NORMAL: mignięcie (150 ms) co 1,5 sekundy,
      - DOORBELL_WAITING: szybkie miganie 10 Hz (50 ms ON / 50 ms OFF),
      - OVERRIDE: diodą steruje aktywne zdarzenie (RFID lub otwieranie bramy).
    """
    with _state.gpio_lock:
        if _state.led_mode is LedMode.OVERRIDE:
            return
        if _state.led_mode is LedMode.DOORBELL_WAITING:
            should_turn_on = (int(now * 20) % 2) == 0
        else:
            should_turn_on = (now % 1.5) < 0.15

        _set_line_unlocked(config_manager.settings.gpio.led_blue, should_turn_on)


def _beep_pattern(count: int, on_sec: float = 0.15, off_sec: float = 0.15) -> None:
    """Generuje w tle zadaną liczbę impulsów na brzęczyku."""
    for i in range(count):
        with _state.gpio_lock:
            _set_line_unlocked(config_manager.settings.gpio.buzzer, True)
        time.sleep(on_sec)
        with _state.gpio_lock:
            _set_line_unlocked(config_manager.settings.gpio.buzzer, False)
        if i < count - 1:
            time.sleep(off_sec)


def _reset_after_feedback() -> None:
    """Gasi wyjścia wynikowe i przywraca diodę niebieską do normalnego czuwania."""
    gpio_cfg = config_manager.settings.gpio
    with _state.gpio_lock:
        _set_line_unlocked(gpio_cfg.gate, False)
        _set_line_unlocked(gpio_cfg.led_green, False)
        _set_line_unlocked(gpio_cfg.led_red, False)
        _set_line_unlocked(gpio_cfg.buzzer, False)
        _set_line_unlocked(gpio_cfg.led_blue, False)
        _state.feedback_timer = None
        _state.led_mode = LedMode.NORMAL


def signal_doorbell_pressed() -> None:
    """
    Sygnalizacja po wciśnięciu przycisku dzwonka i udanym wysłaniu powiadomienia:
      - Dioda niebieska przechodzi w szybkie miganie przez czas doorbell_feedback_sec,
      - Brzęczyk wydaje krótki sygnał potwierdzenia,
      - Głośnik odtwarza dźwięk dzwonka.
    """
    cfg = config_manager.settings
    with _state.gpio_lock:
        _set_line_unlocked(cfg.gpio.led_red, False)
        _state.led_mode = LedMode.DOORBELL_WAITING
        _schedule_reset_unlocked(cfg.doorbell_feedback_sec)

    if cfg.gpio.buzzer is not None:
        threading.Thread(
            target=_beep_pattern,
            args=(1, 0.25, 0.0),
            daemon=True
        ).start()

    doorbell_wav = cfg.sounds.doorbell or cfg.sounds.confirm
    play_audio_sequence(cfg.sounds.silence, doorbell_wav)


def signal_doorbell_failed() -> None:
    """
    Sygnalizacja błędu, gdy nie udało się wysłać powiadomienia do brokera:
      - Czerwona dioda zapala się na 2 sekundy + 2 krótkie sygnały brzęczyka + dźwięk odrzucenia.
    """
    cfg = config_manager.settings
    with _state.gpio_lock:
        _state.led_mode = LedMode.OVERRIDE
        _set_line_unlocked(cfg.gpio.led_blue, False)
        _set_line_unlocked(cfg.gpio.led_red, True)
        _schedule_reset_unlocked(2.0)

    if cfg.gpio.buzzer is not None:
        threading.Thread(
            target=_beep_pattern,
            args=(2, 0.12, 0.12),
            daemon=True
        ).start()

    play_audio_sequence(cfg.sounds.silence, cfg.sounds.reject)


def cancel_doorbell_waiting() -> None:
    """Przerywa szybkie miganie po odrzuceniu powiadomienia z telefonu."""
    with _state.gpio_lock:
        if _state.led_mode is LedMode.DOORBELL_WAITING:
            _cancel_feedback_timer_unlocked()
            _state.led_mode = LedMode.NORMAL


def set_rfid_waiting_state() -> None:
    """Zapala ciągle diodę niebieską podczas oczekiwania na odpowiedź serwera RFID."""
    gpio_cfg = config_manager.settings.gpio
    with _state.gpio_lock:
        _cancel_feedback_timer_unlocked()
        _state.led_mode = LedMode.OVERRIDE
        _set_line_unlocked(gpio_cfg.led_blue, True)
        _set_line_unlocked(gpio_cfg.led_red, False)
        _set_line_unlocked(gpio_cfg.led_green, False)


def open_gate() -> None:
    """Otwiera bramę (z aplikacji/DTMF), odtwarza komunikaty i planuje powrót do stanu normalnego."""
    cfg = config_manager.settings
    with _state.gpio_lock:
        _state.led_mode = LedMode.OVERRIDE
        _set_line_unlocked(cfg.gpio.gate, True)
        _set_line_unlocked(cfg.gpio.led_green, True)
        _set_line_unlocked(cfg.gpio.led_blue, True)
        _schedule_reset_unlocked(cfg.gate_hold_time_sec)

    play_audio_sequence(cfg.sounds.silence, cfg.sounds.confirm, cfg.sounds.gate_open)


def signal_rfid_accepted() -> None:
    """Poprawna karta RFID: niebieska + zielona, przekaźnik bramy i ciągły brzęczyk na 5s."""
    cfg = config_manager.settings
    with _state.gpio_lock:
        _state.led_mode = LedMode.OVERRIDE
        _set_line_unlocked(cfg.gpio.led_blue, True)
        _set_line_unlocked(cfg.gpio.led_green, True)
        _set_line_unlocked(cfg.gpio.gate, True)
        _set_line_unlocked(cfg.gpio.buzzer, True)
        _schedule_reset_unlocked(cfg.gate_hold_time_sec)

    play_audio_sequence(cfg.sounds.silence, cfg.sounds.confirm, cfg.sounds.gate_open)


def signal_rfid_rejected() -> None:
    """Nieprawidłowa karta RFID: niebieska + czerwona przez 5s oraz 3 krótkie sygnały brzęczyka."""
    cfg = config_manager.settings
    with _state.gpio_lock:
        _state.led_mode = LedMode.OVERRIDE
        _set_line_unlocked(cfg.gpio.led_blue, True)
        _set_line_unlocked(cfg.gpio.led_red, True)
        _schedule_reset_unlocked(cfg.gate_hold_time_sec)

    if cfg.gpio.buzzer is not None:
        threading.Thread(
            target=_beep_pattern,
            args=(3, 0.15, 0.15),
            daemon=True
        ).start()

    play_audio_sequence(cfg.sounds.silence, cfg.sounds.reject)


# =====================================================================
# Odczyt wejść i obsługa skrzynki na listy
# =====================================================================

def read_button_state() -> str:
    """Zwraca '1' gdy przycisk dzwonka jest wciśnięty, '0' gdy zwolniony."""
    with _state.gpio_lock:
        req = _state.gpio_request
        if req is None:
            return "0"
        try:
            val = req.get_value(config_manager.settings.gpio.button.line)
            return "1" if val == Value.ACTIVE else "0"
        except Exception as err:
            logger.error(f"Błąd odczytu przycisku dzwonka: {err}")
            return "0"


def read_mailbox_sensor() -> bool:
    """Zwraca True, gdy czujnik skrzynki wykrywa obecność listów."""
    mb_cfg = config_manager.settings.mailbox
    if not mb_cfg.enabled:
        return False
    with _state.gpio_lock:
        req = _state.gpio_request
        if req is None:
            return False
        try:
            val = req.get_value(mb_cfg.sensor.line)
            return val == Value.ACTIVE
        except Exception as err:
            logger.error(f"Błąd odczytu czujnika skrzynki: {err}")
            return False


def set_mailbox_led(has_mail: bool) -> None:
    """Steruje diodą powiadomienia o listach w skrzynce."""
    mb_cfg = config_manager.settings.mailbox
    if not mb_cfg.enabled:
        return
    with _state.gpio_lock:
        _set_line_unlocked(mb_cfg.led, has_mail)


# =====================================================================
# Sekwencyjna kolejka odtwarzania audio (ALSA / aplay)
# =====================================================================

def _audio_worker() -> None:
    """Wątek w tle odtwarzający kolejne sekwencje WAV ściśle sekwencyjnie."""
    while True:
        sound_files = _state.audio_queue.get()
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
            _state.audio_queue.task_done()


def _ensure_audio_worker() -> None:
    """Uruchamia wątek odtwarzania audio przy pierwszym użyciu."""
    if _state.audio_worker_started:
        return
    with _state.audio_worker_lock:
        if not _state.audio_worker_started:
            threading.Thread(
                target=_audio_worker,
                name="AudioWorkerThread",
                daemon=True
            ).start()
            _state.audio_worker_started = True


def play_audio_sequence(*sound_files: str) -> None:
    """Kolejkuje sekwencję plików WAV do odtworzenia w tle bez kolizji w ALSA."""
    files = tuple(f for f in sound_files if f)
    if not files:
        return
    _ensure_audio_worker()
    try:
        _state.audio_queue.put_nowait(files)
    except queue.Full:
        logger.warning("Kolejka audio jest pełna – pominięto odtworzenie sekwencji.")


def play_audio(sound_file: str) -> None:
    """Zachowuje kompatybilność wsteczną dla pojedynczych wywołań."""
    play_audio_sequence(sound_file)
