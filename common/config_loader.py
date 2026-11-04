"""
Uniwersalny loader konfiguracji łączący plik domyślny (config.default.json)
z plikiem lokalnym (config.local.json) przy użyciu walidacji Pydantic.
"""

import json
from pathlib import Path
from typing import Any, Dict, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rekurencyjnie nadpisuje słownik 'base' wartościami ze słownika 'override'.
    Pozwala na częściowe nadpisywanie zagnieżdżonych sekcji w config.local.json.
    """
    merged = base.copy()
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(model_class: Type[T], default_path: Path, local_path: Path) -> T:
    """
    Wczytuje konfigurację domyślną, nakłada na nią zmiany z konfiguracji lokalnej
    (jeśli plik istnieje) i zwraca zwalidowany obiekt modelu Pydantic.
    """
    if not default_path.is_file():
        raise FileNotFoundError(f"Brak domyślnego pliku konfiguracji: {default_path}")

    with open(default_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)

    if local_path.is_file():
        with open(local_path, "r", encoding="utf-8") as f:
            local_data = json.load(f)
        config_data = deep_merge(config_data, local_data)

    return model_class.model_validate(config_data)
