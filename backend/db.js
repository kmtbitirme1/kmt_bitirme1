// ╔══════════════════════════════════════════════════════════════╗
// ║   Akilli Tarim — Kalici Veri Katmani (Render Postgres)        ║
// ║                                                              ║
// ║   DATABASE_URL ortam degiskeni varsa Postgres'e baglanir.    ║
// ║   Yoksa DB devre disi kalir; backend RAM ile calismaya       ║
// ║   devam eder (graceful degrade). Boylece DB olmadan da       ║
// ║   sistem ayakta kalir, sadece gecmis kaydedilmez.            ║
// ║                                                              ║
// ║   Iki tablo:                                                  ║
// ║     readings   — her ESP32 olcumu (sensor verisi)            ║
// ║     decisions  — her otomatik algoritma karari               ║
// ╚══════════════════════════════════════════════════════════════╝

import pg from "pg";

const { Pool } = pg;

const DATABASE_URL = process.env.DATABASE_URL || "";

let pool = null;
let ready = false;

// Render Postgres TLS ister; self-signed oldugu icin rejectUnauthorized=false.
if (DATABASE_URL) {
  pool = new Pool({
    connectionString: DATABASE_URL,
    ssl: { rejectUnauthorized: false },
    max: 5,
    idleTimeoutMillis: 30000,
  });
  pool.on("error", (e) => console.warn(`[DB] pool hatasi: ${e.message}`));
}

// ── Tablolari olustur (idempotent) ────────────────────────────
export async function initDb() {
  if (!pool) {
    console.log("[DB] DATABASE_URL yok — kalici kayit devre disi (RAM modu).");
    return false;
  }
  try {
    await pool.query(`
      CREATE TABLE IF NOT EXISTS readings (
        id            BIGSERIAL PRIMARY KEY,
        ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
        soil_moisture DOUBLE PRECISION,
        humidity      DOUBLE PRECISION,
        temperature   DOUBLE PRECISION,
        pressure_hpa  DOUBLE PRECISION,
        pump          BOOLEAN,
        pump_manual   BOOLEAN,
        green_led     BOOLEAN
      );
    `);
    await pool.query(`
      CREATE TABLE IF NOT EXISTS decisions (
        id                    BIGSERIAL PRIMARY KEY,
        ts                    TIMESTAMPTZ NOT NULL DEFAULT now(),
        soil_moisture         DOUBLE PRECISION,
        temperature           DOUBLE PRECISION,
        air_humidity_pct      DOUBLE PRECISION,
        pressure_kpa          DOUBLE PRECISION,
        on_probability        DOUBLE PRECISION,
        decision_threshold    DOUBLE PRECISION,
        irrigation_required   BOOLEAN,
        decision_label        TEXT,
        pump_duration_seconds DOUBLE PRECISION,
        moisture_deficit      DOUBLE PRECISION,
        target_soil_moisture  DOUBLE PRECISION,
        decision_reason       TEXT
      );
    `);
    // Zaman bazli sorgular icin indeks.
    await pool.query(`CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);`);
    await pool.query(`CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions (ts);`);
    ready = true;
    console.log("[DB] Postgres baglandi, tablolar hazir.");
    return true;
  } catch (e) {
    console.warn(`[DB] init hatasi: ${e.message} — RAM moduna dusuldu.`);
    return false;
  }
}

export function dbReady() {
  return ready;
}

// ── Bir olcumu kaydet ─────────────────────────────────────────
export async function insertReading(r) {
  if (!ready) return;
  try {
    await pool.query(
      `INSERT INTO readings
         (soil_moisture, humidity, temperature, pressure_hpa, pump, pump_manual, green_led)
       VALUES ($1,$2,$3,$4,$5,$6,$7)`,
      [
        r.soilMoisture ?? null,
        r.humidity ?? null,
        r.temperature ?? null,
        r.pressure ?? null,
        !!r.pump,
        !!r.pumpManual,
        !!r.greenLed,
      ]
    );
  } catch (e) {
    console.warn(`[DB] insertReading hatasi: ${e.message}`);
  }
}

// ── Bir algoritma kararini kaydet ─────────────────────────────
export async function insertDecision(d) {
  if (!ready) return;
  try {
    await pool.query(
      `INSERT INTO decisions
         (soil_moisture, temperature, air_humidity_pct, pressure_kpa,
          on_probability, decision_threshold, irrigation_required, decision_label,
          pump_duration_seconds, moisture_deficit, target_soil_moisture, decision_reason)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`,
      [
        d.soilMoisture ?? null,
        d.temperature ?? null,
        d.airHumidityPct ?? null,
        d.pressureKpa ?? null,
        d.onProbability ?? null,
        d.decisionThreshold ?? null,
        d.irrigationRequired ?? null,
        d.decisionLabel ?? null,
        d.pumpDurationSeconds ?? null,
        d.moistureDeficit ?? null,
        d.targetSoilMoisture ?? null,
        d.decisionReason ?? null,
      ]
    );
  } catch (e) {
    console.warn(`[DB] insertDecision hatasi: ${e.message}`);
  }
}

// ── Son N olcumu getir (grafik/gecmis icin, eskiden yeniye) ────
export async function recentReadings(limit = 200) {
  if (!ready) return [];
  try {
    const { rows } = await pool.query(
      `SELECT ts, soil_moisture, humidity, temperature, pressure_hpa, pump
         FROM readings ORDER BY ts DESC LIMIT $1`,
      [limit]
    );
    return rows.reverse();
  } catch (e) {
    console.warn(`[DB] recentReadings hatasi: ${e.message}`);
    return [];
  }
}

// ── Son N karari getir (zaman cizelgesi icin) ─────────────────
export async function recentDecisions(limit = 50) {
  if (!ready) return [];
  try {
    const { rows } = await pool.query(
      `SELECT ts, decision_label, on_probability, pump_duration_seconds, moisture_deficit, decision_reason
         FROM decisions ORDER BY ts DESC LIMIT $1`,
      [limit]
    );
    return rows;
  } catch (e) {
    console.warn(`[DB] recentDecisions hatasi: ${e.message}`);
    return [];
  }
}
