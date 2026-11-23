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
| [2026-11-21](2026-11-21-api-token-management.md) | `magent token`, shell profil kurulumu, README | `main'de` (8023d27) |
| [2026-11-21](2026-11-21-integrate-all.md) | `magent integrate all` | `commit bekliyor` |
| [2026-11-21](2026-11-21-gateway-mcp-http.md) | Gateway'de HTTP MCP endpoint'i (`/mcp`) | `main'de` (d36e1c5) |
| [2026-11-22](2026-11-22-gateway-keys.md) | İstemci başına gateway anahtarları, Keychain, `GATEWAY_API_KEY` kaldırıldı | `commit bekliyor` |
| [2026-11-22](2026-11-22-key-scopes.md) | Anahtar kapsamları (okuma, yazma, model, silme) | `commit bekliyor` |
| [2026-11-23](2026-11-23-key-lifecycle-and-soft-delete.md) | Anahtar süresi, kotalar, istek kaydı, soft delete | `commit bekliyor` |
| [2026-11-23](2026-11-23-standalone-install.md) | Repo dışından çalışan kurulum (pipx, ev dizini, paketli hook) | `commit bekliyor` |
| [2026-11-23](2026-11-23-client-installer.md) | İstemci kurulum sihirbazı, Linux anahtar deposu, GitHub Release betikleri (v0.3.0) | `main'de` (feab0ce) |
| [2026-11-23](2026-11-23-gateway-setup.md) | Gateway'i repodan kurtarma (`magent gateway-setup`), v0.4.0 | `main'de` (643cddc) |
| [2026-11-23](2026-11-23-readme-install-flow.md) | README kurulum akışı: repo olmadan ağ, SSH, magent | `commit bekliyor` |
| [2026-11-23](2026-11-23-gateway-provider-keys.md) | Gateway sağlayıcı anahtarları: düz metin .env yerine Keychain | `commit bekliyor` |
| [2026-11-23](2026-11-23-client-usage-budget.md) | İstemci: `--job` / `--budget`, `magent usage`, `budget`, `models` (gateway v0.5.0 bekliyor) | `commit bekliyor` |
| [2026-11-23](2026-11-23-gateway-usage-budget.md) | Gateway: kullanım kaydı, günlük bütçe, admin uçları (v0.5.0) | `commit bekliyor` |
