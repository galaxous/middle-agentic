# 2026-11-23 · Canlı katman/bütçe kontrolü ve katman başına eşzamanlılık · commit bekliyor

## Durum

`commit bekliyor`. Gateway ve client kodu yazıldı, testler geçiyor (220). Yayın henüz yapılmadı. Bu, v0.5.0'a ek bir sürümdür (öneri: v0.6.0, çünkü yeni admin yazma uçları ve davranış değişikliği içeriyor).

## Sorun

- v0.5.0'da katman ve bütçe ayarları yalnızca `config.yaml` ile değişiyordu; her değişiklik gateway'in yeniden başlatılmasını gerektiriyordu.
- Yerel model aynı anda birden fazla isteği işlemeye çalışıyordu; istekler birbirini yavaşlatıyor ve zaman aşımına uğruyordu.

## Değişenler

**Canlı değişiklikler** (`server/overrides.py`)
- Değişiklikler gateway'in SQLite dosyasında `tier_overrides` ve `runtime_settings` tablolarında tutulur. `config.yaml` değişmez.
- Her istekte depo sürümü kontrol edilir; değiştiyse etkin zincir yeniden kurulur. Yeniden başlatma gerekmez.
- Değiştirilebilen alanlar: `enabled`, `model`, `timeout`, `price_in`, `price_out`, `max_concurrency`, `queue_wait`, sıra; bütçe için `daily_usd` ve `on_exceed`.
- Yönetim uçları ve komutları: bkz. README "Kullanım ve günlük bütçe" ve API tablosu.

**Eşzamanlılık** (`providers/limits.py`, `providers/chain.py`)
- Her katmanın bir sınırlayıcısı var. Yerel katmanlarda varsayılan 1, bulutta sınır yok.
- Sınır doluysa istek `queue_wait` (varsayılan 30 sn) kadar bekler; sonra zincir bir sonraki katmana geçer ve `saturated` olarak kaydedilir.
- Akış istekleri, akış bitene kadar sınır yuvasını tutar; yuva bitiş kaydıyla (`record_stream`) serbest bırakılır, istemci bağlantıyı koparsa da.

**Admin API** (`server/routes_admin.py`)
- Okuma: `GET /admin/models` artık sıra, çalışan istek sayısı (`in_flight`) ve geçersiz kılma durumunu döndürür. `GET /admin/budget` kaynağı (`runtime` veya `config`) gösterir.
- Yazma: `PATCH`/`DELETE /admin/models/{ad}`, `PUT`/`DELETE /admin/models/order`, `DELETE /admin/overrides`, `PUT`/`DELETE /admin/budget`. Hepsi `admin` kapsamı ister ve erişim günlüğüne yazılır.
- Geçersiz değerler 400 veya 422, bilinmeyen katman adı 404 döner.

**Client** (`cli.py`, `client/sdk.py`)
- `magent models [list|enable|disable|set|reorder|reset]`, `magent budget [show|set|reset]`.
- SDK: `patch_model`, `reset_model`, `set_model_order`, `clear_model_order`, `clear_overrides`, `set_budget`, `clear_budget`.

**Yapılandırma** (`config.py`, `providers/registry.py`)
- `TierConfig.max_concurrency` (isteğe bağlı) ve `queue_wait` (varsayılan 30) eklendi.

## Doğrulama

- `./venv/bin/python -m pytest -q`: 220 geçiyor (önceki 203; 17 yeni).
- `tests/test_live_control.py` (13): katmanı kapatma ve geri açma, model değişikliği, sıra değişikliği ve sıfırlama, bütçe değişikliği ve sıfırlama, worker anahtarının reddedilmesi, geçersiz değerler, doygun katmanın atlanması, akışın yuvayı bitene kadar tutması, yerel varsayılan, sınırlayıcının kendi testleri.
- `tests/test_client_admin.py` (+4): istemci yazma isteklerinin doğru gövde ve yolla gitmesi, CLI komutları.
- Bu sırada yakalanan hatalar düzeltildi: `DELETE /admin/models/order` route'u `/models/{name}` tarafından gölgeleniyordu (sıra sıralaması düzeltildi); `set_order` doğrulamadan önce kaydediyordu (düzeltildi).

## Açık kalanlar

- Ücretsiz katman günlük istek sınırları ve soğuma (cooldown) henüz yok (plan Faz 1 maddesi).
- Katman ekleme/silme (yeni katman tanımı) çalışma zamanında yapılamıyor; yalnızca mevcut katmanlar değiştirilir.
- Kapasite bayrakları (`supports_tools`, context) ve profiller (`cheap-first`) yok.
- Bütçede tahmini rezervasyon yok; aşım payı hâlâ mevcut.
- Gateway kurulumu (v0.6.0 yayını sonrası) gateway makinesinde yapılacak; `DELETE /admin/overrides` ile tüm değişiklikler sıfırlanabilir.
