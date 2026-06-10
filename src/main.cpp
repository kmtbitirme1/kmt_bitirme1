#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHTesp.h>

// ── Pin tanimlari ─────────────────────────────────────────────
#define DHT_PIN   15
#define GREEN_LED 13
#define RELAY_PIN 32  // IRLZ44N Gate — HIGH=pompa acik, LOW=kapali

// ── WiFi ──────────────────────────────────────────────────────
// TODO: gercek deger placeholder. Repo public ise burayi commit etme.
const char* ssid     = "WIFI_ADI";
const char* password = "WIFI_SIFRESI";

// ── Render backend ────────────────────────────────────────────
const char* BACKEND_URL  = "https://kmt-bitirme1.onrender.com";
const char* INGEST_TOKEN = ""; // Render'da token ayarlanmadi — bos birak

// ── Esik degerleri ────────────────────────────────────────────
const float HUMIDITY_THRESHOLD = 60.0;  // % altinda pompa acilir
const float TEMP_THRESHOLD     = 30.0;  // C ustunde yesil LED yanar

// ── Nesneler ──────────────────────────────────────────────────
DHTesp dht;

// ── Global durum ──────────────────────────────────────────────
float humidity    = 0;
float temperature = 0;
bool  dhtValid    = false;
bool  pumpActive  = false;
bool  pumpManual  = false;
bool  greenLed    = false;

unsigned long lastCycle = 0;
const unsigned long CYCLE_INTERVAL = 3000; // 3 sn: oku + backend'e gonder + komut al

// ── MOSFET (IRLZ44N) kontrolu ─────────────────────────────────
void controlRelay() {
    if (!pumpManual) {
        pumpActive = dhtValid && (humidity < HUMIDITY_THRESHOLD);
    }
    digitalWrite(RELAY_PIN, pumpActive ? HIGH : LOW);
}

// ── LED kontrolu ──────────────────────────────────────────────
void updateLED() {
    greenLed = dhtValid && temperature > TEMP_THRESHOLD;
    digitalWrite(GREEN_LED, greenLed ? HIGH : LOW);
}

// ── Sensor okuma ──────────────────────────────────────────────
void readSensors() {
    TempAndHumidity data = dht.getTempAndHumidity();
    dhtValid = (dht.getStatus() == DHTesp::ERROR_NONE);
    if (dhtValid) {
        humidity    = data.humidity;
        temperature = data.temperature;
    }
}

// ── Backend'den gelen komutu uygula ───────────────────────────
// "on"=manuel ac, "off"=manuel kapat, "auto"=otomatik moda don
void applyCommand(const char* cmd) {
    if (strcmp(cmd, "on") == 0) {
        pumpManual = true;  pumpActive = true;
    } else if (strcmp(cmd, "off") == 0) {
        pumpManual = true;  pumpActive = false;
    } else if (strcmp(cmd, "auto") == 0) {
        pumpManual = false;
    }
    digitalWrite(RELAY_PIN, pumpActive ? HIGH : LOW);
}

// ── Backend'e veri gonder, cevaptaki komutu uygula ────────────
void sendToBackend() {
    if (WiFi.status() != WL_CONNECTED) return;

    WiFiClientSecure client;
    client.setInsecure(); // Render sertifikasini dogrulama (basit; odev icin yeterli)

    HTTPClient http;
    http.begin(client, String(BACKEND_URL) + "/ingest");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("x-token", INGEST_TOKEN);

    // Govde olustur
    StaticJsonDocument<256> doc;
    if (dhtValid) {
        doc["humidity"]    = humidity;
        doc["temperature"] = temperature;
    }
    doc["pump"]       = pumpActive;
    doc["pumpManual"] = pumpManual;
    doc["greenLed"]   = greenLed;

    String body;
    serializeJson(doc, body);

    int code = http.POST(body);
    if (code == 200) {
        String resp = http.getString();
        StaticJsonDocument<128> rdoc;
        if (deserializeJson(rdoc, resp) == DeserializationError::Ok) {
            const char* cmd = rdoc["command"]; // null olabilir
            if (cmd != nullptr) {
                applyCommand(cmd);
                Serial.printf("[KOMUT] %s uygulandi\n", cmd);
            }
        }
    } else {
        Serial.printf("[UYARI] backend POST hata: %d\n", code);
    }
    http.end();
}

// ── Setup ─────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);

    pinMode(GREEN_LED, OUTPUT);
    pinMode(RELAY_PIN, OUTPUT);
    digitalWrite(GREEN_LED, LOW);
    digitalWrite(RELAY_PIN, LOW);

    pinMode(2, OUTPUT);
    for (int i = 0; i < 5; i++) {
        digitalWrite(2, HIGH); delay(150);
        digitalWrite(2, LOW);  delay(150);
    }

    dht.setup(DHT_PIN, DHTesp::DHT22);

    WiFi.begin(ssid, password);
    Serial.print("WiFi baglaniliyor");
    while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
    Serial.println("\nBaglandi! IP: " + WiFi.localIP().toString());
    Serial.println("Backend: " + String(BACKEND_URL));
}

// ── Loop ──────────────────────────────────────────────────────
void loop() {
    if (millis() - lastCycle >= CYCLE_INTERVAL) {
        lastCycle = millis();

        readSensors();
        controlRelay();
        updateLED();
        sendToBackend();

        if (!dhtValid) {
            Serial.println("[UYARI] DHT22 okunamiyor!");
        } else {
            Serial.printf("Nem: %.1f%%  |  Sicaklik: %.1f C  |  Pompa: %s%s\n",
                humidity, temperature,
                pumpActive ? "ACIK" : "KAPALI",
                pumpManual ? " [Manuel]" : "");
        }
    }
}
