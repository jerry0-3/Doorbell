"""
Zarządca tokenów autoryzacyjnych w pamięci RAM z obsługą TTL.
Eliminuje potrzebę zapisu na kartę SD i jest w pełni bezpieczny wątkowo.
"""

import threading
import time
from typing import Dict

from config import TOKEN_TTL_SECONDS

_tokens_lock = threading.Lock()
# Słownik: {token: czas_wygasniecia_unix}
_active_tokens: Dict[str, float] = {}


def register_token(token: str) -> None:
    """Rejestruje nowy unikalny token z czasem wygaśnięcia."""
    expire_at = time.time() + TOKEN_TTL_SECONDS
    with _tokens_lock:
        # Usuwamy przedawnione tokeny przy okazji dodawania nowego
        now = time.time()
        expired = [t for t, exp in _active_tokens.items() if exp < now]
        for t in expired:
            del _active_tokens[t]

        _active_tokens[token] = expire_at


def validate_token(token: str) -> bool:
    """Sprawdza poprawność tokenu bez jego unieważniania (do akcji 'call')."""
    now = time.time()
    with _tokens_lock:
        if token not in _active_tokens:
            return False

        if _active_tokens[token] < now:
            del _active_tokens[token]
            return False

        return True


def consume_token(token: str) -> bool:
    """
    Weryfikuje poprawność tokenu.
    Jeśli jest poprawny i aktywny – natychmiast go usuwa (zużywa) i zwraca True.
    """
    now = time.time()
    with _tokens_lock:
        if token not in _active_tokens:
            return False

        if _active_tokens[token] < now:
            del _active_tokens[token]
            return False

        # Token prawidłowy — usuwamy, aby nie dało się go użyć ponownie
        del _active_tokens[token]
        return True
