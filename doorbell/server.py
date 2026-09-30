"""
Moduł serwera HTTP Flask do odbierania autoryzowanych decyzji z powiadomień.
"""

import logging
from flask import Flask, jsonify, request
from config import SERVER_PORT, DEFAULT_DIAL_NUMBER
from hardware import open_gate
from lin_phone import dial_sip_number, is_linphone_on_hook
from token_store import consume_token, validate_token

logger = logging.getLogger('vbram_log')
app = Flask(__name__)


@app.route('/decision', methods=['POST'])
def receive_decision():
    """
    Obsługuje akcje:
      - 'start': otwiera bramę i zużywa token (z powiadomienia lub aplikacji mobilnej)
      - 'call': uruchamia połączenie SIP linphone na podany w ciele parametr 'number'
                (lub domyślny DEFAULT_DIAL_NUMBER w razie jego braku); token pozostaje aktywny dla nakładki
      - 'stop': unieważnia token, anuluje akcję
    """
    incoming_token = request.args.get('token', '').strip()
    body = request.get_json(silent=True) or {}
    action = body.get('akcja')

    logger.info(f"Odebrano żądanie HTTP: akcja='{action}', token='{incoming_token}'")

    if not incoming_token:
        return jsonify({"status": "error", "message": "Brak tokenu"}), 400

    # 1. Obsługa rozpoczęcia rozmowy SIP (token NIE jest usuwany)
    if action == 'call':
        if not validate_token(incoming_token):
            logger.warning(f"Odrzucono call: token '{incoming_token}' jest nieprawidłowy lub wygasł.")
            return jsonify({"status": "error", "message": "Token wygasł"}), 403

        # Pobieramy docelowy numer SIP z body zapytania
        target_number = body.get('number')
        if target_number:
            target_number = str(target_number).strip()

        # Jeśli brak parametru lub pusty string, używamy numeru domyślnego
        if not target_number:
            target_number = DEFAULT_DIAL_NUMBER

        print(f"Próba połączenia Linphone na numer: {target_number}")
        if is_linphone_on_hook():
            logger.info(f"Uruchamianie połączenia Linphone na numer: {target_number}")
            print(f"Uruchamianie połączenia Linphone na numer: {target_number}")
            dial_sip_number(target_number)
            return jsonify({"status": "ok", "message": f"Połączenie zainicjowane na numer {target_number}"}), 200
        else:
            logger.info("Linphone już prowadzi rozmowę.")
            return jsonify({"status": "ok", "message": "Linphone już aktywny"}), 200

    # 2. Akcje kończące (zużywające token): 'start' oraz 'stop'
    if not consume_token(incoming_token):
        logger.warning(f"Odrzucono: token '{incoming_token}' jest nieprawidłowy lub wygasł.")
        return jsonify({"status": "error", "message": "Token wygasł lub został już wykorzystany"}), 403

    if action == 'start':
        logger.info("Otwieranie bramy...")
        open_gate()
        return jsonify({"status": "ok", "message": "Brama została otwarta"}), 200

    elif action == 'stop':
        logger.info("Zgłoszenie anulowane/zamknięte przez użytkownika.")
        return jsonify({"status": "ok", "message": "Anulowano"}), 200

    return jsonify({"status": "ignored", "message": "Nieznana akcja"}), 400


def run_http_server() -> None:
    """Uruchamia ultralekki serwer HTTP z wyciszonymi logami dostępowymi."""
    # Wyłączenie hałaśliwych logów żądań Werkzeug
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    # threaded = False na 1 rdzeniu radzi sobie znakomicie z 1 zapytaniem co parę minut
    app.run(host='0.0.0.0', port=SERVER_PORT, threaded=True, debug=False, use_reloader=False)
