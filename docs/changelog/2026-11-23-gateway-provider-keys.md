# 2026-11-23 · Gateway sağlayıcı anahtarları: düz metin .env yerine Keychain · commit bekliyor

## Durum

`commit bekliyor`. Kod ve testler hazır (180 geçiyor). Yayın (v0.4.2) henüz yapılmadı.

## Sorun

- Gateway sağlayıcı anahtarlarını (Gemini, OpenRouter, OpenAI, Anthropic) yalnızca `.env` içindeki ortam değişkenlerinden okuyordu. Bu anahtarlar düz metin olarak duruyordu.
- `magent ask` gateway'e kendi makinesinden de anahtarla bağlanıyor; gateway Mac'te bu anahtar yoktu, `401` alındı.

## Değişenler

- `tokens.provider_secret(env_name)`: önce ortam değişkeni, yoksa işletim sistemi deposundaki (Keychain / DPAPI / Linux) ilgili sağlayıcı kaydı. Süreç başına bir kez okunur, değer hiçbir yere yazılmaz.
- `providers/registry.py`: model çağrısı ve kullanılabilirlik kontrolü bu çözümleyiciyi kullanır.
- Testler gerçek Keychain'i okumaz (fixture ile izole). Yeni testler: öncelik sırası, önbellek, bilinmeyen değişken, kilitli depo.

## Doğrulama

- Testler: 180 geçiyor.
- Gateway Mac'te henüz denenmedi; Keychain'deki anahtarlarla gerçek model çağrısı yapılmadı.

## Gateway Mac adımları (v0.4.2 yayınlandıktan sonra)

1. Sağlayıcı anahtarlarını Keychain'e taşıyın (değerler ekrana yazılmaz, prompt'a yapıştırırsınız):
   ```bash
   ~/.local/bin/magent token set gemini
   ~/.local/bin/magent token set openrouter
   ~/.local/bin/magent token set openai
   ```
2. Gateway'in kendi istemci anahtarını oluşturun ve Keychain'e koyun (yerel `magent ask` için):
   ```bash
   ~/.local/bin/magent key create gateway-local --scopes memory:read,memory:write,models
   ~/.local/bin/magent token set gateway
   ```
   `key create` çıktısındaki anahtarı `token set gateway` prompt'una yapıştırın.
3. Servisi yeniden başlatın (yeni sürüm kodu için): `~/.local/bin/magent service install`.
4. `~/.config/middle-agentic/.env` içinden sağlayıcı anahtarı satırlarını silin (`GEMINI_API_KEY=`, `OPENROUTER_API_KEY=`, `OPENAI_API_KEY=`, `ANTHROPIC_API_KEY=`). Silmeden önce Keychain'e kaydettiğinizden emin olun.
5. Kontrol: `magent health` (katmanlar `available: true`) ve `magent ask "Sadece 'tamam' yaz" --model local --no-memory`.

## Açık kalanlar

- Keychain'deki sağlayıcı anahtarlarını gateway launchd servisi okuyabilmeli (kullanıcı oturumu açıkken). Okuyamazsa `ask` ve model katmanları anahtar eksik diye atlanır; `magent health` bunu `reason` alanında gösterir.
- Anahtar rotasyonu için ayrı bir komut yok; `magent token set <sağlayıcı>` üzerine yazar.
