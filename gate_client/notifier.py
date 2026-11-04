"""
Uniwersalny klient powiadomień obsługujący dowolne tematy (topics),
priorytety oraz opcjonalne przyciski akcji na tym samym brokerze.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

import requests

from gate_client.config import settings
from gate_client.token_store import register_token

logger = logging.getLogger("vbram_log")


def send_notification(
        topic: str,
        title: str,
        message: str,
        priority: int = 3,
        actions: Optional[List[Dict[str, Any]]] = None
) -> bool:
    """
    Wysyła powiadomienie do skonfigurowanego brokera.
    Parametr 'actions' jest opcjonalny (np. brak akcji dla skrzynki na listy).
    """
    payload: Dict[str, Any] = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
    }
    if actions:
        payload["actions"] = actions

    try:
        response = requests.post(
            settings.notification.broker_url,
            json=payload,
            timeout=2.5
        )
        response.raise_for_status()
        logger.info(f"Wysłano powiadomienie na temat '{topic}' (priorytet={priority}).")
        return True
    except requests.RequestException as err:
        logger.error(f"Błąd wysyłania powiadomienia na temat '{topic}': {err}")
        return False


def send_doorbell_notification() -> bool:
    """Wysyła interaktywne powiadomienie o dzwonku do bramy z 3 akcjami."""
    token = str(uuid.uuid4())[:8]
    register_token(token)

    srv_ip = settings.http_server.ip
    srv_port = settings.http_server.port
    notif_cfg = settings.notification

    # noinspection HttpUrlsUsage
    callback_url = f"http://{srv_ip}:{srv_port}/decision?token={token}"
    app_deep_link = f"doorbell://call?token={token}&host={srv_ip}&port={srv_port}"

    actions = [
        {
            "action": "http",
            "label": "Otwórz bramę",
            "url": callback_url,
            "method": "POST",
            "body": '{"akcja": "start"}',
            "headers": {"Content-Type": "application/json"},
            "clear": True
        },
        {
            "action": "view",
            "label": "Rozmawiaj",
            "url": app_deep_link,
            "clear": True
        },
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

    return send_notification(
        topic=notif_cfg.topic,
        title="Dzwonek do drzwi",
        message="Ktoś dzwoni do bramy. Wybierz akcję:",
        priority=notif_cfg.priority,
        actions=actions
    )


def send_mailbox_notification() -> bool:
    """Wysyła bezakcyjne powiadomienie o wykryciu nowych listów w skrzynce."""
    mb_cfg = settings.mailbox
    return send_notification(
        topic=mb_cfg.topic,
        title=mb_cfg.title,
        message=mb_cfg.message,
        priority=mb_cfg.priority,
        actions=None
    )
