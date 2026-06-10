# Akıllı Tarım — Backend

ESP32'den gelen sensör verisini toplar, frontend'e sunar. Render.com'da çalışır.

## Mimari

```
ESP32  --POST /ingest-->  Render backend  <--GET /api--  GitHub Pages frontend
ESP32  --GET /command-->  Render backend  <--POST /command--  frontend (pompa butonu)
```

Veri **RAM**'de tutulur (son durum + son 60 ölçüm). Render restart/uyku olunca silinir,
ESP32 saniyeler içinde tekrar doldurur.

## API

| Method | Yol         | Kim       | Açıklama |
|--------|-------------|-----------|----------|
| GET    | `/`         | herkes    | Sağlık kontrolü |
| POST   | `/ingest`   | ESP32     | Sensör verisi yazar. Header `x-token: <INGEST_TOKEN>`. Cevapta bekleyen komut döner. |
| GET    | `/api`      | frontend  | Anlık durum + geçmiş (JSON) |
| POST   | `/command`  | frontend  | Pompa komutu bırakır. Body `{"command":"on\|off\|auto"}` |
| GET    | `/command`  | ESP32     | Bekleyen komutu çeker |

### `GET /api` örnek cevap
```json
{
  "humidity": 28.4,
  "temperature": 31.2,
  "pump": true,
  "pumpManual": false,
  "greenLed": true,
  "updatedAt": "2026-06-10T12:00:00.000Z",
  "online": true,
  "history": [{ "t": "...", "humidity": 28.4, "temperature": 31.2 }]
}
```

## Lokal çalıştırma

```bash
cd kmt_bitirme1/backend
npm install
npm start          # http://localhost:3000
```

Test:
```bash
curl http://localhost:3000/api
curl -X POST http://localhost:3000/ingest -H "Content-Type: application/json" \
  -d '{"humidity":42,"temperature":25,"pump":false,"pumpManual":false,"greenLed":false}'
```

## Render'a deploy

1. Bu repoyu GitHub'a push et.
2. [render.com](https://render.com) > **New** > **Blueprint** > repoyu seç. `render.yaml` otomatik bulunur.
3. Deploy bitince Render bir URL verir: `https://akilli-tarim-backend-xxxx.onrender.com`
4. **Environment** sekmesinden `INGEST_TOKEN` değerini kopyala — ESP32 firmware'ine aynısını yaz.

> Bedava plan: 15 dk inaktiflikte uyur (ilk istek ~30 sn gecikir). ESP32 sürekli
> POST attığı için pratikte uyanık kalır.
