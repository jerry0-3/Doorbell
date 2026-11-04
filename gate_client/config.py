"""
Modele konfiguracyjne Pydantic dla klienta bramy/domofonu.
"""

from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field
from common.config_loader import load_config


class HttpServerConfig(BaseModel):
    ip: str
    port: int = Field(ge=1, le=65535)


class NotificationConfig(BaseModel):
    broker_url: str
    topic: str
    priority: int = Field(default=5, ge=1, le=5)


class SipConfig(BaseModel):
    default_dial_number: str
    linphone_log_path: str
    magic_dtmf_regex: str
    max_inter_digit_sec: float = Field(gt=0.0)


class GpioConfig(BaseModel):
    chip_path: str
    consumer_name: str
    line_button: int
    line_led_red: int
    line_led_blue: int
    line_gate: int
    line_led_green: Optional[int] = None
    line_buzzer: int
    buzzer_active_low: bool = False


class SoundsConfig(BaseModel):
    silence: str
    confirm: str
    reject: str
    gate_open: str


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
    line_sensor: int = 19
    line_led: int = 20
    sensor_active_low: bool = True
    led_active_low: bool = True
    debounce_sec: float = Field(default=2.0, gt=0.0)
    topic: str = "mailbox_alerts"
    priority: int = Field(default=3, ge=1, le=5)
    title: str = "Skrzynka na listy"
    message: str = "W skrzynce znajduje się nowa korespondencja."


class GateAppConfig(BaseModel):
    app_log_path: str
    gate_hold_time_sec: float = Field(gt=0.0)
    token_ttl_seconds: float = Field(gt=0.0)
    http_server: HttpServerConfig
    notification: NotificationConfig
    sip: SipConfig
    gpio: GpioConfig
    sounds: SoundsConfig
    rfid: RfidConfig
    mailbox: MailboxConfig


BASE_DIR = Path(__file__).resolve().parent
settings: GateAppConfig = load_config(
    GateAppConfig,
    default_path=BASE_DIR / "config.default.json",
    local_path=BASE_DIR / "config.local.json"
)