# Akıllı Tarım — Frontend

Backend'den (Render) canlı veri çekip gösteren tek dosyalık panel. Saf HTML/CSS/JS.

## Backend URL'sini ayarla

[index.html](index.html) içinde, script bloğunun başında:

```js
const BACKEND_URL = "https://kmt-bitirme1.onrender.com";
```

Render'ın verdiği gerçek URL ile değiştir (sonunda `/` olmasın).

## Nasıl çalışır

- Her 3 saniyede `GET <BACKEND_URL>/api` → kartlar, durum şeridi, nem grafiği güncellenir.
- "Pompa Aç / Kapat / Otomatik" butonları `POST <BACKEND_URL>/command` gönderir.
  Komutu ESP32 bir sonraki döngüde (≤3 sn) çekip uygular.
- Backend kapalı/uykudaysa durum şeridi kırmızı "Bağlantı yok" gösterir.

## Lokal test

Dosyayı çift tıkla ya da basit sunucu:
```bash
cd kmt_bitirme1/arayuz
python -m http.server 8080   # http://localhost:8080
```

## GitHub Pages'e yayınla

1. Repoyu GitHub'a push et.
2. Repo > **Settings** > **Pages**.
3. **Source: Deploy from a branch**, branch `main`, klasör `/root` (veya `index.html`'i
   repo köküne/`docs/`'a koy; `arayuz/` alt klasörse Pages onu da sunar:
   `https://<kullanici>.github.io/<repo>/kmt_bitirme1/arayuz/`).
4. Birkaç dakikada canlı.

> Not: GitHub Pages HTTPS sunar. Backend de HTTPS (Render) olduğu için karışık-içerik
> (mixed content) sorunu yok.
