"""
Adaptif Sulama Controller — Kalıcı State Saklama Katmanı (Render Postgres)
==========================================================================

Python algo servisi (app.py) varsayılan olarak STATELESS çalışır: gunicorn
birden çok worker açabilir ve Render free plan boşta kalınca süreci uyutur.
Her iki durumda da bellekteki adaptif eşik / sensör uyarı sayaçları kaybolur.

Bu modül, AdaptiveIrrigationController durumunu Postgres'te tek satırda tutar.
app.py her ölçümde:  state'i DB'den yükle -> ölç -> state'i DB'ye geri yaz.
Böylece worker sayısından ve restart'tan bağımsız kalıcılık sağlanır.

Yarış koşulu: state satırı `SELECT ... FOR UPDATE` ile kilitlenir; aynı anda
gelen iki ölçüm birbirinin gözlemini ezmez.

Graceful degrade: DATABASE_URL yoksa, psycopg kurulu değilse ya da DB'ye
ulaşılamazsa modül RAM moduna düşer (state_session boş state döndürür) —
Node tarafındaki db.js ile aynı desen. Servis DB olmadan da ayakta kalır,
yalnızca adaptif durum süreç belleğinde tutulur.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional

# psycopg (v3) opsiyonel: kurulu değilse RAM moduna düşülür.
try:
    from psycopg.types.json import Json
    from psycopg_pool import ConnectionPool

    _PSYCOPG_AVAILABLE = True
except Exception:  # ImportError vb.
    Json = None  # type: ignore
    ConnectionPool = None  # type: ignore
    _PSYCOPG_AVAILABLE = False


DATABASE_URL = os.environ.get("DATABASE_URL", "")
DEFAULT_CONTROLLER_ID = os.environ.get("CONTROLLER_ID", "default")

_pool: "Optional[ConnectionPool]" = None
_enabled: bool = False
_last_error: Optional[str] = None


def _build_pool() -> "Optional[ConnectionPool]":
    """Render Postgres TLS ister; self-signed olduğu için sslmode=require
    (şifreler ama sertifika doğrulamaz — db.js'teki rejectUnauthorized:false eşdeğeri)."""
    return ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=3,
        kwargs={"sslmode": "require"},
        open=False,
    )


def init_state_store() -> bool:
    """
    State tablosunu (idempotent) oluşturur ve DB'yi etkinleştirir.
    app.py import edilirken bir kez çağrılır. Başarısız olursa RAM moduna düşülür.
    """
    global _pool, _enabled, _last_error

    if not DATABASE_URL:
        _last_error = "DATABASE_URL yok"
        print("[STATE] DATABASE_URL yok — adaptif state RAM modunda (kalıcı değil).")
        return False
    if not _PSYCOPG_AVAILABLE:
        _last_error = "psycopg kurulu değil"
        print("[STATE] psycopg bulunamadı — adaptif state RAM modunda (kalıcı değil).")
        return False

    try:
        _pool = _build_pool()
        _pool.open(wait=True, timeout=10.0)
        with _pool.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS controller_state (
                    id          TEXT PRIMARY KEY,
                    state       JSONB NOT NULL,
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
        _enabled = True
        _last_error = None
        print("[STATE] Postgres bağlandı; controller_state tablosu hazır (kalıcı adaptif state).")
        return True
    except Exception as exc:  # bağlanamadı / yetki / DNS ...
        _last_error = f"{type(exc).__name__}: {exc}"
        _enabled = False
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None
        print(f"[STATE] init hatası: {_last_error} — RAM moduna düşüldü.")
        return False


def db_enabled() -> bool:
    return _enabled


def diagnostics() -> Dict[str, Any]:
    host = None
    if DATABASE_URL:
        try:
            from urllib.parse import urlparse

            host = urlparse(DATABASE_URL).hostname
        except Exception:
            host = "parse-error"
    return {
        "url_set": bool(DATABASE_URL),
        "psycopg_available": _PSYCOPG_AVAILABLE,
        "enabled": _enabled,
        "host": host,
        "last_error": _last_error,
        "controller_id": DEFAULT_CONTROLLER_ID,
    }


class _Session:
    """state_session() içinde verilen oturum. `state` yüklü durumdur (veya None),
    `save(...)` yeni durumu kilitli işlem içinde yazılmak üzere kaydeder."""

    def __init__(self, state: Optional[Dict[str, Any]]) -> None:
        self.state: Optional[Dict[str, Any]] = state
        self._pending: Optional[Mapping[str, Any]] = None

    def save(self, new_state: Mapping[str, Any]) -> None:
        self._pending = new_state


@contextmanager
def state_session(controller_id: str = DEFAULT_CONTROLLER_ID) -> Iterator[_Session]:
    """
    Kalıcı state için kilitli bir DB oturumu açar.

    DB etkinse: ilgili satırı FOR UPDATE ile kilitler, mevcut state'i `session.state`
    olarak verir; blok bittiğinde `session.save(...)` ile verilen state upsert edilir
    ve işlem commit edilir. Hata olursa rollback (gözlem kaybolmaz, lock serbest kalır).

    DB kapalıysa: state=None döndürür, save() no-op'tur. Çağıran taraf süreç belleğindeki
    controller'a düşer.
    """
    if not _enabled or _pool is None:
        yield _Session(None)
        return

    with _pool.connection() as conn:
        with conn.transaction():
            cur = conn.execute(
                "SELECT state FROM controller_state WHERE id = %s FOR UPDATE",
                (controller_id,),
            )
            row = cur.fetchone()
            current_state = row[0] if row else None  # JSONB -> dict (psycopg otomatik)

            session = _Session(current_state)
            yield session

            if session._pending is not None:
                conn.execute(
                    """
                    INSERT INTO controller_state (id, state, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (id)
                    DO UPDATE SET state = EXCLUDED.state, updated_at = now()
                    """,
                    (controller_id, Json(session._pending)),
                )
        # conn.transaction() çıkışında commit; lock serbest kalır.
