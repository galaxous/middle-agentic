# 2026-11-21 · Gateway'de HTTP MCP endpoint'i (`/mcp`) · commit bekliyor

## Durum

`commit bekliyor`. Çalışma ağacında; `main` üzerinde commit yok. Gateway Mac'te henüz deploy edilmedi.

## Değişenler

- `middle_agentic/server/mcp_http.py`: gateway'in kendi MCP endpoint'i (streamable HTTP, `/mcp`). Araçlar `ask`, `memory_search`, `memory_record`, `list_nodes`; gateway'in bellek ve model router'ını doğrudan kullanır, HTTP'ye gitmez.
- `McpGuard`: `/mcp` isteklerini `/mcp/` yoluna çevirir (307 yok) ve `GATEWAY_API_KEY` tanımlıysa bearer kontrolü yapar. Anahtar yoksa açık kalır; gateway bunu zaten uyarır.
- DNS rebinding kontrolü, API key tanımlıyken kapatılır. MCP SDK yalnızca localhost Host başlıklarını kabul ettiği için Tailscale adreslerinden erişim engelleniyordu. Anahtar yoksa koruma açık kalır.
- `middle_agentic/server/app.py`: MCP endpoint mount edildi, MCP session manager gateway lifespan'ında başlatılıyor.
- `magent mcp` (stdio) değişmedi; yerel kullanım için aynen duruyor.

## Doğrulama

- Testler: 118 geçti. `tests/test_mcp_http.py` auth (401), yönlendirme yokluğu, araçların uçtan uca çalışması ve `ask` yolunu kapsıyor.
- Geçici gateway (127.0.0.1:8001, geçici bellek DB'si, geçici anahtar) üzerinde gerçek MCP istemcisiyle kayıt ve arama çalıştı.
- Claude Code: test girdisi (`${MAGENT_MCP_TEST_KEY}` header'ı) `claude mcp list` ile `✔ Connected`. Test girdisi kaldırıldı.
- Cursor: test girdisi yüklendi, bağlantı denendi, 401 alındı. Sebep, Cursor'un `launchctl setenv` öncesi açılmış olması. Cursor'un tamamen kapatılıp açılması gerekiyor.
- Codex: CLI kurulu değil; `url` + `bearer_token_env_var` yalnızca dokümandan doğrulandı.
- Continue: remote header ayarı doğrulanamadı.
- Antigravity: `serverUrl` ve `headers` destekli, ama ortam değişkeni genişletmesi dokümanda yok; anahtar düz metin yazılmak zorunda kalır.

## Açık kalanlar

- Gateway Mac (`oguzhan-macbook-pro`) yeni koda geçmeli: `git pull`, `pip install -e .`, `launchctl kickstart`. Bu olmadan `/mcp` 404 döner.
- Deploy sonrası Cursor, Claude Code ve Codex config'leri `http://100.68.92.41:8000/mcp/` adresine çevrilecek. Stdio girdileri kaldırılmadan önce HTTP girdisi doğrulanacak.
- Cursor ve Claude Code'un GUI ortamı anahtarı nereden alacağı kararı gerekiyor: `launchctl setenv` oturuma özel, kalıcı hale getirmek için LaunchAgent gerekir. Alternatif olarak header'a düz anahtar yazılır.
- Continue ve Antigravity stdio'da kalıyor.
- `mcp` paketi 2.x ile test edildi; `pyproject.toml` ve `requirements.txt` hâlâ `mcp>=1.2.0`. 1.x'te `streamable_http_app` yalnızca 1.8 ve sonrasında var, sürüm alt sınırı yükseltilmeli.
