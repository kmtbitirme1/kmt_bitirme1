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
let latest = {
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

// ── Dahili Algoritma (Python'dan JS'ye cevrilmis) ─────────────
async function callAlgorithm(humidity, temperature, pressureHpa) {
  const pressureKpa = typeof pressureHpa === "number" ? pressureHpa / 10 : DEFAULT_PRESSURE_KPA;
  
  // Model Sabitleri (Python modelinden birebir alindi)
  const SCALER_MEAN = [45.433090227130656, 22.492227547405708, 58.52105188580954, 101.13141821212753];
  const SCALER_SCALE = [26.007173042265276, 13.28271661038062, 30.072821403765282, 0.2184434734277875];
  const MODEL_COEFFICIENTS = [-0.7745651265412601, 0.7426499310497418, -0.015164651317430384, -0.020784588977389953];
  const MODEL_INTERCEPT = 0.20465532647535664;
  
  const DECISION_THRESHOLD = 0.40;
  const TARGET_SOIL_MOISTURE = 55.0;
  const MINIMUM_IRRIGATION_INTERVAL_MINUTES = 30.0;
  const SECONDS_PER_MOISTURE_POINT = 0.20;
  const MIN_PUMP_DURATION = 1.0;
  const MAX_PUMP_DURATION = 8.0;

  // Girdiler (soil_moisture su an humidity proxy olarak kullaniliyor)
  const inputs = [humidity, temperature, humidity, pressureKpa];
  const lastIrrigationMinutesAgo = minutesSinceLastIrrigation();

  // Olasilik Hesaplama (Logistic Regression)
  let logit = MODEL_INTERCEPT;
  for (let i = 0; i < 4; i++) {
    const standardized = (inputs[i] - SCALER_MEAN[i]) / SCALER_SCALE[i];
    logit += MODEL_COEFFICIENTS[i] * standardized;
  }
  const onProbability = 1 / (1 + Math.exp(-logit));
  const modelSaysOn = onProbability >= DECISION_THRESHOLD;

  // Optimizasyon Hesaplama
  const moistureDeficit = Math.max(0, TARGET_SOIL_MOISTURE - humidity);

  // Karar Mantigi
  if (!modelSaysOn) {
    return {
      irrigation_required: false,
      decision_label: "OFF",
      on_probability: onProbability,
      pump_duration_seconds: 0.0,
      moisture_deficit: moistureDeficit,
      decision_reason: `ON olasiligi ${onProbability.toFixed(3)}, ${DECISION_THRESHOLD} karar esiginin altindadir.`
    };
  }

  if (humidity >= TARGET_SOIL_MOISTURE) {
    return {
      irrigation_required: false,
      decision_label: "OFF",
      on_probability: onProbability,
      pump_duration_seconds: 0.0,
      moisture_deficit: 0.0,
      decision_reason: `Toprak nemi hedef degere (${TARGET_SOIL_MOISTURE}) ulasti.`
    };
  }

  if (lastIrrigationMinutesAgo !== null && lastIrrigationMinutesAgo < MINIMUM_IRRIGATION_INTERVAL_MINUTES) {
    return {
      irrigation_required: false,
      decision_label: "OFF",
      on_probability: onProbability,
      pump_duration_seconds: 0.0,
      moisture_deficit: moistureDeficit,
      decision_reason: `Model ON egilimi gostermistir ancak son sulamadan yalnizca ${lastIrrigationMinutesAgo.toFixed(1)} dakika gecmistir.`
    };
  }

  let rawDuration = moistureDeficit * SECONDS_PER_MOISTURE_POINT;
  let pumpDuration = Math.max(MIN_PUMP_DURATION, Math.min(rawDuration, MAX_PUMP_DURATION));

  return {
    irrigation_required: true,
    decision_label: "ON",
    on_probability: onProbability,
    pump_duration_seconds: parseFloat(pumpDuration.toFixed(2)),
    moisture_deficit: parseFloat(moistureDeficit.toFixed(2)),
    decision_reason: `ON olasiligi ${onProbability.toFixed(3)}. Nem acigi ${moistureDeficit.toFixed(2)} -> Pompa ${pumpDuration.toFixed(2)} sn calisacak.`
  };
}

// ── Karari CSV log'a yaz (performans degerlendirme icin) ──────
async function logDecision(humidity, temperature, pressureHpa, decision) {
  // Sutun sirasi algorithm_log_template.csv ile ayni.
  // soil_moisture_after / true_irrigation_required saha verisi yok -> bos birak.
  const pressureKpa =
    typeof pressureHpa === "number" ? pressureHpa / 10 : DEFAULT_PRESSURE_KPA;
  const row = [
    new Date().toISOString(),
    humidity,                                  // soil_moisture_before (proxy)
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

// ── ESP32 -> backend: veri yaz ────────────────────────────────
// Beklenen body: { humidity, temperature, pump, pumpManual, greenLed }
app.post("/ingest", async (req, res) => {
  if (INGEST_TOKEN && req.headers["x-token"] !== INGEST_TOKEN) {
    return res.status(401).json({ error: "gecersiz token" });
  }

  const { humidity, temperature, pressure, pump, pumpManual, greenLed } =
    req.body || {};

  const haveReadings =
    typeof humidity === "number" && typeof temperature === "number";

  latest = {
    ...latest,
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
    history.push({ t: latest.updatedAt, humidity, temperature });
    if (history.length > MAX_HISTORY) history.shift();
  }

  // Pompa ON'a gecmisse son sulama zamanini guncelle.
  if (latest.pump) lastIrrigationAt = Date.now();

  // ── Otomatik modda algoritmayi calistir ──
  if (autoMode && haveReadings) {
    const decision = await callAlgorithm(humidity, temperature, latest.pressure);
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

      logDecision(humidity, temperature, latest.pressure, decision);
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

  res.json({ ok: true, command: cmd });
});

// ── Frontend -> backend: anlik durum + gecmis + karar ─────────
app.get("/api", (_req, res) => {
  res.json({
    ...latest,
    online: isOnline(),
    autoMode,
    history,
  });
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

ensureLogHeader().finally(() => {
  app.listen(PORT, () => {
    console.log(`Akilli Tarim backend calisiyor — port ${PORT}`);
    console.log(`Algoritma servisi: ${ALGO_URL}`);
  });
});
