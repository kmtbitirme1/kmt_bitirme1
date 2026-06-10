// ╔══════════════════════════════════════════════════════════════╗
// ║   Akilli Tarim — Backend (Render.com)                         ║
// ║                                                              ║
// ║   Akis:                                                       ║
// ║   1) ESP32  --POST /ingest-->  backend     (veri gonderir)    ║
// ║   2) Frontend --GET /api-->    backend     (veriyi okur)      ║
// ║   3) Frontend --POST /command-> backend    (pompa komutu)     ║
// ║   4) ESP32  --GET /command-->  backend      (komutu ceker)    ║
// ║                                                              ║
// ║   Veri RAM'de tutulur. Render restart/uyku olunca silinir;   ║
// ║   ESP32 saniyeler icinde tekrar doldurur.                    ║
// ╚══════════════════════════════════════════════════════════════╝

import express from "express";
import cors from "cors";

const app = express();
const PORT = process.env.PORT || 3000;

// ESP32'nin /ingest'e yazarken gonderecegi gizli anahtar.
// Render'da Environment > INGEST_TOKEN olarak ayarla. Bos birakilirsa kontrol yapilmaz.
const INGEST_TOKEN = process.env.INGEST_TOKEN || "";

const MAX_HISTORY = 60; // son 60 olcum (grafik icin)

app.use(cors());          // GitHub Pages farkli origin — hepsine izin
app.use(express.json());  // JSON body parse

// ── Durum (RAM) ───────────────────────────────────────────────
let latest = {
  humidity: null,
  temperature: null,
  pump: false,
  pumpManual: false,
  greenLed: false,
  updatedAt: null,     // ISO zaman — son veri ne zaman geldi
  online: false,       // ESP32 son 15sn icinde veri gonderdi mi
};

let history = [];        // [{ t, humidity, temperature }]

// Frontend'in biraktigi, ESP32'nin cekecegi komut.
// "on" | "off" | "auto" | null(komut yok)
let pendingCommand = null;

// ── Yardimci: ESP32 online mi? ────────────────────────────────
function isOnline() {
  if (!latest.updatedAt) return false;
  return Date.now() - new Date(latest.updatedAt).getTime() < 15000;
}

// ── Saglik / kok ──────────────────────────────────────────────
app.get("/", (_req, res) => {
  res.json({ ok: true, service: "akilli-tarim-backend", online: isOnline() });
});

// ── ESP32 -> backend: veri yaz ────────────────────────────────
// Beklenen body: { humidity, temperature, pump, pumpManual, greenLed }
app.post("/ingest", (req, res) => {
  if (INGEST_TOKEN && req.headers["x-token"] !== INGEST_TOKEN) {
    return res.status(401).json({ error: "gecersiz token" });
  }

  const { humidity, temperature, pump, pumpManual, greenLed } = req.body || {};

  latest = {
    humidity: typeof humidity === "number" ? humidity : latest.humidity,
    temperature: typeof temperature === "number" ? temperature : latest.temperature,
    pump: !!pump,
    pumpManual: !!pumpManual,
    greenLed: !!greenLed,
    updatedAt: new Date().toISOString(),
    online: true,
  };

  if (typeof humidity === "number" && typeof temperature === "number") {
    history.push({ t: latest.updatedAt, humidity, temperature });
    if (history.length > MAX_HISTORY) history.shift();
  }

  // Cevapta bekleyen komutu da don — ESP32 ayri istek atmadan alabilir
  const cmd = pendingCommand;
  pendingCommand = null;
  res.json({ ok: true, command: cmd });
});

// ── Frontend -> backend: anlik durum + gecmis ─────────────────
app.get("/api", (_req, res) => {
  res.json({
    ...latest,
    online: isOnline(),
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
  pendingCommand = command;
  res.json({ ok: true, queued: command });
});

// ── ESP32 -> backend: bekleyen komutu cek ─────────────────────
// (Ayri pull isteyen ESP32 icin; /ingest cevabi da komutu donuyor)
app.get("/command", (_req, res) => {
  const cmd = pendingCommand;
  pendingCommand = null;
  res.json({ command: cmd });
});

app.listen(PORT, () => {
  console.log(`Akilli Tarim backend calisiyor — port ${PORT}`);
});
