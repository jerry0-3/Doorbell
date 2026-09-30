"""
Klient wysyłający powiadomienia interaktywne na telefon.
"""

import uuid
import logging
import requests

from config import BROKER_URL, TOPIC, SERVER_IP, SERVER_PORT
from token_store import register_token

logger = logging.getLogger('vbram_log')


def send_doorbell_notification() -> bool:
    """
    Generuje jednorazowy token i wysyła powiadomienie z opcjami:
      1. 'Otwórz bramę' (akcja HTTP w tle — bez otwierania apek ani dzwonienia)
      2. 'Rozmawiaj' (jedno kliknięcie: akcja VIEW otwiera aplikację Android,
         która sama wyzwala połączenie SIP i pokazuje pływające przyciski)
      3. 'Odrzuć' (akcja HTTP kasująca token)

    Zwraca:
        True jeśli broker przyjął powiadomienie, False w razie błędu sieci/brokera.
    """
    token = str(uuid.uuid4())[:8]
    register_token(token)

    # noinspection HttpUrlsUsage
    callback_url = f"http://{SERVER_IP}:{SERVER_PORT}/decision?token={token}"
    app_deep_link = f"doorbell://call?token={token}&host={SERVER_IP}&port={SERVER_PORT}"

    actions = [
        # Akcja 1: Błyskawiczne otwarcie z poziomu paska powiadomień
        {
            "action": "http",
            "label": "Otwórz bramę",
            "url": callback_url,
            "method": "POST",
            "body": '{"akcja": "start"}',
            "headers": {"Content-Type": "application/json"},
            "clear": True
        },
        # Akcja 2: Jedno kliknięcie -> otwiera overlay, który wysyła POST "call"
        {
            "action": "view",
            "label": "Rozmawiaj",
            "url": app_deep_link,
            "clear": True
        },
        # Akcja 3: Odrzucenie
        {
            "action": "http",
            "label": "Odrzuć",
            "url": callback_url,
            "method": "POST",
            "body": '{"akcja": "stop"}',
            "headers": {"Content-Type": "application/json"},
            "clear": True
        }
    ]

    payload = {
        "topic": TOPIC,
        "title": "Dzwonek do drzwi",
        "message": "Ktoś dzwoni do bramy. Wybierz akcję:",
        "priority": 5,
        "actions": actions
    }

    try:
        response = requests.post(BROKER_URL, json=payload, timeout=3.0)
        response.raise_for_status()
        logger.info(f"[Notifier] Powiadomienie wysłane pomyślnie. Token: {token}")
        return True
    except requests.RequestException as err:
        logger.error(f"[Notifier] Nie udało się wysłać powiadomienia do brokera: {err}")
        return False
