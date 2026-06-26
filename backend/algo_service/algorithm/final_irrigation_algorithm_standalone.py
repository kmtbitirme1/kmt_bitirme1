"""
Final Sulama Karar ve Optimizasyon Algoritması - Standalone Sürüm
-------------------------------------------------------------------

Bu dosya tek başına çalışır. Ayrı bir Logistic Regression model dosyası gerektirmez.
Model katsayıları, ölçekleme değerleri ve karar eşiği doğrudan bu dosyanın içine gömülüdür.

Algoritma iki katmandan oluşur:
1. Karar katmanı: Sensör verilerinden ON olasılığı hesaplanır ve karar eşiğiyle sulama kararı verilir.
2. Optimizasyon katmanı: Sulama gerekiyorsa nem açığına göre pompa çalışma süresi hesaplanır.

Ek olarak bu sürümde adaptif kontrol katmanı bulunmaktadır.
Gerçek sistemde ana kullanım AdaptiveIrrigationController sınıfıdır.
Bu katman, karar eşiğini belirli gözlem aralıkları sonunda gerçek hedef nem sapmasına göre günceller.
predict_irrigation fonksiyonu tekil ölçüm/test amaçlı yardımcı fonksiyon olarak korunmuştur.

Bu dosya arkadaşlara verilecek ana algoritma dosyasıdır.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Mapping
from pathlib import Path
import csv
import json
import math
from datetime import datetime


# ============================================================
# 1. MODEL SABİTLERİ
# ============================================================

MODEL_FEATURES = [
    "Soil_Moisture",
    "Temperature",
    "Air_humidity_pct",
    "Pressure_KPa",
]

# StandardScaler ortalamaları
SCALER_MEAN = [
    45.433090227130656,
    22.492227547405708,
    58.52105188580954,
    101.13141821212753,
]

# StandardScaler standart sapma değerleri
SCALER_SCALE = [
    26.007173042265276,
    13.28271661038062,
    30.072821403765282,
    0.2184434734277875,
]

# Logistic Regression katsayıları
MODEL_COEFFICIENTS = [
    -0.7745651265412601,
    0.7426499310497418,
    -0.015164651317430384,
    -0.020784588977389953,
]

# Logistic Regression sabiti
MODEL_INTERCEPT = 0.20465532647535664


# ============================================================
# 2. GİRDİ ANAHTARLARI
# ============================================================

INPUT_KEY_ALIASES = {
    "Soil_Moisture": ["Soil_Moisture", "soil_moisture"],
    "Temperature": ["Temperature", "temperature"],
    "Air_humidity_pct": ["Air_humidity_pct", "air_humidity_pct", "air_humidity"],
    "Pressure_KPa": ["Pressure_KPa", "pressure_kpa", "pressure"],
}

OPTIONAL_INPUTS = {
    "light_lux": ["light_lux", "light_level", "Light_Lux"],
    "last_irrigation_minutes_ago": ["last_irrigation_minutes_ago", "minutes_since_last_irrigation"],
}

# Yüzde işaretiyle gelebilecek alanlar.
# Sensör/API entegrasyonunda sayısal değer gönderilmesi önerilir.
# Ancak kullanıcı arayüzü veya ara katmanda "55%" gibi string gelirse yalnızca bu alanlarda kabul edilir.
PERCENT_COMPATIBLE_FIELDS = {
    "Soil_Moisture",
    "soil_moisture",
    "Air_humidity_pct",
    "air_humidity_pct",
    "air_humidity",
}


# ============================================================
# 3. ALGORİTMA PARAMETRELERİ
# ============================================================

@dataclass(frozen=True)
class IrrigationConfig:
    # Logistic Regression karar eşiği
    decision_threshold: float = 0.40

    # Hedef toprak nem seviyesi
    target_soil_moisture: float = 55.0

    # Aynı noktaya çok sık sulama yapılmasını engelleyen minimum bekleme süresi
    minimum_irrigation_interval_minutes: float = 30.0

    # Nem açığı başına pompa çalışma süresi katsayısı
    seconds_per_moisture_point: float = 0.20

    # Pompa çalışma süresi sınırları
    min_pump_duration_seconds: float = 1.0
    max_pump_duration_seconds: float = 8.0

    # Satın alınan pompanın nominal akış aralığı
    # Bu bilgi raporlama ve kalibrasyon içindir.
    pump_flow_lph_min: float = 80.0
    pump_flow_lph_max: float = 120.0

    def validate(self) -> None:
        if not 0 < self.decision_threshold < 1:
            raise ValueError("decision_threshold 0 ile 1 arasında olmalıdır.")
        if not 0 < self.target_soil_moisture <= 100:
            raise ValueError("target_soil_moisture 0 ile 100 arasında olmalıdır.")
        if self.minimum_irrigation_interval_minutes < 0:
            raise ValueError("minimum_irrigation_interval_minutes negatif olamaz.")
        if self.seconds_per_moisture_point <= 0:
            raise ValueError("seconds_per_moisture_point pozitif olmalıdır.")
        if self.min_pump_duration_seconds < 0:
            raise ValueError("min_pump_duration_seconds negatif olamaz.")
        if self.max_pump_duration_seconds <= 0:
            raise ValueError("max_pump_duration_seconds pozitif olmalıdır.")
        if self.min_pump_duration_seconds > self.max_pump_duration_seconds:
            raise ValueError("min_pump_duration_seconds max_pump_duration_seconds değerinden büyük olamaz.")




@dataclass(frozen=True)
class AdaptiveThresholdConfig:
    """Karar eşiğinin gerçek sistem geri bildirimiyle güncellenmesi için ayarlar."""

    # Eşik güncellemesi yapılmadan önce aynı eşikle değerlendirilecek gözlem sayısı.
    update_window_size: int = 600

    # Hedef nemden sapma oranı bu değerin üzerindeyse iki kademe güncelleme yapılır.
    large_deviation_percent: float = 10.0

    # Küçük ve büyük güncelleme adımları.
    small_threshold_step: float = 0.01
    large_threshold_step: float = 0.02

    # Karar eşiğinin güvenli sınırları.
    min_decision_threshold: float = 0.20
    max_decision_threshold: float = 0.70

    # Toprak nemi 0 veya 100 değerini art arda bu kadar kez üretirse uyarı aktif olur.
    extreme_moisture_warning_count: int = 10

    def validate(self) -> None:
        if self.update_window_size <= 0:
            raise ValueError("update_window_size pozitif olmalıdır.")
        if self.large_deviation_percent <= 0:
            raise ValueError("large_deviation_percent pozitif olmalıdır.")
        if self.small_threshold_step <= 0:
            raise ValueError("small_threshold_step pozitif olmalıdır.")
        if self.large_threshold_step <= 0:
            raise ValueError("large_threshold_step pozitif olmalıdır.")
        if self.small_threshold_step > self.large_threshold_step:
            raise ValueError("small_threshold_step, large_threshold_step değerinden büyük olamaz.")
        if not 0 < self.min_decision_threshold < self.max_decision_threshold < 1:
            raise ValueError("Karar eşiği sınırları 0 ile 1 arasında ve sıralı olmalıdır.")
        if self.extreme_moisture_warning_count <= 0:
            raise ValueError("extreme_moisture_warning_count pozitif olmalıdır.")


@dataclass
class AdaptiveIrrigationState:
    """Adaptif eşik ve sensör uyarısı için çalışma zamanı durumu."""

    current_decision_threshold: float = 0.40
    window_soil_moisture_values: list[float] = field(default_factory=list)
    consecutive_extreme_value: float | None = None
    consecutive_extreme_count: int = 0
    last_threshold_update: Dict[str, Any] | None = None

# ============================================================
# 4. YARDIMCI FONKSİYONLAR
# ============================================================

def _to_float(value: Any, field_name: str) -> float:
    original_value = value

    if isinstance(value, bool):
        raise ValueError(f"{field_name} bool olamaz. Gelen değer: {value!r}")

    if isinstance(value, str):
        value = value.strip().replace(",", ".")

        if value.endswith("%"):
            if field_name not in PERCENT_COMPATIBLE_FIELDS:
                raise ValueError(
                    f"{field_name} yüzde işaretiyle gönderilemez. Gelen değer: {original_value!r}"
                )
            value = value[:-1].strip()

    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} sayısal olmalıdır. Gelen değer: {original_value!r}")

    if math.isnan(numeric_value) or math.isinf(numeric_value):
        raise ValueError(f"{field_name} geçerli bir sayı olmalıdır. Gelen değer: {original_value!r}")

    return numeric_value


def _get_required_value(sensor_data: Mapping[str, Any], feature_name: str) -> float:
    aliases = INPUT_KEY_ALIASES[feature_name]

    for key in aliases:
        if key in sensor_data:
            return _to_float(sensor_data[key], key)

    raise ValueError(f"Girdi eksik: {feature_name}. Kabul edilen anahtarlar: {aliases}")


def _get_optional_value(sensor_data: Mapping[str, Any], optional_name: str) -> float | None:
    aliases = OPTIONAL_INPUTS[optional_name]

    for key in aliases:
        if key in sensor_data and sensor_data[key] is not None:
            return _to_float(sensor_data[key], key)

    return None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def build_model_input(sensor_data: Mapping[str, Any]) -> Dict[str, float]:
    model_input = {
        feature: _get_required_value(sensor_data, feature)
        for feature in MODEL_FEATURES
    }
    _validate_model_input_ranges(model_input)
    return model_input


def _validate_model_input_ranges(model_input: Mapping[str, float]) -> None:
    soil_moisture = model_input["Soil_Moisture"]
    temperature = model_input["Temperature"]
    air_humidity = model_input["Air_humidity_pct"]
    pressure = model_input["Pressure_KPa"]

    if not 0 <= soil_moisture <= 100:
        raise ValueError(
            "Soil_Moisture 0-100 aralığında normalize edilmiş değer olmalıdır. "
            f"Gelen değer: {soil_moisture}"
        )

    if not -20 <= temperature <= 80:
        raise ValueError(
            "Temperature °C cinsinden beklenir ve -20 ile 80 arasında olmalıdır. "
            f"Gelen değer: {temperature}"
        )

    if not 0 <= air_humidity <= 100:
        raise ValueError(
            "Air_humidity_pct 0-100 aralığında yüzde nem değeri olmalıdır. "
            f"Gelen değer: {air_humidity}"
        )

    if not 80 <= pressure <= 120:
        raise ValueError(
            "Pressure_KPa kPa cinsinden beklenir ve 80-120 aralığında olmalıdır. "
            f"Gelen değer: {pressure}"
        )


# ============================================================
# 5. KARAR MODELİ
# ============================================================

def calculate_on_probability(model_input: Mapping[str, float]) -> float:
    """
    Logistic Regression modelinin ON olasılığını manuel olarak hesaplar.
    Ayrı model dosyası gerekmez.
    """
    standardized_values = []

    for index, feature in enumerate(MODEL_FEATURES):
        value = model_input[feature]
        standardized_value = (value - SCALER_MEAN[index]) / SCALER_SCALE[index]
        standardized_values.append(standardized_value)

    logit = MODEL_INTERCEPT

    for coefficient, standardized_value in zip(MODEL_COEFFICIENTS, standardized_values):
        logit += coefficient * standardized_value

    on_probability = 1 / (1 + math.exp(-logit))
    return float(on_probability)


# ============================================================
# 6. POMPA SÜRESİ OPTİMİZASYONU
# ============================================================

def calculate_pump_duration_seconds(
    soil_moisture: float,
    config: IrrigationConfig,
) -> tuple[float, float]:
    """
    Nem açığına göre pompa çalışma süresini hesaplar.

    moisture_deficit = target_soil_moisture - soil_moisture
    raw_duration = moisture_deficit * seconds_per_moisture_point
    pump_duration = min/max sınırları içinde tutulur.
    """
    moisture_deficit = max(0.0, config.target_soil_moisture - soil_moisture)

    if moisture_deficit <= 0:
        return 0.0, 0.0

    raw_duration = moisture_deficit * config.seconds_per_moisture_point
    pump_duration = _clamp(
        raw_duration,
        config.min_pump_duration_seconds,
        config.max_pump_duration_seconds,
    )

    return round(pump_duration, 2), round(moisture_deficit, 2)


# ============================================================
# 7. FİNAL ALGORİTMA
# ============================================================

def predict_irrigation(
    sensor_data: Mapping[str, Any],
    config: IrrigationConfig | None = None,
) -> Dict[str, Any]:
    """
    Sensör verilerine göre sulama kararı ve pompa çalışma süresini üretir.
    """
    if config is None:
        config = IrrigationConfig()

    config.validate()

    model_input = build_model_input(sensor_data)
    soil_moisture = model_input["Soil_Moisture"]

    light_lux = _get_optional_value(sensor_data, "light_lux")
    last_irrigation_minutes_ago = _get_optional_value(sensor_data, "last_irrigation_minutes_ago")

    if light_lux is not None and light_lux < 0:
        raise ValueError(f"light_lux negatif olamaz. Gelen değer: {light_lux}")

    if last_irrigation_minutes_ago is not None and last_irrigation_minutes_ago < 0:
        raise ValueError(
            "last_irrigation_minutes_ago negatif olamaz. "
            f"Gelen değer: {last_irrigation_minutes_ago}"
        )

    on_probability = calculate_on_probability(model_input)
    model_says_on = on_probability >= config.decision_threshold

    moisture_deficit = round(max(0.0, config.target_soil_moisture - soil_moisture), 2)

    # 1. Model OFF diyorsa pompa çalışmaz.
    if not model_says_on:
        return {
            "irrigation_required": False,
            "decision_label": "OFF",
            "on_probability": round(on_probability, 6),
            "decision_threshold": config.decision_threshold,
            "pump_duration_seconds": 0.0,
            "moisture_deficit": moisture_deficit,
            "decision_reason": (
                f"ON olasılığı {on_probability:.3f}, "
                f"{config.decision_threshold:.2f} karar eşiğinin altındadır."
            ),
            "model_input": model_input,
            "operational_inputs": {
                "light_lux": light_lux,
                "last_irrigation_minutes_ago": last_irrigation_minutes_ago,
            },
            "config": asdict(config),
        }

    # 2. Toprak nemi hedefe ulaşmışsa pompa çalışmaz.
    if soil_moisture >= config.target_soil_moisture:
        return {
            "irrigation_required": False,
            "decision_label": "OFF",
            "on_probability": round(on_probability, 6),
            "decision_threshold": config.decision_threshold,
            "pump_duration_seconds": 0.0,
            "moisture_deficit": 0.0,
            "decision_reason": (
                f"Model ON eğilimi göstermiştir ancak toprak nemi "
                f"hedef değer olan {config.target_soil_moisture:.1f} seviyesine ulaşmıştır."
            ),
            "model_input": model_input,
            "operational_inputs": {
                "light_lux": light_lux,
                "last_irrigation_minutes_ago": last_irrigation_minutes_ago,
            },
            "config": asdict(config),
        }

    # 3. Son sulamadan yeterince süre geçmediyse pompa çalışmaz.
    if (
        last_irrigation_minutes_ago is not None
        and last_irrigation_minutes_ago < config.minimum_irrigation_interval_minutes
    ):
        return {
            "irrigation_required": False,
            "decision_label": "OFF",
            "on_probability": round(on_probability, 6),
            "decision_threshold": config.decision_threshold,
            "pump_duration_seconds": 0.0,
            "moisture_deficit": moisture_deficit,
            "decision_reason": (
                f"Model ON eğilimi göstermiştir ancak son sulamadan yalnızca "
                f"{last_irrigation_minutes_ago:.1f} dakika geçmiştir."
            ),
            "model_input": model_input,
            "operational_inputs": {
                "light_lux": light_lux,
                "last_irrigation_minutes_ago": last_irrigation_minutes_ago,
            },
            "config": asdict(config),
        }

    # 4. Sulama gerekiyorsa pompa süresi hesaplanır.
    pump_duration_seconds, moisture_deficit = calculate_pump_duration_seconds(
        soil_moisture=soil_moisture,
        config=config,
    )

    return {
        "irrigation_required": True,
        "decision_label": "ON",
        "on_probability": round(on_probability, 6),
        "decision_threshold": config.decision_threshold,
        "pump_duration_seconds": pump_duration_seconds,
        "moisture_deficit": moisture_deficit,
        "decision_reason": (
            f"ON olasılığı {on_probability:.3f}, "
            f"{config.decision_threshold:.2f} karar eşiğinin üzerindedir. "
            f"Nem açığı {moisture_deficit:.2f} olarak hesaplanmış ve "
            f"pompa süresi {pump_duration_seconds:.2f} saniye belirlenmiştir."
        ),
        "model_input": model_input,
        "operational_inputs": {
            "light_lux": light_lux,
            "last_irrigation_minutes_ago": last_irrigation_minutes_ago,
        },
        "config": asdict(config),
    }



# ============================================================
# 8. ADAPTİF EŞİK GÜNCELLEME KATMANI
# ============================================================

class AdaptiveIrrigationController:
    """
    Gerçek sistemde ardışık ölçümler için kullanılacak adaptif kontrol katmanı.

    Bu sınıf predict_irrigation fonksiyonunun temel karar mantığını değiştirmez.
    Yalnızca karar eşiğini belirli gözlem pencereleri sonunda hedef nem sapmasına göre
    günceller ve 0/100 toprak nemi tekrarları için uyarı bilgisi üretir.
    """

    def __init__(
        self,
        config: IrrigationConfig | None = None,
        adaptive_config: AdaptiveThresholdConfig | None = None,
        state: AdaptiveIrrigationState | None = None,
    ) -> None:
        self.config = config or IrrigationConfig()
        self.adaptive_config = adaptive_config or AdaptiveThresholdConfig()
        self.state = state or AdaptiveIrrigationState(
            current_decision_threshold=self.config.decision_threshold
        )
        self.config.validate()
        self.adaptive_config.validate()
        if not self.adaptive_config.min_decision_threshold <= self.state.current_decision_threshold <= self.adaptive_config.max_decision_threshold:
            raise ValueError("current_decision_threshold adaptif eşik sınırlarının dışında olamaz.")

    def predict(self, sensor_data: Mapping[str, Any]) -> Dict[str, Any]:
        """
        Tek bir sensör ölçümü için karar üretir, kalite uyarısını günceller ve
        gerekirse bir sonraki pencere için karar eşiğini günceller.
        """
        threshold_used = self.state.current_decision_threshold
        runtime_config = replace(self.config, decision_threshold=threshold_used)

        result = predict_irrigation(sensor_data=sensor_data, config=runtime_config)
        soil_moisture = float(result["model_input"]["Soil_Moisture"])

        sensor_quality = self._update_sensor_quality_warning(soil_moisture)
        threshold_update = self._record_observation_and_update_threshold(soil_moisture)

        result["sensor_quality"] = sensor_quality
        result["adaptive_threshold"] = {
            "threshold_used_for_this_decision": round(threshold_used, 4),
            "current_threshold_for_next_decision": round(self.state.current_decision_threshold, 4),
            "window_size": self.adaptive_config.update_window_size,
            "observations_in_current_window": len(self.state.window_soil_moisture_values),
            "threshold_update_applied_after_this_observation": threshold_update["updated"],
            "last_threshold_update": self.state.last_threshold_update,
        }
        return result

    def process_measurement(self, sensor_data: Mapping[str, Any]) -> Dict[str, Any]:
        """
        Gerçek sistem entegrasyonu için predict metoduyla aynı işi yapan okunabilir alias.
        Her yeni sensör ölçümü bu metoda gönderilebilir.
        """
        return self.predict(sensor_data)

    def get_state_dict(self) -> Dict[str, Any]:
        """
        Adaptif sistem durumunu dışarı aktarır.

        Backend bu çıktıyı veritabanında saklayabilir. Böylece uygulama yeniden
        başlatıldığında karar eşiği, pencere ölçümleri ve sensör uyarı sayaçları
        sıfırlanmadan devam edebilir.
        """
        return asdict(self.state)

    def load_state_dict(self, state_dict: Mapping[str, Any]) -> None:
        """
        Daha önce kaydedilmiş adaptif sistem durumunu geri yükler.
        """
        current_threshold = float(
            state_dict.get(
                "current_decision_threshold",
                self.config.decision_threshold,
            )
        )
        if not self.adaptive_config.min_decision_threshold <= current_threshold <= self.adaptive_config.max_decision_threshold:
            raise ValueError("Kaydedilmiş current_decision_threshold adaptif eşik sınırlarının dışında.")

        window_values_raw = state_dict.get("window_soil_moisture_values", []) or []
        window_values = [float(value) for value in window_values_raw]

        consecutive_value = state_dict.get("consecutive_extreme_value", None)
        if consecutive_value is not None:
            consecutive_value = float(consecutive_value)

        self.state = AdaptiveIrrigationState(
            current_decision_threshold=current_threshold,
            window_soil_moisture_values=window_values,
            consecutive_extreme_value=consecutive_value,
            consecutive_extreme_count=int(state_dict.get("consecutive_extreme_count", 0) or 0),
            last_threshold_update=state_dict.get("last_threshold_update", None),
        )

    def _record_observation_and_update_threshold(self, soil_moisture: float) -> Dict[str, Any]:
        self.state.window_soil_moisture_values.append(soil_moisture)

        if len(self.state.window_soil_moisture_values) < self.adaptive_config.update_window_size:
            return {"updated": False, "reason": "Gözlem penceresi henüz tamamlanmadı."}

        values = self.state.window_soil_moisture_values
        average_soil_moisture = sum(values) / len(values)
        target = self.config.target_soil_moisture
        deviation = average_soil_moisture - target
        deviation_percent = abs(deviation) / target * 100 if target != 0 else 0.0

        old_threshold = self.state.current_decision_threshold
        if deviation == 0:
            step = 0.0
        elif deviation_percent > self.adaptive_config.large_deviation_percent:
            step = self.adaptive_config.large_threshold_step
        else:
            step = self.adaptive_config.small_threshold_step

        if deviation > 0:
            direction = "increase"
            reason = "Ortalama nem hedef nemin üstünde kaldı; daha seçici sulama için eşik artırıldı."
            new_threshold = old_threshold + step
        elif deviation < 0:
            direction = "decrease"
            reason = "Ortalama nem hedef nemin altında kaldı; daha kolay sulama kararı için eşik azaltıldı."
            new_threshold = old_threshold - step
        else:
            direction = "unchanged"
            reason = "Ortalama nem hedef neme eşit olduğu için eşik değiştirilmedi."
            new_threshold = old_threshold

        new_threshold = _clamp(
            new_threshold,
            self.adaptive_config.min_decision_threshold,
            self.adaptive_config.max_decision_threshold,
        )
        self.state.current_decision_threshold = round(new_threshold, 4)
        actual_threshold_change = abs(self.state.current_decision_threshold - round(old_threshold, 4)) > 1e-9

        if step > 0 and not actual_threshold_change:
            reason = (
                reason + 
                " Ancak karar eşiği güvenli alt/üst sınırda olduğu için değer değişmedi."
            )

        update_record: Dict[str, Any] = {
            "updated": actual_threshold_change,
            "direction": direction,
            "old_threshold": round(old_threshold, 4),
            "new_threshold": round(self.state.current_decision_threshold, 4),
            "average_soil_moisture": round(average_soil_moisture, 4),
            "target_soil_moisture": round(target, 4),
            "deviation": round(deviation, 4),
            "deviation_percent": round(deviation_percent, 4),
            "step": round(step, 4),
            "reason": reason,
            "window_size": len(values),
        }
        self.state.last_threshold_update = update_record
        self.state.window_soil_moisture_values = []
        return update_record

    def _update_sensor_quality_warning(self, soil_moisture: float) -> Dict[str, Any]:
        is_extreme = soil_moisture in (0.0, 100.0)

        if is_extreme:
            if self.state.consecutive_extreme_value == soil_moisture:
                self.state.consecutive_extreme_count += 1
            else:
                self.state.consecutive_extreme_value = soil_moisture
                self.state.consecutive_extreme_count = 1
        else:
            self.state.consecutive_extreme_value = None
            self.state.consecutive_extreme_count = 0

        warning_active = (
            self.state.consecutive_extreme_count
            >= self.adaptive_config.extreme_moisture_warning_count
        )

        message = None
        if warning_active:
            message = (
                f"Son {self.state.consecutive_extreme_count} gözlemde toprak nemi "
                f"{soil_moisture:.0f} olarak ölçülüyor. Daha güvenli sonuçlar için "
                "toprak nem sensörünün bağlantı ve kalibrasyon durumunun kontrol edilmesi önerilir."
            )

        return {
            "warning_active": warning_active,
            "warning_type": "consecutive_extreme_soil_moisture" if warning_active else None,
            "message": message,
            "consecutive_extreme_value": self.state.consecutive_extreme_value,
            "consecutive_extreme_count": self.state.consecutive_extreme_count,
            "note": "Bu uyarı yalnızca bilgilendirme amaçlıdır; sulama kararını değiştirmez.",
        }



# ============================================================
# 9. LOG KAYDI VE GERÇEK SİSTEM PERFORMANSI İÇİN YARDIMCI FONKSİYONLAR
# ============================================================

LOG_FIELDNAMES = [
    "timestamp",
    "measurement_id",
    "soil_moisture",
    "temperature",
    "air_humidity_pct",
    "pressure_kpa",
    "light_lux",
    "last_irrigation_minutes_ago",
    "target_soil_moisture",
    "target_deviation",
    "absolute_target_deviation",
    "target_status",
    "on_probability",
    "decision_threshold_used",
    "decision_threshold_next",
    "irrigation_required",
    "decision_label",
    "pump_duration_seconds",
    "moisture_deficit",
    "decision_reason",
    "window_size",
    "observations_in_current_window",
    "threshold_update_applied",
    "threshold_update_direction",
    "threshold_old",
    "threshold_new",
    "threshold_window_average_soil_moisture",
    "threshold_window_deviation_percent",
    "sensor_quality_warning_active",
    "sensor_quality_warning_type",
    "sensor_quality_message",
    "consecutive_extreme_count",
]


def build_log_record(
    result: Mapping[str, Any],
    timestamp: str | None = None,
    measurement_id: int | str | None = None,
) -> Dict[str, Any]:
    """
    Algoritma çıktısını CSV/veritabanı için düz bir performans log kaydına dönüştürür.

    Bu fonksiyon karar mantığını değiştirmez. Yalnızca gerçek sistem performansının
    sonradan hesaplanabilmesi için gerekli alanları tek satırlık kayıt haline getirir.
    Backend tarafı her ölçümden sonra bu kaydı veritabanına veya CSV dosyasına yazabilir.
    """
    model_input = result.get("model_input", {}) or {}
    operational_inputs = result.get("operational_inputs", {}) or {}
    config = result.get("config", {}) or {}
    adaptive_threshold = result.get("adaptive_threshold", {}) or {}
    sensor_quality = result.get("sensor_quality", {}) or {}
    last_update = adaptive_threshold.get("last_threshold_update") or {}

    soil_moisture = float(model_input.get("Soil_Moisture", 0.0))
    target = float(config.get("target_soil_moisture", 55.0))
    target_deviation = soil_moisture - target
    absolute_target_deviation = abs(target_deviation)

    if target_deviation < 0:
        target_status = "below_target"
    elif target_deviation > 0:
        target_status = "above_target"
    else:
        target_status = "on_target"

    threshold_update_applied = bool(
        adaptive_threshold.get("threshold_update_applied_after_this_observation", False)
    )

    record: Dict[str, Any] = {
        "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
        "measurement_id": measurement_id,
        "soil_moisture": round(soil_moisture, 4),
        "temperature": model_input.get("Temperature"),
        "air_humidity_pct": model_input.get("Air_humidity_pct"),
        "pressure_kpa": model_input.get("Pressure_KPa"),
        "light_lux": operational_inputs.get("light_lux"),
        "last_irrigation_minutes_ago": operational_inputs.get("last_irrigation_minutes_ago"),
        "target_soil_moisture": round(target, 4),
        "target_deviation": round(target_deviation, 4),
        "absolute_target_deviation": round(absolute_target_deviation, 4),
        "target_status": target_status,
        "on_probability": result.get("on_probability"),
        "decision_threshold_used": adaptive_threshold.get(
            "threshold_used_for_this_decision", result.get("decision_threshold")
        ),
        "decision_threshold_next": adaptive_threshold.get(
            "current_threshold_for_next_decision", result.get("decision_threshold")
        ),
        "irrigation_required": int(bool(result.get("irrigation_required", False))),
        "decision_label": result.get("decision_label"),
        "pump_duration_seconds": result.get("pump_duration_seconds"),
        "moisture_deficit": result.get("moisture_deficit"),
        "decision_reason": result.get("decision_reason"),
        "window_size": adaptive_threshold.get("window_size"),
        "observations_in_current_window": adaptive_threshold.get("observations_in_current_window"),
        "threshold_update_applied": int(threshold_update_applied),
        "threshold_update_direction": last_update.get("direction") if threshold_update_applied else None,
        "threshold_old": last_update.get("old_threshold") if threshold_update_applied else None,
        "threshold_new": last_update.get("new_threshold") if threshold_update_applied else None,
        "threshold_window_average_soil_moisture": last_update.get("average_soil_moisture") if threshold_update_applied else None,
        "threshold_window_deviation_percent": last_update.get("deviation_percent") if threshold_update_applied else None,
        "sensor_quality_warning_active": int(bool(sensor_quality.get("warning_active", False))),
        "sensor_quality_warning_type": sensor_quality.get("warning_type"),
        "sensor_quality_message": sensor_quality.get("message"),
        "consecutive_extreme_count": sensor_quality.get("consecutive_extreme_count"),
    }
    return record


def append_log_record(csv_path: str | Path, record: Mapping[str, Any]) -> None:
    """
    Tek bir performans log kaydını CSV dosyasına ekler.

    Dosya yoksa başlık satırıyla birlikte oluşturulur. Backend veritabanı kullanıyorsa
    aynı alanlar tablo kolonları olarak da saklanabilir.
    """
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists() and path.stat().st_size > 0

    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=LOG_FIELDNAMES, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow({field: record.get(field) for field in LOG_FIELDNAMES})

# ============================================================
# 10. ÖRNEK ÇALIŞTIRMA
# ============================================================

if __name__ == "__main__":
    # Doğrudan çalıştırıldığında adaptif kontrol katmanı gösterilir.
    # Gerçek sistemde de aynı AdaptiveIrrigationController nesnesi ardışık ölçümlerde kullanılmalıdır.
    controller = AdaptiveIrrigationController()

    # Demo amacıyla 600 ölçümlük bir pencere oluşturulur.
    # Ortalama nem hedef değer olan 55'in üstünde olduğu için 600. ölçüm sonunda eşik 0.01 artar.
    last_result: Dict[str, Any] | None = None
    for _ in range(controller.adaptive_config.update_window_size):
        sample_sensor_data = {
            "soil_moisture": 60.0,
            "temperature": 28.0,
            "air_humidity_pct": 60.0,
            "pressure_kpa": 101.2,
            "light_lux": 450.0,
            "last_irrigation_minutes_ago": 120.0,
        }
        last_result = controller.process_measurement(sample_sensor_data)

    demo_output = {
        "demo_type": "adaptive_controller_600_observation_demo",
        "note": "Bu çıktı, doğrudan çalıştırmada adaptif eşik güncelleme ve performans log katmanının kullanıldığını gösterir.",
        "last_result_after_600_measurements": last_result,
        "example_log_record": build_log_record(last_result, measurement_id=controller.adaptive_config.update_window_size),
    }
    print(json.dumps(demo_output, ensure_ascii=False, indent=2))
