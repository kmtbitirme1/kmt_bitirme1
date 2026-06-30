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
let lastError = null;   // son init/baglanti hatasi (debug icin)

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
        decision_reason       TEXT,
        target_deviation      DOUBLE PRECISION,
        absolute_target_deviation DOUBLE PRECISION,
        target_status         TEXT,
        decision_threshold_next DOUBLE PRECISION,
        window_size           INTEGER,
        observations_in_current_window INTEGER,
        threshold_update_applied BOOLEAN,
        threshold_update_direction TEXT,
        threshold_old         DOUBLE PRECISION,
        threshold_new         DOUBLE PRECISION,
        threshold_window_average_soil_moisture DOUBLE PRECISION,
        threshold_window_deviation_percent DOUBLE PRECISION,
        sensor_quality_warning_active BOOLEAN,
        sensor_quality_warning_type TEXT,
        sensor_quality_message TEXT,
        consecutive_extreme_count INTEGER,
        last_irrigation_minutes_ago DOUBLE PRECISION,
        light_lux             DOUBLE PRECISION
      );
    `);
    
    // Eksik kolonlari ekle (zaten varsa IF NOT EXISTS ile gecer)
    const columnsToAdd = [
      "target_deviation DOUBLE PRECISION",
      "absolute_target_deviation DOUBLE PRECISION",
      "target_status TEXT",
      "decision_threshold_next DOUBLE PRECISION",
      "window_size INTEGER",
      "observations_in_current_window INTEGER",
      "threshold_update_applied BOOLEAN",
      "threshold_update_direction TEXT",
      "threshold_old DOUBLE PRECISION",
      "threshold_new DOUBLE PRECISION",
      "threshold_window_average_soil_moisture DOUBLE PRECISION",
      "threshold_window_deviation_percent DOUBLE PRECISION",
      "sensor_quality_warning_active BOOLEAN",
      "sensor_quality_warning_type TEXT",
      "sensor_quality_message TEXT",
      "consecutive_extreme_count INTEGER",
      "last_irrigation_minutes_ago DOUBLE PRECISION",
      "light_lux DOUBLE PRECISION"
    ];
    for (const col of columnsToAdd) {
      await pool.query(`ALTER TABLE decisions ADD COLUMN IF NOT EXISTS ${col};`);
    }

    // Zaman bazli sorgular icin indeks.
    await pool.query(`CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);`);
    await pool.query(`CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions (ts);`);
    ready = true;
    lastError = null;
    console.log("[DB] Postgres baglandi, tablolar hazir.");
    return true;
  } catch (e) {
    lastError = `${e.code ? e.code + ": " : ""}${e.message}`;
    console.warn(`[DB] init hatasi: ${lastError} — RAM moduna dusuldu.`);
    return false;
  }
}

export function dbReady() {
  return ready;
}

// ── DEBUG: DB durum teshisi ───────────────────────────────────
export async function dbDiagnostics() {
  const urlSet = !!DATABASE_URL;
  // URL'i sizdirma; sadece host kismini goster.
  let host = null;
  try { host = urlSet ? new URL(DATABASE_URL).host : null; } catch { host = "parse-error"; }

  const diag = { urlSet, host, ready, lastError };
  if (!pool) return diag;

  // Canli ping dene.
  try {
    const { rows } = await pool.query("SELECT now() AS ts");
    diag.ping = "ok";
    diag.serverTime = rows[0].ts;
    if (!ready) {            // pool var ama init basarisizdi -> tekrar dene
      await initDb();
      diag.reinit = ready;
    }
  } catch (e) {
    diag.ping = `${e.code ? e.code + ": " : ""}${e.message}`;
  }
  return diag;
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
          pump_duration_seconds, moisture_deficit, target_soil_moisture, decision_reason,
          target_deviation, absolute_target_deviation, target_status, decision_threshold_next,
          window_size, observations_in_current_window, threshold_update_applied,
          threshold_update_direction, threshold_old, threshold_new,
          threshold_window_average_soil_moisture, threshold_window_deviation_percent,
          sensor_quality_warning_active, sensor_quality_warning_type,
          sensor_quality_message, consecutive_extreme_count,
          last_irrigation_minutes_ago, light_lux)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30)`,
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
        d.targetDeviation ?? null,
        d.absoluteTargetDeviation ?? null,
        d.targetStatus ?? null,
        d.decisionThresholdNext ?? null,
        d.windowSize ?? null,
        d.observationsInCurrentWindow ?? null,
        d.thresholdUpdateApplied ?? null,
        d.thresholdUpdateDirection ?? null,
        d.thresholdOld ?? null,
        d.thresholdNew ?? null,
        d.thresholdWindowAverageSoilMoisture ?? null,
        d.thresholdWindowDeviationPercent ?? null,
        d.sensorQualityWarningActive ?? null,
        d.sensorQualityWarningType ?? null,
        d.sensorQualityMessage ?? null,
        d.consecutiveExtremeCount ?? null,
        d.lastIrrigationMinutesAgo ?? null,
        d.lightLux ?? null,
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
export async function recentDecisions(limit = 2000) {
  if (!ready) return [];
  try {
    const { rows } = await pool.query(
      `SELECT * FROM decisions ORDER BY ts DESC LIMIT $1`,
      [limit]
    );
    return rows;
  } catch (e) {
    console.warn(`[DB] recentDecisions hatasi: ${e.message}`);
    return [];
  }
}
