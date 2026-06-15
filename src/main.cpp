#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DHTesp.h>
#include <Preferences.h>
#include <Wire.h>
#include <Adafruit_BMP280.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>

// ── Pin tanimlari ─────────────────────────────────────────────
#define DHT_PIN   15
#define GREEN_LED 13
#define RELAY_PIN 32  // IRLZ44N Gate — HIGH=pompa acik, LOW=kapali

// ── Varsayilan WiFi (Preferences'ta kayit yoksa kullanilir) ───
#define DEFAULT_SSID     "TURKNET_B7776"
#define DEFAULT_PASSWORD "fhCNASdY"

// ── Cloud backend (Render) ────────────────────────────────────
#define BACKEND_URL "https://kmt-bitirme1.onrender.com"

// ── Esik degerleri (NVS'den yuklenir, web'den degistirilebilir) ─
float HUMIDITY_THRESHOLD = 60.0;  // % altinda pompa acilir
float TEMP_THRESHOLD     = 30.0;  // C ustunde yesil LED yanar

// ── Nesneler ──────────────────────────────────────────────────
DHTesp          dht;
WebServer       server(80);
Preferences     prefs;
Adafruit_BMP280 bmp; // I2C: SDA=GPIO21, SCL=GPIO22

// ── WiFi bilgileri (NVS'den yuklenir) ─────────────────────────
String wifiSSID     = DEFAULT_SSID;
String wifiPassword = DEFAULT_PASSWORD;

// ── Global durum ──────────────────────────────────────────────
float humidity    = 0;
float temperature = 0;
float pressure    = 0;   // hPa (BMP280)
bool  dhtValid    = false;
bool  bmpValid    = false;
bool  pumpActive  = false;
bool  pumpManual  = false;

// Backend (Python algoritma) ile en az bir kez konusuldu mu?
// Konusulmadiysa asagidaki yerel esik kontrolu failsafe olarak devrede kalir.
bool  algoCommandSeen = false;

// Sureli pompa: algoritma "su kadar saniye sula" der (pump_duration_seconds).
// pumpUntil != 0 ise pompa o ana kadar acik kalir, sonra otomatik kapanir.
unsigned long pumpUntil = 0;

unsigned long lastRead = 0;
const unsigned long READ_INTERVAL = 3000; // 3 sn: sensor + backend

// ── Sureli pompayi kontrol et (sure dolduysa kapat) ───────────
// loop icinde sik cagrilir; 1-8 sn'lik algoritma surelerini hassas uygular.
void serviceTimedPump() {
    if (pumpActive && pumpUntil != 0 && millis() >= pumpUntil) {
        pumpActive = false;
        pumpUntil  = 0;
        digitalWrite(RELAY_PIN, LOW);
        Serial.println("[POMPA] sure doldu, kapatildi");
    }
}

// ── MOSFET (IRLZ44N) kontrolu ─────────────────────────────────
void controlRelay() {
    serviceTimedPump();

    // Manuel mod VEYA sureli (algoritma) sulama devam ediyorsa karisma.
    if (pumpManual || pumpUntil != 0) return;

    // Otomatik modda backend komutu geldiyse karari algoritmaya birak.
    // Hic backend komutu gelmediyse yerel esik failsafe devreye girer.
    if (algoCommandSeen) return;
    pumpActive = dhtValid && (humidity < HUMIDITY_THRESHOLD);
    digitalWrite(RELAY_PIN, pumpActive ? HIGH : LOW);
}

// ── LED kontrolu ──────────────────────────────────────────────
void updateLED() {
    digitalWrite(GREEN_LED, (dhtValid && temperature > TEMP_THRESHOLD) ? HIGH : LOW);
}

// ── Sensor okuma ──────────────────────────────────────────────
void readSensors() {
    // DHT22
    TempAndHumidity data = dht.getTempAndHumidity();
    dhtValid = (dht.getStatus() == DHTesp::ERROR_NONE);
    if (dhtValid) {
        humidity    = data.humidity;
        temperature = data.temperature;
    }
    // BMP280 — bmpValid false ise sensör bulunamadi demektir
    if (bmpValid) {
        pressure = bmp.readPressure() / 100.0F; // Pa → hPa
    }
}

// ── Backend'e veri gonder, cevaptaki komutu uygula ────────────
// POST /ingest → sensor verisi gonder
// Yanit icinde "command" alani varsa pompayi komuta gore kontrol et
void sendToBackend() {
    if (WiFi.status() != WL_CONNECTED) return;

    WiFiClientSecure client;
    client.setInsecure(); // SSL sertifika dogrulamasi atlaniyor (prototip icin yeterli)

    HTTPClient http;
    http.begin(client, BACKEND_URL "/ingest");
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(5000);

    StaticJsonDocument<256> doc;
    if (dhtValid) {
        doc["humidity"]    = humidity;
        doc["temperature"] = temperature;
    }
    if (bmpValid) {
        doc["pressure"] = pressure;
    }
    doc["pump"]       = pumpActive;
    doc["pumpManual"] = pumpManual;
    doc["greenLed"]   = (dhtValid && temperature > TEMP_THRESHOLD);

    String body;
    serializeJson(doc, body);

    int code = http.POST(body);
    if (code == 200) {
        StaticJsonDocument<384> rdoc;
        if (deserializeJson(rdoc, http.getString()) == DeserializationError::Ok) {
            // Eşik ayarlarını (config) çek ve gerekirse NVS'e kaydet
            JsonVariant cfg = rdoc["config"];
            if (cfg.is<JsonObject>()) {
                float newHum = cfg["humThreshold"] | HUMIDITY_THRESHOLD;
                float newTemp = cfg["tempThreshold"] | TEMP_THRESHOLD;
                bool changed = false;
                if (abs(newHum - HUMIDITY_THRESHOLD) > 0.1) {
                    HUMIDITY_THRESHOLD = newHum;
                    prefs.putFloat("humThresh", HUMIDITY_THRESHOLD);
                    changed = true;
                }
                if (abs(newTemp - TEMP_THRESHOLD) > 0.1) {
                    TEMP_THRESHOLD = newTemp;
                    prefs.putFloat("tmpThresh", TEMP_THRESHOLD);
                    changed = true;
                }
                if (changed) {
                    Serial.printf("[AYAR] Sunucudan yeni esik alindi: Nem=%.1f, Sicaklik=%.1f\n", HUMIDITY_THRESHOLD, TEMP_THRESHOLD);
                }
            }

            // command artik bir nesne: { "command": "on", "durationSeconds": 4 }
            // (eski string bicimine de geriye donuk uyum)
            JsonVariant node = rdoc["command"];
            const char* cmd = nullptr;
            float dur = 0.0f;
            if (node.is<JsonObject>()) {
                cmd = node["command"];
                dur = node["durationSeconds"] | 0.0f;
            } else if (node.is<const char*>()) {
                cmd = node.as<const char*>();
            }

            if (cmd != nullptr) {
                algoCommandSeen = true; // backend ile iletisim kuruldu

                if (strcmp(cmd, "on") == 0) {
                    pumpActive = true;
                    digitalWrite(RELAY_PIN, HIGH);
                    if (dur > 0) {
                        // Algoritma sureli sulama: manuel moda gecirme.
                        pumpUntil = millis() + (unsigned long)(dur * 1000.0);
                    } else {
                        // Sure yok -> manuel sinirsiz ON.
                        pumpManual = true;
                        pumpUntil  = 0;
                    }
                } else if (strcmp(cmd, "off") == 0) {
                    pumpActive = false;
                    pumpUntil  = 0;
                    digitalWrite(RELAY_PIN, LOW);
                    if (dur == 0) pumpManual = true; // manuel OFF; sureli OFF'ta otomatik kalir
                } else if (strcmp(cmd, "auto") == 0) {
                    pumpManual = false;
                    pumpUntil  = 0;
                    // Backend "auto" komutu gelince de hemen esige gore uygula.
                    pumpActive = dhtValid && (humidity < HUMIDITY_THRESHOLD);
                    digitalWrite(RELAY_PIN, pumpActive ? HIGH : LOW);
                }
                Serial.printf("[CLOUD] Komut: %s (%.2f sn)\n", cmd, dur);
            }
        }
    } else if (code < 0) {
        Serial.println("[CLOUD] Baglanti hatasi: " + http.errorToString(code));
    }
    http.end();
}

// ╔══════════════════════════════════════════════════════════════╗
// ║              WEB ARAYÜZÜ — GELİŞTİRİCİ NOTU               ║
// ║                                                              ║
// ║  Bu fonksiyon ESP32 üzerindeki yerel web sunucusunun        ║
// ║  her istekte oluşturduğu HTML sayfasını döndürür.           ║
// ║                                                              ║
// ║  Mimari:                                                     ║
// ║  • Sayfa 3 saniyede bir otomatik yenilenir (meta refresh)   ║
// ║  • Tüm veriler global değişkenlerden okunur                 ║
// ║  • Renk mantığı: yeşil = normal, kırmızı = eşik aşıldı     ║
// ║  • Asıl arayüz GitHub Pages'te, bu sayfa yerel yedektir    ║
// ║                                                              ║
// ║  Geliştirme önerileri:                                      ║
// ║  • meta refresh yerine JavaScript fetch() + JSON API        ║
// ║    kullanılabilir (daha akıcı deneyim)                      ║
// ║  • /api endpoint JSON döndürür, harici uygulama             ║
// ║    bu adresten veri çekebilir                               ║
// ║  • CSS ve HTML ayrı dosyaya taşınabilir (SPIFFS)            ║
// ╚══════════════════════════════════════════════════════════════╝
String buildPage() {

    // ── <head> ve CSS ─────────────────────────────────────────
    // Tüm stiller tek satırda gömülü (harici CSS dosyası yok)
    // Geliştiriciler buraya yeni class ekleyebilir
    String html = F("<!DOCTYPE html><html lang='tr'><head>"
        "<meta charset='UTF-8'>"
        "<meta http-equiv='refresh' content='3'>"          // 3 sn otomatik yenile
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Akilli Tarim</title>"
        "<style>"
        "body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;margin:0;padding:24px}"
        "h1{color:#4ecca3;text-align:center}"              /* Ana başlık rengi */
        "p.sub{text-align:center;color:#888;font-size:.85em;margin-top:-10px}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;max-width:900px;margin:24px auto}"
        ".card{background:#16213e;border-radius:12px;padding:24px;text-align:center}"
        ".val{font-size:2.2em;font-weight:700;color:#4ecca3;margin:10px 0}" /* Kart değer rengi */
        ".lbl{font-size:.85em;color:#aaa}"                 /* Kart başlık rengi */
        ".warn{color:#ff6b6b!important}"                   /* Uyarı rengi — eşik aşıldığında */
        ".btn{padding:10px 20px;margin:6px;border:none;border-radius:8px;font-weight:700;cursor:pointer}"
        ".btn-set{padding:8px 18px;background:#555;color:#fff;border:none;border-radius:8px;font-weight:700;cursor:pointer;font-size:.85em}" /* Ayar butonu */
        "footer{text-align:center;color:#555;font-size:.78em;margin-top:28px}"
        "</style></head><body>");

    // ── Sayfa başlığı ─────────────────────────────────────────
    // WiFi.localIP() → ESP32'nin ağdaki IP adresi
    html += F("<h1>&#127807; Akilli Tarim</h1>");
    html += "<p class='sub'>IP: " + WiFi.localIP().toString() +
            " &nbsp;|&nbsp; WiFi: " + wifiSSID + "</p>";

    // ── Kart ızgarası ─────────────────────────────────────────
    html += F("<div class='grid'>");

    // KART 1: Nem
    // nemRenk → nem eşiğin üstündeyse normal (yeşil), altındaysa uyarı (kırmızı)
    // dhtValid → sensör okunabiliyorsa değer göster, değilse "---"
    String nemRenk = (dhtValid && humidity > HUMIDITY_THRESHOLD) ? "" : " warn";
    html += "<div class='card'><div class='lbl'>Nem</div>"
            "<div class='val" + nemRenk + "'>" +
            (dhtValid ? String(humidity, 1) + "%" : "---") + "</div>"
            "<div class='lbl'>Esik: %" + String(HUMIDITY_THRESHOLD, 0) + "</div></div>";

    // KART 2: Sıcaklık
    // sicRenk → sıcaklık eşiğin üstündeyse uyarı (kırmızı)
    String sicRenk = (dhtValid && temperature > TEMP_THRESHOLD) ? " warn" : "";
    html += "<div class='card'><div class='lbl'>Sicaklik</div>"
            "<div class='val" + sicRenk + "'>" +
            (dhtValid ? String(temperature, 1) + " C" : "---") + "</div>"
            "<div class='lbl'>Esik: " + String(TEMP_THRESHOLD, 0) + " C</div></div>";

    // KART 3: Yeşil LED durumu
    // Sıcaklık > eşik → LED YANIYOR, değilse KAPALI
    String ledDurum = (dhtValid && temperature > TEMP_THRESHOLD) ? "YANIYOR" : "KAPALI";
    html += "<div class='card'><div class='lbl'>Yesil LED</div>"
            "<div class='val'>" + ledDurum + "</div></div>";

    // KART 4: Basınç (BMP280)
    // bmpValid → sensör bulundu mu; bulunamadiysa "BAGLI DEGIL" gosterilir
    html += "<div class='card'><div class='lbl'>Basinc</div>"
            "<div class='val'>" +
            (bmpValid ? String(pressure, 1) + " hPa" : "<span style='font-size:.55em;color:#ff6b6b'>BAGLI DEGIL</span>") +
            "</div>"
            "<div class='lbl'>BMP280</div></div>";

    // KART 5: Pompa / Röle durumu
    // pumpActive → röle tetiklendi mi
    // pumpManual → web'den manuel mi kontrol ediliyor, otomatik mi
    String pumpRenk = pumpActive ? " warn" : "";
    html += "<div class='card'><div class='lbl'>Pompa</div>"
            "<div class='val" + pumpRenk + "'>" + String(pumpActive ? "ACIK" : "KAPALI") + "</div>"
            "<div class='lbl'>" + String(pumpManual ? "Manuel" : "Otomatik") + "</div></div>";

    html += F("</div>"); // grid kapanışı

    // ── Manuel Kontrol Butonları ──────────────────────────────
    // Her buton ayrı <form> içinde — GET isteği gönderir
    // /pump/on   → pumpManual=true,  pompa zorla açık
    // /pump/off  → pumpManual=true,  pompa zorla kapalı
    // /pump/auto → pumpManual=false, nem eşiğine göre otomatik
    html += F("<div style='text-align:center;margin-top:16px'>"
        "<form action='/pump/on' method='get' style='display:inline'>"
        "<button class='btn' style='background:#4ecca3;color:#000'>Pompa AC</button></form>"
        "<form action='/pump/off' method='get' style='display:inline'>"
        "<button class='btn' style='background:#ff6b6b;color:#fff'>Pompa KAPAT</button></form>"
        "<form action='/pump/auto' method='get' style='display:inline'>"
        "<button class='btn' style='background:#a29bfe;color:#fff'>Otomatik</button></form>"
        "</div>");

    // ── Ayar Butonları ────────────────────────────────────────
    // /thresh → Nem ve sıcaklık eşik değerlerini değiştir
    // /wifi   → WiFi SSID ve şifresini değiştir
    html += F("<div style='text-align:center;margin-top:12px'>"
        "<form action='/thresh' method='get' style='display:inline'>"
        "<button class='btn-set'>&#127777; Esik Ayarlari</button></form>"
        "&nbsp;&nbsp;"
        "<form action='/wifi' method='get' style='display:inline'>"
        "<button class='btn-set'>&#128246; WiFi Ayarlari</button></form>"
        "</div>"
        "<footer>Sayfa 3 sn yenilenir</footer></body></html>");

    return html;
}

// ── Eşik Ayarları Sayfası ─────────────────────────────────────
// /thresh → Nem ve sicaklik esik degerlerini degistir
// Degisiklik aninda gecerli olur, yeniden baslatma gerekmez
String buildThreshPage(const String& msg = "") {
    String html = F("<!DOCTYPE html><html lang='tr'><head>"
        "<meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Esik Ayarlari</title>"
        "<style>"
        "body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;margin:0;padding:24px}"
        "h1{color:#4ecca3;text-align:center}"
        ".box{background:#16213e;border-radius:12px;padding:28px;max-width:420px;margin:24px auto}"
        "label{display:block;margin-bottom:5px;color:#aaa;font-size:.88em}"
        "input[type=number]{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;"
        "border:1px solid #333;background:#0f3460;color:#eee;font-size:1em;margin-bottom:14px}"
        ".row{display:grid;grid-template-columns:1fr 1fr;gap:12px}"
        ".btn{width:100%;padding:12px;background:#4ecca3;color:#000;border:none;"
        "border-radius:8px;font-weight:700;font-size:1em;cursor:pointer}"
        ".back{display:block;text-align:center;margin-top:14px;color:#888;font-size:.85em;text-decoration:none}"
        ".ok{text-align:center;padding:9px;border-radius:8px;margin-bottom:14px;background:#2d5a27;color:#7dff7d;font-size:.88em}"
        "</style></head><body>"
        "<h1>&#127777; Esik Ayarlari</h1>"
        "<div class='box'>");

    if (msg.length() > 0) html += "<div class='ok'>" + msg + "</div>";

    html += "<form action='/save-thresh' method='get'>"
            "<div class='row'>"
            "<div><label>Nem Esigi (%)</label>"
            "<input type='number' name='hum' min='1' max='100' step='1' value='" + String((int)HUMIDITY_THRESHOLD) + "' required></div>"
            "<div><label>Sicaklik Esigi (C)</label>"
            "<input type='number' name='tmp' min='-10' max='80' step='1' value='" + String((int)TEMP_THRESHOLD) + "' required></div>"
            "</div>"
            "<button class='btn' type='submit'>Kaydet</button>"
            "</form>"
            "</div>"
            "<a class='back' href='/'>&#8592; Ana Sayfaya Don</a>"
            "</body></html>";
    return html;
}

// ── WiFi Ayarları Sayfası ─────────────────────────────────────
// /wifi → SSID ve sifre girmek icin form gosterir
// Kaydedince ESP32 yeniden baslar; yeni IP Serial Monitor'dan okunur
String buildWifiPage(const String& msg = "") {
    String html = F("<!DOCTYPE html><html lang='tr'><head>"
        "<meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>WiFi Ayarlari</title>"
        "<style>"
        "body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;margin:0;padding:24px}"
        "h1{color:#4ecca3;text-align:center}"
        ".box{background:#16213e;border-radius:12px;padding:28px;max-width:420px;margin:24px auto}"
        "label{display:block;margin-bottom:5px;color:#aaa;font-size:.88em}"
        "input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;"
        "border:1px solid #333;background:#0f3460;color:#eee;font-size:1em;margin-bottom:14px}"
        ".btn{width:100%;padding:12px;background:#555;color:#fff;border:none;"
        "border-radius:8px;font-weight:700;font-size:1em;cursor:pointer}"
        ".back{display:block;text-align:center;margin-top:14px;color:#888;font-size:.85em;text-decoration:none}"
        ".err{text-align:center;padding:9px;border-radius:8px;margin-bottom:14px;background:#5a2d2d;color:#ff9b9b;font-size:.88em}"
        "</style></head><body>"
        "<h1>&#128246; WiFi Ayarlari</h1>"
        "<div class='box'>");

    if (msg.length() > 0) html += "<div class='err'>" + msg + "</div>";

    html += "<form action='/save' method='get'>"
            "<label>WiFi Adi (SSID)</label>"
            "<input type='text' name='ssid' placeholder='WiFi aginizin adi' value='" + wifiSSID + "' required>"
            "<label>Sifre</label>"
            "<input type='password' name='pass' placeholder='WiFi sifreniz' value='" + wifiPassword + "'>"
            "<button class='btn' type='submit'>Kaydet ve Yeniden Baslat</button>"
            "</form>"
            "</div>"
            "<a class='back' href='/'>&#8592; Ana Sayfaya Don</a>"
            "</body></html>";
    return html;
}

// ── HTTP Handler'lar ──────────────────────────────────────────
// Tarayıcıdan gelen GET isteklerini karşılar
// Her handler işlemi yapıp ana sayfaya (/) yönlendirir (302 redirect)

// GET / → Ana sayfayı döndür
void handleRoot() { server.send(200, "text/html", buildPage()); }

// GET /thresh → Esik ayarlari sayfasini goster
void handleThresh() { server.send(200, "text/html", buildThreshPage()); }

// GET /wifi → WiFi ayarlari sayfasini goster
void handleWifi() { server.send(200, "text/html", buildWifiPage()); }

// GET /save-thresh?hum=X&tmp=Y → Esik degerlerini NVS'e yaz, aninda uygula
void handleSaveThresh() {
    if (server.hasArg("hum") && server.hasArg("tmp")) {
        HUMIDITY_THRESHOLD = server.arg("hum").toFloat();
        TEMP_THRESHOLD     = server.arg("tmp").toFloat();

        prefs.begin("thresh", false);
        prefs.putFloat("hum", HUMIDITY_THRESHOLD);
        prefs.putFloat("tmp", TEMP_THRESHOLD);
        prefs.end();

        Serial.printf("[AYAR] Yeni esikler — Nem: %.0f%%  Sicaklik: %.0f C\n",
                      HUMIDITY_THRESHOLD, TEMP_THRESHOLD);
        server.send(200, "text/html",
            buildThreshPage("Kaydedildi: Nem %" +
                String((int)HUMIDITY_THRESHOLD) + "  /  Sicaklik " +
                String((int)TEMP_THRESHOLD) + " C"));
    } else {
        server.send(200, "text/html", buildThreshPage("Gecersiz deger!"));
    }
}

// GET /save?ssid=X&pass=Y → Yeni WiFi bilgilerini NVS'e yaz, ESP32'yi yeniden baslatma
void handleSave() {
    if (server.hasArg("ssid") && server.arg("ssid").length() > 0) {
        String newSSID = server.arg("ssid");
        String newPass = server.arg("pass");

        prefs.begin("wifi", false);
        prefs.putString("ssid", newSSID);
        prefs.putString("pass", newPass);
        prefs.end();

        Serial.println("[AYAR] Yeni WiFi bilgileri kaydedildi: " + newSSID);
        Serial.println("[AYAR] Yeniden baslatiliyor...");

        String page = F("<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            "<meta http-equiv='refresh' content='3;url=/'>"
            "<style>body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
            ".box{text-align:center;background:#16213e;border-radius:12px;padding:40px}"
            "h2{color:#4ecca3}</style></head><body>"
            "<div class='box'><h2>Kaydedildi!</h2>"
            "<p>Yeni WiFi: <b>");
        page += newSSID;
        page += F("</b></p><p>ESP32 yeniden baslatiliyor...</p></div></body></html>");

        server.send(200, "text/html", page);
        delay(2000);
        ESP.restart();
    } else {
        server.send(200, "text/html", buildWifiPage("SSID bos birakilamaz!"));
    }
}

// GET /pump/on → Pompayı manuel olarak aç
void handlePumpOn() {
    pumpManual = true;
    pumpActive = true;
    pumpUntil  = 0;                // manuel override -> varsa sureli isi iptal et
    digitalWrite(RELAY_PIN, HIGH); // Röleyi tetikle
    server.sendHeader("Location", "/");
    server.send(302);
}

// GET /pump/off → Pompayı manuel olarak kapat
void handlePumpOff() {
    pumpManual = true;
    pumpActive = false;
    pumpUntil  = 0;                // sureli isi iptal et
    digitalWrite(RELAY_PIN, LOW);  // Röleyi bırak
    server.sendHeader("Location", "/");
    server.send(302);
}

// GET /pump/auto → Otomatik moda dön (algoritma / esik karari)
void handlePumpAuto() {
    pumpManual = false;
    pumpUntil  = 0;
    // Hemen esige gore durumu uygula; bir sonraki backend cevabini bekleme.
    // Bu olmadan son manuel durumu relay'de kaliyor.
    pumpActive = dhtValid && (humidity < HUMIDITY_THRESHOLD);
    digitalWrite(RELAY_PIN, pumpActive ? HIGH : LOW);
    server.sendHeader("Location", "/");
    server.send(302);
}

// ── Setup ─────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);

    pinMode(GREEN_LED, OUTPUT);
    pinMode(RELAY_PIN, OUTPUT);
    digitalWrite(GREEN_LED, LOW);
    digitalWrite(RELAY_PIN, LOW);

    // Açılış yanıp sönmesi (dahili LED GPIO2)
    pinMode(2, OUTPUT);
    for (int i = 0; i < 5; i++) {
        digitalWrite(2, HIGH); delay(150);
        digitalWrite(2, LOW);  delay(150);
    }

    dht.setup(DHT_PIN, DHTesp::DHT22);

    // BMP280 — once 0x76 (SDO=GND), bulamazsa 0x77 (SDO=VCC) dene
    Wire.begin(); // SDA=21, SCL=22
    if (bmp.begin(0x76)) {
        bmpValid = true;
        Serial.println("[BMP280] Bulundu! Adres: 0x76");
    } else if (bmp.begin(0x77)) {
        bmpValid = true;
        Serial.println("[BMP280] Bulundu! Adres: 0x77");
    } else {
        bmpValid = false;
        Serial.println("[BMP280] UYARI: Sensor bulunamadi! SDA=GPIO21, SCL=GPIO22 baglantilarini kontrol et.");
    }
    if (bmpValid) {
        bmp.setSampling(Adafruit_BMP280::MODE_NORMAL,
                        Adafruit_BMP280::SAMPLING_X2,
                        Adafruit_BMP280::SAMPLING_X16,
                        Adafruit_BMP280::FILTER_X16,
                        Adafruit_BMP280::STANDBY_MS_500);
    }

    // NVS'den esik degerlerini yukle
    prefs.begin("thresh", true);
    HUMIDITY_THRESHOLD = prefs.getFloat("hum", 60.0);
    TEMP_THRESHOLD     = prefs.getFloat("tmp", 30.0);
    prefs.end();
    Serial.printf("[AYAR] Esikler — Nem: %.0f%%  Sicaklik: %.0f C\n",
                  HUMIDITY_THRESHOLD, TEMP_THRESHOLD);

    // NVS'den WiFi bilgilerini yukle; kayıt yoksa DEFAULT_SSID kullanılır
    prefs.begin("wifi", true); // read-only
    wifiSSID     = prefs.getString("ssid", DEFAULT_SSID);
    wifiPassword = prefs.getString("pass", DEFAULT_PASSWORD);
    prefs.end();
    Serial.println("[WiFi] SSID: " + wifiSSID);

    WiFi.begin(wifiSSID.c_str(), wifiPassword.c_str());
    Serial.print("WiFi baglaniliyor");
    while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
    Serial.println("\nBaglandi! IP: " + WiFi.localIP().toString());
    Serial.println("Backend: " BACKEND_URL);

    server.on("/",            handleRoot);
    server.on("/pump/on",     handlePumpOn);
    server.on("/pump/off",    handlePumpOff);
    server.on("/pump/auto",   handlePumpAuto);
    server.on("/thresh",      handleThresh);      // Esik ayarlari sayfasi
    server.on("/wifi",        handleWifi);         // WiFi ayarlari sayfasi
    server.on("/save-thresh", handleSaveThresh);   // Esik degerlerini kaydet
    server.on("/save",        handleSave);          // Yeni WiFi kaydet + yeniden basla
    server.begin();
    Serial.println("Yerel web sunucu baslatildi.");
}

// ── Loop ──────────────────────────────────────────────────────
void loop() {
    server.handleClient(); // Yerel web isteklerini isle
    serviceTimedPump();    // sureli sulama kapanisini her dongude kontrol et

    if (millis() - lastRead >= READ_INTERVAL) {
        lastRead = millis();

        readSensors();
        controlRelay();
        updateLED();

        if (!dhtValid) {
            Serial.println("[UYARI] DHT22 okunamiyor!");
        } else {
            Serial.printf("Nem: %.1f%%  |  Sicaklik: %.1f C  |  Basinc: %s  |  Pompa: %s%s\n",
                humidity, temperature,
                bmpValid ? (String(pressure, 1) + " hPa").c_str() : "---",
                pumpActive ? "ACIK" : "KAPALI",
                pumpManual ? " [Manuel]" : "");
        }

        // Sensor verilerini cloud backend'e gonder, komut al
        sendToBackend();
    }
}
