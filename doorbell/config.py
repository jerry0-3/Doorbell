"""
Konfiguracja parametrów sieciowych, GPIO i ścieżek systemu domofonu.
"""

# Konfiguracja brokera powiadomień (np. ntfy)
BROKER_URL = "http://10.47.34.96:8080"
TOPIC = "doorbell_control"
SERVER_IP = "10.47.34.126"
SERVER_PORT = 6000

# Bezpieczeństwo i tokeny
TOKEN_TTL_SECONDS = 300.0  # Token ważny 5 minut od zadzwonienia

# Konfiguracja SIP / Linphone
DEFAULT_DIAL_NUMBER = "42"
LINPHONE_LOG_PATH = "/home/greg/linph.log"
APP_LOG_PATH = "/var/log/gregac/vbram_log"

# Konfiguracja nowego libgpiod v2 (/dev/gpiochipX)
GPIO_CHIP_PATH = "/dev/gpiochip1"
GPIO_CONSUMER_NAME = "gregbl0"

LINE_BUTTON = 13   # Przycisk dzwonka (INPUT)
LINE_LED = 15      # Dioda kontrolna (OUTPUT, active-low)
LINE_GATE = 16     # Przekaźnik bramy (OUTPUT)

# Ścieżki do plików audio
SOUND_SILENCE = "/home/greg/Brama/cisza.wav"
SOUND_CONFIRM = "/home/greg/Brama/pi-pi4.5p.wav"
SOUND_GATE_OPEN = "/home/greg/Brama/BramaOtw.wav"

# Czasy sterowania
GATE_HOLD_TIME_SEC = 5.0
