# Değişiklik günlüğü

Her özellik veya önemli değişiklik, **commit atılmadan önce** bu klasöre tarihli bir dosyayla işlenir.

## Kurallar

- Dosya adı: `YYYY-MM-DD-kisa-konu.md` (tarih, değişikliğin yapıldığı gündür).
- Başlık, durumu tek satırda özetler: `# 2026-11-21 · API token yönetimi · commit bekliyor`.
- Her dosyada şu bölümler bulunur: **Durum**, **Değişenler**, **Doğrulama**, **Açık kalanlar**.
- Durum alanı şu değerlerden biridir: `commit bekliyor`, `main'de`, `iptal edildi`. Commit hash'i yazıldığında durum `main'de` olur ve hash eklenir.
- Yeni bir özellik eklenirken önce ilgili tarihli dosya oluşturulur, sonra kod commit'lenir. Eski dosyalar düzenlenmez; düzeltme gerekiyorsa yeni bir dosya açılır.

## Dizin

| Tarih | Konu | Durum |
| --- | --- | --- |
| [2026-11-19](2026-11-19-memory-search-and-forget.md) | FTS5 arama, hafıza silme | `main'de` (0c74270, 5616bd8, b69e002) |
| [2026-11-20](2026-11-20-codex-antigravity-integrations.md) | Codex ve Antigravity hook'ları, Continue MCP | `main'de` (b23207f) |
| [2026-11-21](2026-11-21-api-token-management.md) | `magent token`, shell profil kurulumu, README | `commit bekliyor` |
