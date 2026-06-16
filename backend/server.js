// ╔══════════════════════════════════════════════════════════════╗
// ║   Akilli Tarim — Backend (Render.com)                         ║
// ║                                                              ║
// ║   Akis:                                                       ║
// ║   1) ESP32  --POST /ingest-->  backend     (veri gonderir)    ║
// ║   2) backend --POST /predict-> ALGO SERVIS (Python)           ║
// ║   3) Frontend --GET /api-->    backend     (veri + karar)     ║
// ║   4) Frontend --POST /command-> backend    (manuel komut)     ║
// ║   5) ESP32  --GET /command-->  backend      (komutu ceker)    ║
// ║                                                              ║
// ║   Otomatik modda pompa karari artik basit esik degil,        ║
// ║   Python sulama algoritmasi tarafindan verilir. Algoritma    ║
// ║   pompanin kac saniye calisacagini da hesaplar.              ║
// ║                                                              ║
// ║   Veri RAM'de tutulur. Render restart/uyku olunca silinir;   ║
// ║   ESP32 saniyeler icinde tekrar doldurur.                    ║
// ╚══════════════════════════════════════════════════════════════╝

import express from "express";
import cors from "cors";
import { appendFile, access, writeFile } from "node:fs/promises";
import {
  initDb,
  dbReady,
  insertReading,
  insertDecision,
  recentReadings,
  recentDecisions,
  dbDiagnostics,
} from "./db.js";

const app = express();
const PORT = process.env.PORT || 3000;

// ESP32'nin /ingest'e yazarken gonderecegi gizli anahtar.
const INGEST_TOKEN = process.env.INGEST_TOKEN || "";

// Python sulama algoritmasi servisinin adresi.
// Render'da ALGO_URL ortam degiskeniyle ayarla. Lokal varsayilan: localhost:5001
// Render "hostport" semasiz "host:port" verebilir -> basina https:// ekle.
function normalizeAlgoUrl(raw) {
  const v = (raw || "http://localhost:5001").trim();
  if (/^https?:\/\//i.test(v)) return v.replace(/\/+$/, "");
  return "https://" + v.replace(/\/+$/, "");
}
const ALGO_URL = normalizeAlgoUrl(process.env.ALGO_URL);

// Karar/log dosyasi — evaluate_algorithm_performance.py bunu okur.
const LOG_PATH = process.env.LOG_PATH || "./algorithm_log.csv";

// algorithm_log_template.csv ile birebir ayni sutun sirasi.
const LOG_HEADER =
  "timestamp,soil_moisture_before,temperature,air_humidity_pct,pressure_kpa," +
  "light_lux,last_irrigation_minutes_ago,on_probability,decision_threshold," +
  "irrigation_required,pump_duration_seconds,soil_moisture_after," +
  "target_soil_moisture,true_irrigation_required";

// Log dosyasi yoksa basligi yaz (Render gibi efemer FS'lerde her acilista).
async function ensureLogHeader() {
  try {
    await access(LOG_PATH);
  } catch {
    await writeFile(LOG_PATH, LOG_HEADER + "\n");
  }
}

// BMP280 yoksa kullanilacak deniz seviyesi varsayilani (algoritma 80-120 kPa bekler).
const DEFAULT_PRESSURE_KPA = 101.3;

const MAX_HISTORY = 60; // son 60 olcum (grafik icin)

app.use(cors());
app.use(express.json());

// ── Durum (RAM) ───────────────────────────────────────────────
let appConfig = { humThreshold: 60, tempThreshold: 30 };

let latest = {
  soilMoisture: null,  // Yeni: Toprak Nemi
  humidity: null,
  temperature: null,
  pressure: null,      // hPa (BMP280); yoksa null
  pump: false,
  pumpManual: false,
  greenLed: false,
  updatedAt: null,
  online: false,

  // ── Algoritma karar alanlari ──
  irrigationRequired: null,    // bool
  decisionLabel: null,         // "ON" | "OFF"
  onProbability: null,         // 0..1
  pumpDurationSeconds: null,   // saniye
  moistureDeficit: null,
  decisionReason: null,
  algoOnline: false,           // Python servis ulasilabildi mi
};

let history = []; // [{ t, humidity, temperature }]

// Frontend'in biraktigi manuel komut: "on" | "off" | "auto" | null
let pendingCommand = null;

// Otomatik modda algoritmanin urettigi komut (sure dahil).
// { command: "on"|"off", durationSeconds } | null
let autoCommand = null;

// Sistem otomatik modda mi? (manuel on/off algoritmayi gecici devre disi birakir)
let autoMode = true;

// Son sulamanin (pompa ON) zamani — last_irrigation_minutes_ago icin.
let lastIrrigationAt = null;

// ── Yardimci: ESP32 online mi? ────────────────────────────────
function isOnline() {
  if (!latest.updatedAt) return false;
  return Date.now() - new Date(latest.updatedAt).getTime() < 15000;
}

function minutesSinceLastIrrigation() {
  if (!lastIrrigationAt) return null;
  return (Date.now() - lastIrrigationAt) / 60000;
}

// ── Algoritma servisini cagir ─────────────────────────────────
async function callAlgorithm(soilMoisture, humidity, temperature, pressureHpa) {
  // Basinc: BMP280 hPa gonderir (~1013). Algoritma kPa (80-120) bekler -> /10.
  const pressureKpa =
    typeof pressureHpa === "number" ? pressureHpa / 10 : DEFAULT_PRESSURE_KPA;

  const sensorData = {
    soil_moisture: soilMoisture,      // Gerçek Toprak Nemi
    air_humidity_pct: humidity,       // Hava Nemi
    temperature: temperature,
    pressure_kpa: pressureKpa,
    last_irrigation_minutes_ago: minutesSinceLastIrrigation(),
    config: { target_soil_moisture: appConfig.humThreshold }
  };

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    const r = await fetch(`${ALGO_URL}/predict`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sensorData),
      signal: controller.signal,
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      console.warn(`[ALGO] ${r.status}: ${err.error || "bilinmeyen hata"}`);
      return null;
    }
    return await r.json();
  } catch (e) {
    console.warn(`[ALGO] servise ulasilamadi: ${e.message}`);
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

// ── Karari CSV log'a yaz (performans degerlendirme icin) ──────
async function logDecision(soilMoisture, humidity, temperature, pressureHpa, decision) {
  // Sutun sirasi algorithm_log_template.csv ile ayni.
  // soil_moisture_after / true_irrigation_required saha verisi yok -> bos birak.
  const pressureKpa =
    typeof pressureHpa === "number" ? pressureHpa / 10 : DEFAULT_PRESSURE_KPA;
  const row = [
    new Date().toISOString(),
    soilMoisture,                              // soil_moisture_before (Gerçek toprak nemi)
    temperature,
    humidity,                                  // air_humidity_pct
    pressureKpa,
    "",                                        // light_lux (sensor yok)
    minutesSinceLastIrrigation() ?? "",
    decision.on_probability ?? "",
    decision.decision_threshold ?? "",
    decision.irrigation_required ? 1 : 0,
    decision.pump_duration_seconds ?? "",
    "",                                        // soil_moisture_after (saha)
    decision.config?.target_soil_moisture ?? "",
    "",                                        // true_irrigation_required (etiket)
  ].join(",");

  try {
    await appendFile(LOG_PATH, row + "\n");
  } catch (e) {
    console.warn(`[LOG] yazilamadi: ${e.message}`);
  }
}

// ── Saglik / kok ──────────────────────────────────────────────
app.get("/", (_req, res) => {
  res.json({ ok: true, service: "akilli-tarim-backend", online: isOnline() });
});

// ── DEBUG: algoritma baglantisini canli test et ───────────────
// Node -> Python istegini gercek sensor verisiyle atar, ham sonucu/hatayi doner.
app.get("/debug/algo", async (_req, res) => {
  const sm = typeof latest.soilMoisture === "number" ? latest.soilMoisture : 0;
  const hum = typeof latest.humidity === "number" ? latest.humidity : 50;
  const temp = typeof latest.temperature === "number" ? latest.temperature : 25;
  const pressureKpa =
    typeof latest.pressure === "number" ? latest.pressure / 10 : DEFAULT_PRESSURE_KPA;
  const sensorData = {
    soil_moisture: sm,
    air_humidity_pct: hum,
    temperature: temp,
    pressure_kpa: pressureKpa,
    last_irrigation_minutes_ago: minutesSinceLastIrrigation(),
    config: { target_soil_moisture: appConfig.humThreshold },
  };

  const t0 = Date.now();
  let status = null, body = null, fetchError = null;
  try {
    const r = await fetch(`${ALGO_URL}/predict`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sensorData),
    });
    status = r.status;
    body = await r.text();
  } catch (e) {
    fetchError = `${e.name}: ${e.message}${e.cause ? " | cause: " + e.cause : ""}`;
  }

  res.json({
    algoUrl: ALGO_URL,
    nodeVersion: process.version,
    sent: sensorData,
    elapsedMs: Date.now() - t0,
    httpStatus: status,       // 200 beklenir
    fetchError,               // dolu ise Node fetch patladi (asil sebep burada)
    body: body ? body.slice(0, 500) : null,
  });
});

// ── DEBUG: DB baglanti durumu ─────────────────────────────────
app.get("/debug/db", async (_req, res) => {
  res.json(await dbDiagnostics());
});

// ── ESP32 -> backend: veri yaz ────────────────────────────────
// Beklenen body: { humidity, temperature, pump, pumpManual, greenLed }
app.post("/ingest", async (req, res) => {
  if (INGEST_TOKEN && req.headers["x-token"] !== INGEST_TOKEN) {
    return res.status(401).json({ error: "gecersiz token" });
  }

  const { soilMoisture, humidity, temperature, pressure, pump, pumpManual, greenLed } =
    req.body || {};

  // Artık okumaların gecerli sayilmasi icin soilMoisture da lazim
  const haveReadings =
    typeof humidity === "number" && typeof temperature === "number" && typeof soilMoisture === "number";

  latest = {
    ...latest,
    soilMoisture: typeof soilMoisture === "number" ? soilMoisture : latest.soilMoisture,
    humidity: typeof humidity === "number" ? humidity : latest.humidity,
    temperature: typeof temperature === "number" ? temperature : latest.temperature,
    pressure: typeof pressure === "number" ? pressure : latest.pressure,
    pump: !!pump,
    pumpManual: !!pumpManual,
    greenLed: !!greenLed,
    updatedAt: new Date().toISOString(),
    online: true,
  };

  if (haveReadings) {
    history.push({ t: latest.updatedAt, soilMoisture, humidity, temperature, pump: latest.pump });
    if (history.length > MAX_HISTORY) history.shift();
    // Kalici kayit (DB yoksa no-op).
    insertReading({
      soilMoisture, humidity, temperature,
      pressure: latest.pressure, pump: latest.pump,
      pumpManual: latest.pumpManual, greenLed: latest.greenLed,
    });
  }

  // Pompa ON'a gecmisse son sulama zamanini guncelle.
  if (latest.pump) lastIrrigationAt = Date.now();

  // ── Otomatik modda algoritmayi calistir ──
  if (autoMode && haveReadings) {
    const decision = await callAlgorithm(soilMoisture, humidity, temperature, latest.pressure);
    if (decision) {
      latest.algoOnline = true;
      latest.irrigationRequired = decision.irrigation_required;
      latest.decisionLabel = decision.decision_label;
      latest.onProbability = decision.on_probability;
      latest.pumpDurationSeconds = decision.pump_duration_seconds;
      latest.moistureDeficit = decision.moisture_deficit;
      latest.decisionReason = decision.decision_reason;

      // ESP32'ye gidecek otomatik komut (sure dahil).
      autoCommand = decision.irrigation_required
        ? { command: "on", durationSeconds: decision.pump_duration_seconds }
        : { command: "off", durationSeconds: 0 };

      logDecision(soilMoisture, humidity, temperature, latest.pressure, decision);

      // Kalici karar kaydi (DB yoksa no-op).
      const pressureKpa =
        typeof latest.pressure === "number" ? latest.pressure / 10 : DEFAULT_PRESSURE_KPA;
      insertDecision({
        soilMoisture, temperature, airHumidityPct: humidity, pressureKpa,
        onProbability: decision.on_probability,
        decisionThreshold: decision.decision_threshold,
        irrigationRequired: decision.irrigation_required,
        decisionLabel: decision.decision_label,
        pumpDurationSeconds: decision.pump_duration_seconds,
        moistureDeficit: decision.moisture_deficit,
        targetSoilMoisture: decision.config?.target_soil_moisture,
        decisionReason: decision.decision_reason,
      });
    } else {
      latest.algoOnline = false;
    }
  }

  // Cevapta uygulanacak komutu don. Oncelik: manuel komut > otomatik komut.
  let cmd = null;
  if (pendingCommand) {
    cmd = { command: pendingCommand, durationSeconds: 0 };
    pendingCommand = null;
  } else if (autoMode && autoCommand) {
    cmd = autoCommand;
    autoCommand = null;
  }

  res.json({ ok: true, command: cmd, config: appConfig });
});

// ── Frontend -> backend: anlik durum + gecmis + karar ─────────
app.get("/api", (_req, res) => {
  res.json({
    ...latest,
    online: isOnline(),
    autoMode,
    history,
    config: appConfig,
    dbReady: dbReady(),   // kalici kayit aktif mi
  });
});

// ── Frontend -> backend: kalici gecmis (DB) ───────────────────
// ?readings=N  ?decisions=M ile limit ayarlanabilir.
app.get("/history", async (req, res) => {
  if (!dbReady()) {
    return res.json({ dbReady: false, readings: [], decisions: [] });
  }
  const rLimit = Math.min(parseInt(req.query.readings) || 200, 3000);
  const dLimit = Math.min(parseInt(req.query.decisions) || 50, 500);
  const [readings, decisions] = await Promise.all([
    recentReadings(rLimit),
    recentDecisions(dLimit),
  ]);
  res.json({ dbReady: true, readings, decisions });
});

// ── Frontend -> backend: pompa komutu birak ───────────────────
// body: { command: "on" | "off" | "auto" }
app.post("/command", (req, res) => {
  const { command } = req.body || {};
  if (!["on", "off", "auto"].includes(command)) {
    return res.status(400).json({ error: "command on|off|auto olmali" });
  }

  if (command === "auto") {
    autoMode = true;       // algoritma tekrar devrede
    pendingCommand = "auto";
  } else {
    autoMode = false;      // manuel mudahale -> algoritma beklemede
    autoCommand = null;
    pendingCommand = command;
  }

  res.json({ ok: true, queued: command, autoMode });
});

// ── Frontend -> backend: ayarlari (esikleri) guncelle ─────────
app.post("/config", (req, res) => {
  const { humThreshold, tempThreshold } = req.body || {};
  if (typeof humThreshold === "number") appConfig.humThreshold = humThreshold;
  if (typeof tempThreshold === "number") appConfig.tempThreshold = tempThreshold;
  res.json({ ok: true, config: appConfig });
});

// ── ESP32 -> backend: bekleyen komutu cek ─────────────────────
// (/ingest cevabi da komutu donuyor; bu ayri pull isteyen ESP32 icin)
app.get("/command", (_req, res) => {
  let cmd = null;
  if (pendingCommand) {
    cmd = { command: pendingCommand, durationSeconds: 0 };
    pendingCommand = null;
  } else if (autoMode && autoCommand) {
    cmd = autoCommand;
    autoCommand = null;
  }
  res.json({ command: cmd });
});

Promise.allSettled([ensureLogHeader(), initDb()]).finally(() => {
  app.listen(PORT, () => {
    console.log(`Akilli Tarim backend calisiyor — port ${PORT}`);
    console.log(`Algoritma servisi: ${ALGO_URL}`);
    console.log(`Kalici DB: ${dbReady() ? "AKTIF (Postgres)" : "KAPALI (RAM modu)"}`);
  });
});
