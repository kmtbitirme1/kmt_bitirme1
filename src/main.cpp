#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DHTesp.h>

// ── Pin tanimlari ─────────────────────────────────────────────
#define DHT_PIN   15
#define GREEN_LED 13
#define RELAY_PIN 32  // IRLZ44N Gate — HIGH=pompa acik, LOW=kapali

// ── WiFi ──────────────────────────────────────────────────────
const char* ssid     = "TURKNET_B7776";
const char* password = "fhCNASdY";

// ── Esik degerleri ────────────────────────────────────────────
const float HUMIDITY_THRESHOLD = 60.0;  // % altinda pompa acilir
const float TEMP_THRESHOLD     = 30.0;  // C ustunde yesil LED yanar

// ── Nesneler ──────────────────────────────────────────────────
DHTesp    dht;
WebServer server(80);

// ── Global durum ──────────────────────────────────────────────
float humidity    = 0;
float temperature = 0;
bool  dhtValid    = false;
bool  pumpActive  = false;
bool  pumpManual  = false;

unsigned long lastRead = 0;
const unsigned long READ_INTERVAL = 2000;

// ── MOSFET (IRLZ44N) kontrolu ─────────────────────────────────
void controlRelay() {
    if (pumpManual) return;
    pumpActive = dhtValid && (humidity < HUMIDITY_THRESHOLD);
    digitalWrite(RELAY_PIN, pumpActive ? HIGH : LOW);
}

// ── LED kontrolu ──────────────────────────────────────────────
void updateLED() {
    digitalWrite(GREEN_LED, (dhtValid && temperature > TEMP_THRESHOLD) ? HIGH : LOW);
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

// ╔══════════════════════════════════════════════════════════════╗
// ║              WEB ARAYÜZÜ — GELİŞTİRİCİ NOTU               ║
// ║                                                              ║
// ║  Bu fonksiyon ESP32 üzerindeki web sunucusunun              ║
// ║  her istekte oluşturduğu HTML sayfasını döndürür.           ║
// ║                                                              ║
// ║  Mimari:                                                     ║
// ║  • Sayfa 3 saniyede bir otomatik yenilenir (meta refresh)   ║
// ║  • Tüm veriler global değişkenlerden okunur                 ║
// ║  • Renk mantığı: yeşil = normal, kırmızı = eşik aşıldı     ║
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
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;max-width:700px;margin:24px auto}"
        ".card{background:#16213e;border-radius:12px;padding:24px;text-align:center}"
        ".val{font-size:2.2em;font-weight:700;color:#4ecca3;margin:10px 0}" /* Kart değer rengi */
        ".lbl{font-size:.85em;color:#aaa}"                 /* Kart başlık rengi */
        ".warn{color:#ff6b6b!important}"                   /* Uyarı rengi — eşik aşıldığında */
        ".btn{padding:10px 20px;margin:6px;border:none;border-radius:8px;font-weight:700;cursor:pointer}"
        "footer{text-align:center;color:#555;font-size:.78em;margin-top:28px}"
        "</style></head><body>");

    // ── Sayfa başlığı ─────────────────────────────────────────
    // WiFi.localIP() → ESP32'nin ağdaki IP adresi
    html += F("<h1>&#127807; Akilli Tarim</h1>");
    html += "<p class='sub'>IP: " + WiFi.localIP().toString() + "</p>";

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

    // KART 4: Pompa / Röle durumu
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
        "</div>"
        "<footer>Sayfa 3 sn yenilenir</footer></body></html>");

    return html;
}

// ── HTTP Handler'lar ──────────────────────────────────────────
// Tarayıcıdan gelen GET isteklerini karşılar
// Her handler işlemi yapıp ana sayfaya (/) yönlendirir (302 redirect)

// GET / → Ana sayfayı döndür
void handleRoot() { server.send(200, "text/html", buildPage()); }

// GET /pump/on → Pompayı manuel olarak aç
void handlePumpOn() {
    pumpManual = true;
    pumpActive = true;
    digitalWrite(RELAY_PIN, HIGH); // Röleyi tetikle
    server.sendHeader("Location", "/");
    server.send(302);
}

// GET /pump/off → Pompayı manuel olarak kapat
void handlePumpOff() {
    pumpManual = true;
    pumpActive = false;
    digitalWrite(RELAY_PIN, LOW);  // Röleyi bırak
    server.sendHeader("Location", "/");
    server.send(302);
}

// GET /pump/auto → Otomatik moda dön (nem eşiğine göre karar verir)
void handlePumpAuto() {
    pumpManual = false;
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

    server.on("/",          handleRoot);
    server.on("/pump/on",   handlePumpOn);
    server.on("/pump/off",  handlePumpOff);
    server.on("/pump/auto", handlePumpAuto);
    server.begin();
    Serial.println("Web sunucu baslatildi.");
}

// ── Loop ──────────────────────────────────────────────────────
void loop() {
    server.handleClient();
    updateLED();

    if (millis() - lastRead >= READ_INTERVAL) {
        lastRead = millis();
        readSensors();
        controlRelay();

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
