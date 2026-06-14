# Sulama Algoritması Servisi

Endüstri ekibinden gelen `final_irrigation_algorithm_standalone.py` algoritmasını
HTTP servis olarak sunar. Node backend (`../server.js`) bu servisi çağırır.

## Mimari

```
ESP32 --/ingest--> server.js (Node) --POST /predict--> app.py (Flask) --> algoritma
                                     <-- karar (ON/OFF + pompa süresi) --
```

Algoritma dosyası **değiştirilmez**, sadece import edilir.

## Çalıştırma

```bash
cd backend/algo_service
pip install -r requirements.txt
python app.py          # http://localhost:5001
```

## Soil sensörü notu (önemli)

Donanımda toprak nem sensörü **yok**. DHT22 sadece hava nemi + sıcaklık verir.
Algoritmanın ANA girdisi olan `soil_moisture`, geçici olarak **hava nemi (proxy)**
ile doldurulur (`server.js` içinde). Bu bilimsel olarak ideal değildir; ileride
capacitive toprak nem sensörü eklenince proxy kaldırılmalıdır.
Bkz. `decision_criteria_list.md` → "Toprak nem sensörü zorunludur".

Model katsayıları zaten `soil_moisture` (-0.78) ve `temperature` (+0.74) ağırlıklı;
hava nemi ve basınç ağırlıkları ~0 olduğu için proxy demoda çalışır.

## Endpoint

| Method | Path | Açıklama |
|---|---|---|
| GET | `/health` | servis ayakta mı |
| POST | `/predict` | sensör verisi -> karar |

`POST /predict` body örneği `sample_input.json` ile aynıdır.
