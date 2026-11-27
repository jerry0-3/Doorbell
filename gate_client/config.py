"""
Modele konfiguracyjne Pydantic dla klienta bramy oraz menedżer gorącego
przeładowywania (hot-reload) konfiguracji na podstawie st_mtime i rozmiaru pliku.
"""

import logging
from pathlib import Path
import threading
import time
from typing import Callable, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field, model_validator

from common.config_loader import load_config

logger = logging.getLogger("vbram_log")


class HttpServerConfig(BaseModel):
    ip: str
    port: int = Field(ge=1, le=65535)


class NotificationConfig(BaseModel):
    broker_url: str
    doorbell_topic: str
    doorbell_priority: int = Field(default=5, ge=1, le=5)


class SipConfig(BaseModel):
    default_dial_number: str
    linphone_log_path: str
    magic_dtmf_regex: str
    max_inter_digit_sec: float = Field(gt=0.0)


class GpioOutputConfig(BaseModel):
    """Konfiguracja pojedynczego wyjścia GPIO z możliwością ustawienia active_low."""
    line: int = Field(ge=0)
    active_low: bool = False


class GpioInputConfig(BaseModel):
    """Konfiguracja pojedynczego wejścia GPIO z active_low oraz rezystorem pull-up/down."""
    line: int = Field(ge=0)
    active_low: bool = True
    bias: Literal["pull_up", "pull_down", "disabled"] = "pull_up"


class GpioConfig(BaseModel):
    chip_path: str
    consumer_name: str
    button: GpioInputConfig
    led_red: Optional[GpioOutputConfig] = None
    led_blue: Optional[GpioOutputConfig] = None
    gate: GpioOutputConfig
    led_green: Optional[GpioOutputConfig] = None
    buzzer: Optional[GpioOutputConfig] = None


class SoundsConfig(BaseModel):
    silence: str
    confirm: str
    reject: str
    gate_open: str
    # Opcjonalny plik WAV dla dzwonka; jeśli None, użyty zostanie dźwięk 'confirm'
    doorbell: Optional[str] = None


class RfidConfig(BaseModel):
    enabled: bool = True
    serial_port: str
    baudrate: int = 9600
    auth_server_ip: str
    auth_server_port: int = Field(ge=1, le=65535)
    socket_timeout_sec: float = 4.0
    max_time_drift_sec: float = 12.0
    aes_key_pem_path: str


class MailboxConfig(BaseModel):
    enabled: bool = False
    sensor: GpioInputConfig = GpioInputConfig(line=19, active_low=True, bias="pull_up")
    led: Optional[GpioOutputConfig] = GpioOutputConfig(line=20, active_low=True)
    debounce_sec: float = Field(default=2.0, gt=0.0)
    topic: str = "mailbox_alerts"
    priority: int = Field(default=3, ge=1, le=5)
    title: str = "Skrzynka na listy"
    message: str = "W skrzynce znajduje się nowa korespondencja."


class GateAppConfig(BaseModel):
    app_log_path: str
    gate_hold_time_sec: float = Field(gt=0.0)
    doorbell_feedback_sec: float = Field(default=20.0, gt=0.0)
    token_ttl_seconds: float = Field(gt=0.0)
    http_server: HttpServerConfig
    notification: NotificationConfig
    sip: SipConfig
    gpio: GpioConfig
    sounds: SoundsConfig
    rfid: RfidConfig
    mailbox: MailboxConfig

    @model_validator(mode="after")
    def validate_unique_gpio_lines(self) -> "GateAppConfig":
        """Blokuje przypadkowe przypisanie tego samego pinu GPIO do dwóch różnych funkcji."""
        assigned: Dict[int, str] = {}

        candidates: List[Tuple[str, Optional[BaseModel]]] = [
            ("gpio.button", self.gpio.button),
            ("gpio.led_red", self.gpio.led_red),
            ("gpio.led_blue", self.gpio.led_blue),
            ("gpio.gate", self.gpio.gate),
            ("gpio.led_green", self.gpio.led_green),
            ("gpio.buzzer", self.gpio.buzzer),
        ]
        if self.mailbox.enabled:
            candidates.append(("mailbox.sensor", self.mailbox.sensor))
            candidates.append(("mailbox.led", self.mailbox.led))

        for name, pin_cfg in candidates:
            if pin_cfg is None:
                continue
            # noinspection unresolved-references
            line_num = pin_cfg.line
            if line_num in assigned:
                raise ValueError(
                    f"Kolizja pinów GPIO! Linia {line_num} jest przypisana jednocześnie do "
                    f"'{assigned[line_num]}' oraz '{name}'. Jeśli element współdzieli pin sprzętowo "
                    f"(np. zielona dioda z bramą), ustaw dla niego wartość null."
                )
            assigned[line_num] = name

        return self


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CFG_PATH = BASE_DIR / "config.default.json"
LOCAL_CFG_PATH = BASE_DIR / "config.local.json"

ReloadCallback = Callable[[GateAppConfig, GateAppConfig], None]


def _get_files_signature() -> Tuple[Tuple[float, int], Tuple[float, int]]:
    """Pobiera (mtime, size) obu plików JSON bez ich otwierania."""
    def _stat_pair(path: Path) -> Tuple[float, int]:
        if not path.exists():
            return 0.0, 0
        st = path.stat()
        return st.st_mtime, st.st_size

    return _stat_pair(DEFAULT_CFG_PATH), _stat_pair(LOCAL_CFG_PATH)


class HotReloadGateConfigManager:
    """
    Zarządza konfiguracją klienta bramy w pamięci RAM.
    Sprawdza sygnaturę plików (mtime, size) nie częściej niż co `check_interval_sec`.
    W razie wykrycia zmian przeładowuje ustawienia i powiadamia zarejestrowane moduły.
    """

    def __init__(self, check_interval_sec: float = 2.0) -> None:
        self._lock = threading.Lock()
        self._check_interval_sec = check_interval_sec
        self._last_check_time: float = 0.0
        self._callbacks: List[ReloadCallback] = []
        self._settings: GateAppConfig = load_config(
            GateAppConfig, DEFAULT_CFG_PATH, LOCAL_CFG_PATH
        )
        self._file_signature = _get_files_signature()

    @property
    def settings(self) -> GateAppConfig:
        """Zwraca aktualnie aktywną, zwalidowaną konfigurację."""
        return self._settings

    def register_reload_callback(self, callback: ReloadCallback) -> None:
        """Rejestruje funkcję wywoływaną po każdym udanym przeładowaniu konfiguracji."""
        with self._lock:
            self._callbacks.append(callback)

    def check_and_reload(self, force: bool = False) -> bool:
        """
        Sprawdza, czy pliki konfiguracyjne uległy modyfikacji i bezpiecznie je przeładowuje.
        Zwraca True, jeśli załadowano nową wersję konfiguracji.
        """
        now = time.monotonic()
        if not force and (now - self._last_check_time) < self._check_interval_sec:
            return False

        self._last_check_time = now
        current_sig = _get_files_signature()
        if current_sig == self._file_signature:
            return False

        with self._lock:
            current_sig = _get_files_signature()
            if current_sig == self._file_signature:
                return False

            try:
                new_cfg = load_config(GateAppConfig, DEFAULT_CFG_PATH, LOCAL_CFG_PATH)
            except Exception as err:
                logger.error(
                    f"Błąd składni lub walidacji przy przeładowywaniu konfiguracji "
                    f"(zachowano poprzedni stan): {err}"
                )
                self._file_signature = current_sig
                return False

            old_cfg = self._settings
            self._settings = new_cfg
            self._file_signature = current_sig
            callbacks = list(self._callbacks)

        logger.info("Wykryto zmianę konfiguracji – przeładowano ustawienia bramy na gorąco.")

        if old_cfg.http_server.port != new_cfg.http_server.port:
            logger.warning("Zmiana portu serwera HTTP wymaga restartu usługi.")
        if old_cfg.rfid.serial_port != new_cfg.rfid.serial_port:
            logger.warning("Zmiana portu szeregowego RFID wymaga restartu usługi.")

        for cb in callbacks:
            try:
                cb(old_cfg, new_cfg)
            except Exception as err:
                logger.error(f"Błąd w callbacku po przeładowaniu konfiguracji: {err}")

        return True


config_manager = HotReloadGateConfigManager(check_interval_sec=2.0)
