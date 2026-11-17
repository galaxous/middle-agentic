# Integrations

Bu klasördeki JSON dosyaları **örnektir**; `/path/to/middle-agentic` yolları sizin makinenize göre değişir. Elle kopyalamak yerine yükleyiciyi kullanın. Yükleyici mutlak yolları kendisi yazar, mevcut ayarlarınızı korur ve her dosyanın `.bak` yedeğini alır:

```bash
magent integrate cursor                     # ~/.cursor/hooks.json + ~/.cursor/mcp.json
magent integrate claude                     # ~/.claude/settings.json + `claude mcp add --scope user`
magent integrate cursor --scope project     # <proje>/.cursor/hooks.json + mcp.json
magent integrate claude --scope project     # <proje>/.claude/settings.json + .mcp.json
magent integrate cursor --url http://acer-worker:8000   # hook'ları başka bir node'a yönlendir
```

| Dosya | Görev |
| --- | --- |
| `hooks/magent_hook.py` | stdin'den gelen hook payload'unu `POST /v1/hooks/<tool>/<event>` olarak gateway'e iletir. Yalnızca standart kütüphane kullanır ve fail-open çalışır: gateway kapalıysa agent'ı bekletmez. |
| `cursor/hooks.json`, `cursor/mcp.json` | Cursor için örnek yapılandırma |
| `claude/settings.json`, `claude/mcp.json` | Claude Code için örnek yapılandırma |

Gizli anahtarlar bu dosyalara yazılmaz. Hook scripti `MAGENT_URL` ve `MAGENT_API_KEY` (yoksa `GATEWAY_API_KEY`) değerlerini ortam değişkenlerinden ya da reponun `.env` dosyasından okur.
