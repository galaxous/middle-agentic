# 2026-11-23 · İstemci tarafı: iş etiketi, istek bütçesi, kullanım komutları · main'de (0337420)

## Durum

`main'de` (0337420), v0.5.0 yayınlandı. Yalnızca istemci (SDK ve `magent` CLI). Gateway tarafı aynı sürümde yayınlandı ([2026-11-23-gateway-usage-budget.md](2026-11-23-gateway-usage-budget.md)). Bu komutlar `/admin` uçları olan bir gateway (v0.5.0+) gerektirir; eski gateway'de 404 döner.

## Sorun

- Otonom işçilerin harcamasını işe göre ayırmak için istemcinin bir iş etiketi gönderebilmesi gerekiyor.
- Tek bir istek için bütçe sınırı koymak (ör. bir haber özeti için $0.05) istemci tarafından ifade edilemiyordu.
- Harcama ve model fiyatlarını terminalden görmek için yetkili bir okuma yolu yoktu.

## Değişenler

- `client/sdk.py`:
  - `chat()` ve `stream()` yeni anahtar argümanlar alır: `job` (başlık `X-Job`), `budget_usd` (başlık `X-Budget-USD`, negatifse `ValueError`).
  - Başlıkları `_gateway_headers()` üretir; eski davranış (yalnız `X-Session-Id` ve `X-Memory`) aynen korunur.
  - Yeni salt okunur admin çağrıları: `usage(by, days)`, `budget()`, `admin_models()`.
- `cli.py`:
  - `magent ask --job NAME --budget USD`.
  - `magent usage [--by tier|key|job|day] [--days N] [--json]`.
  - `magent budget [--json]`: "today: $X of $Y (on exceed: ...)" satırı.
  - `magent models [--json]`: tier, etkin/devre dışı, model, fiyat in/out, geçersiz kılındı mı.
- `tests/test_client_admin.py`: 10 yeni test. Gateway'e bağlanmaz; `httpx.MockTransport` kullanır.

## Doğrulama

- `./venv/bin/python -m pytest -q`: 190 geçiyor (önceki 180 + 10 yeni).
- Kapsam: başlıkların doğru gönderilmesi, bütçe başlığının yalnız verildiğinde eklenmesi, negatif bütçenin istek göndermeden reddedilmesi, admin uçlarının yolu ve parametreleri, CLI tablo ve JSON çıktıları, 403'ün hata olarak raporlanması.
- Canlı gateway ile test edilmedi (kasıtlı; gateway tarafı manuel test edilecek).

## Açık kalanlar

- Gateway v0.5.0: `X-Job` / `X-Budget-USD` işleme, `usage` kaydı, `/admin/usage`, `/admin/budget`, `/admin/models`, `admin` kapsamı. Plan dosyasındaki Faz 1.
- Admin yazma komutları (`models enable/disable/set`, `budget set`) bilerek eklenmedi; gateway uçları hazır olunca eklenecek.
- README'deki komut listesi bu komutları henüz içermiyor.
