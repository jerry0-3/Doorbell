"""
Modele konfiguracyjne Pydantic dla serwera autoryzacji oraz menedżer
gorącego przeładowywania (hot-reload) listy kart i konfiguracji bram bez restartu.
"""

import threading
from pathlib import Path
from typing import Dict, List, Optional, Set

from pydantic import BaseModel, Field, field_validator

from common.config_loader import load_config
from common.crypto import AESCipher


class GateNodeConfig(BaseModel):
    """Konfiguracja pojedynczej bramy/domofonu."""
    name: str
    enabled: bool = True
    aes_key_pem_path: Optional[str] = None
    extra_allowed_tags: Set[str] = set()

    @field_validator("extra_allowed_tags", mode="before")
    @classmethod
    def normalize_extra_tags(cls, tags: List[str]) -> Set[str]:
        return {str(t).strip().upper().zfill(12) for t in tags}


class AuthServerConfig(BaseModel):
    log_path: str
    bind_host: str = "0.0.0.0"
    bind_port: int = Field(default=5005, ge=1, le=65535)
    client_timeout_sec: float = 5.0
    max_time_drift_sec: float = 12.0
    default_aes_key_pem_path: str
    gates: Dict[str, GateNodeConfig]
    allowed_tags: Set[str] = set()

    @field_validator("allowed_tags", mode="before")
    @classmethod
    def normalize_tags(cls, tags: List[str]) -> Set[str]:
        normalized = set()
        for tag in tags:
            clean = str(tag).strip().upper()
            int(clean, 16)
            normalized.add(clean.zfill(12))
        return normalized


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CFG_PATH = BASE_DIR / "config.default.json"
LOCAL_CFG_PATH = BASE_DIR / "config.local.json"


def _get_files_mtime() -> float:
    """Zwraca najnowszy czas modyfikacji plików konfiguracyjnych (koszt ~2 mikrosekundy)."""
    mtime = DEFAULT_CFG_PATH.stat().st_mtime if DEFAULT_CFG_PATH.exists() else 0.0
    if LOCAL_CFG_PATH.exists():
        mtime = max(mtime, LOCAL_CFG_PATH.stat().st_mtime)
    return mtime


class HotReloadAuthManager:
    """
    Zarządza konfiguracją i listą dozwolonych kart w pamięci RAM.
    Automatycznie przeładowuje pliki konfiguracyjne i klucze .pem wyłącznie wtedy,
    gdy zmieni się ich data modyfikacji na dysku (st_mtime).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_mtime: float = 0.0
        self.config: AuthServerConfig = load_config(
            AuthServerConfig, DEFAULT_CFG_PATH, LOCAL_CFG_PATH
        )
        self._ciphers: Dict[str, AESCipher] = {}
        self._reload_state()

    def _reload_state(self) -> None:
        """Wczytuje konfigurację i inicjalizuje szyfratory AES z plików .pem."""
        cfg = load_config(AuthServerConfig, DEFAULT_CFG_PATH, LOCAL_CFG_PATH)
        default_cipher = AESCipher.from_pem_file(cfg.default_aes_key_pem_path)

        ciphers: Dict[str, AESCipher] = {}
        for ip, gate in cfg.gates.items():
            if gate.aes_key_pem_path:
                ciphers[ip] = AESCipher.from_pem_file(gate.aes_key_pem_path)
            else:
                ciphers[ip] = default_cipher

        self.config = cfg
        self._ciphers = ciphers
        self._last_mtime = _get_files_mtime()

    def ensure_up_to_date(self) -> None:
        """Sprawdza, czy plik z kartami uległ zmianie i bezpiecznie przeładowuje pamięć RAM."""
        current_mtime = _get_files_mtime()
        if current_mtime != self._last_mtime:
            with self._lock:
                if current_mtime != self._last_mtime:
                    try:
                        self._reload_state()
                    except Exception as err:
                        import logging
                        logging.getLogger("acsrv_log").error(
                            f"Błąd składni podczas hot-reloadu konfiguracji (zachowano stary stan): {err}"
                        )
                        # Aktualizujemy mtime, aby nie próbować w kółko czytać uszkodzonego pliku
                        self._last_mtime = current_mtime

    def get_gate_and_cipher(self, client_ip: str):
        """Zwraca (GateNodeConfig, AESCipher) dla danego adresu IP bramy."""
        self.ensure_up_to_date()
        gate = self.config.gates.get(client_ip)
        cipher = self._ciphers.get(client_ip)
        return gate, cipher

    def is_tag_authorized(self, tag_hex: str, gate: GateNodeConfig) -> bool:
        """Sprawdza w czasie O(1), czy karta jest uprawniona do otwarcia danej bramy."""
        normalized = tag_hex.strip().upper().zfill(12)
        return (
                normalized in self.config.allowed_tags
                or normalized in gate.extra_allowed_tags
        )


auth_manager = HotReloadAuthManager()
