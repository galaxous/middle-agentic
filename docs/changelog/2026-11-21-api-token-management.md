# 2026-11-21 · API token yönetimi, shell profil kurulumu, README · commit bekliyor

## Durum

`commit bekliyor`. Commit edilmedi, `main` üzerinde çalışma ağacında duruyor. Testler 113 geçiyor.

## Değişenler

- **`magent token`** (`middle_agentic/tokens.py`, `middle_agentic/cli.py`):
  - `set <provider>`: değer gizli prompt'tan veya `--stdin` ile alınır.
  - `get <provider>`: yalnızca değeri yazdırır (`apiKeyHelper` ve `$(...)` için).
  - `delete`, `list` (değer göstermez), `env --shell zsh|bash|powershell`.
  - Sağlayıcılar: `anthropic`, `openai`, `gemini`, `openrouter`.
- **macOS**: Keychain, `security` üzerinden. Servis adı `<provider>-api-key`, hesap `$USER`. Mevcut `anthropic-api-key` kaydıyla uyumlu. Değer `security -i` stdin'i ile gider, argv'de görünmez.
- **Windows**: `%LOCALAPPDATA%\middle-agentic\secrets\<provider>.dpapi`. PowerShell `ConvertFrom-SecureString` ile DPAPI kullanır, değer yine stdin ile gider.
- **`magent token install-shell`**: profil dosyasının sonuna işaretli bir blok ekler.
  - zsh: `~/.zprofile`, bash: `~/.bash_profile` (macOS, `security` satırları).
  - powershell: `$PROFILE` (`Documents\PowerShell\Microsoft.PowerShell_profile.ps1`), `python -m middle_agentic token get` satırları.
  - Bloğun dışındaki `export`/`$env:` satırlarına dokunmaz. Düz metin değer varsa uyarır, değeri yazdırmaz.
  - Yazmadan önce `.bak` alır. `--dry-run` destekler. Tekrar çalıştırma değişiklik yapmaz.
- **README**: komut listesi, anahtar saklama bölümü, değişiklik günlüğü bağlantısı.

## Doğrulama

- Testler fake `security` ve fake `powershell` ile çalıştı. Gerçek Keychain ve gerçek DPAPI kullanılmadı.
- Gerçek Keychain okuma denemesi otomatik izin kontrolü tarafından engellendi; bu yüzden gerçek anahtarlarla uçtan uca doğrulama yapılmadı.
- Windows PowerShell betikleri bu makinede çalıştırılmadı.

## Açık kalanlar

- Bu konuşmada görünen Gemini, OpenRouter ve OpenAI anahtarları rotate edilmeli.
- `~/.zprofile` içindeki düz metin anahtar satırları silinmeli, sonra `install-shell` çalıştırılmalı.
- Windows'ta `python -m middle_agentic token ...` komutlarının denenmesi gerekiyor.
- GUI uygulamaları (VS Code Dock'tan açıldığında) profil dosyalarını okumaz. Bu durumda `apiKeyHelper` veya terminalden başlatma gerekir.
- Gateway hâlâ anahtarları `.env` dosyasından okuyor. Keychain entegrasyonu ayrı bir iş.
