# 2026-11-22 · Gateway anahtarlarında kapsamlar (scopes) · commit bekliyor

## Durum

`commit bekliyor`. Çalışma ağacında; `main` üzerinde commit yok. Önceki kayıt: `2026-11-22-gateway-keys.md`.

## Değişenler

- **Kapsamlar**: `memory:read`, `memory:write`, `memory:delete`, `models`. Yeni anahtarların varsayılanı `memory:read,memory:write`; model çağrısı ve silme açıkça verilmeli.
- **REST**: `/v1/chat/completions` ve `/v1/models` → `models`; `/v1/memory/context` ve `/sessions` → `memory:read`; `/v1/memory/events` → `memory:write`; `/v1/memory/forget` → `memory:delete`; `/v1/hooks/*` → `memory:read` + `memory:write`.
- **MCP**: gateway, `tools/call` isteklerinin araç adını kontrol eder. `ask` → `models`, `memory_search` → `memory:read`, `memory_record` → `memory:write`, `list_nodes` → yalnızca geçerli anahtar. Kontrol gövdeyi okuyup aynen geri verdiği için uzun süren oturumlarda da geçerlidir.
- **Kapalı başarısızlık**: kapsamı olmayan anahtarlar (kapsam öncesi veritabanlarından) geçersiz sayılır. Bilinmeyen kapsam ya da boş liste reddedilir.
- **CLI**: `magent key create <ad> --scopes ...`, `magent key set-scopes <ad> --scopes ...`, `magent key list` artık kapsamları gösterir.
- **Yetki yanıtları**: anahtar yok ya da geçersizse 401, anahtar var ama kapsam eksikse 403.

## Doğrulama

- Testler: 138 geçiyor. `tests/test_scopes.py` okuma, yazma, model, silme ve hook kısıtlarını; MCP araç çağrılarının kapsam kontrolünü; eski veritabanı migrasyonunu ve kapsam değişikliğinin anında etkisini kapsıyor.
- Geçici veritabanında CLI ile oluşturma, değiştirme ve listeleme denendi.

## Açık kalanlar

- Mevcut istemci anahtarları yeniden oluşturulmalı ya da `set-scopes` ile kapsam verilmeli. Cursor için `models` kapsamı gerekiyorsa (ask aracı) açıkça verin.
- Anahtar süresi (expiry) ve istek başına kota henüz yok.
- Anahtar başına istek kaydı (audit log) yalnızca `last_used_at` ile sınırlı.
