# 2026-11-20 · Codex ve Antigravity hook'ları, Continue MCP · main'de

## Durum

`main'de`. Commit: `b23207f`.

## Değişenler

- **Codex**: `magent integrate codex` `~/.codex/hooks.json` içine `SessionStart`, `UserPromptSubmit`, `Stop` hook'larını yazar. MCP, `config.toml` içinde işaretli bir blok olarak yazılır. Hook'lar `/hooks` ile bir kez güvenilmelidir.
- **Antigravity**: `magent integrate antigravity` `hooks.json` içine `SessionStart` ve `PreInvocation` hook'larını yazar. Payload'da olay adı olmadığı için hook betiğine `--event` eklendi. `PreInvocation` yalnızca turun ilk çağrısında kaydeder.
- **Continue**: `magent integrate continue` `.continue/mcpServers/middle-agentic.yaml` yazar (yalnızca MCP).
- **Kurallar**: `magent rules install` beş araç için kural yazar. Codex ve Antigravity ortak `AGENTS.md` bloğunu kullanır.
- **Gateway**: `middle_agentic/hooks/codex.py` ve `antigravity.py` adaptörleri eklendi.

## Doğrulama

- Yerel testler: 89 geçti. Codex ve Antigravity adaptörleri, kurulum birleştirme, idempotensi ve hook betiğinin `--event` yolu test edildi.
- Geçici HOME altında kurulum çıktıları incelendi.
- Gerçek Codex ve Antigravity oturumlarında hook'ların tetiklendiği henüz görülmedi.

## Açık kalanlar

- Gateway Mac yeni koda geçmeli, aksi halde `/v1/hooks/codex` ve `/v1/hooks/antigravity` 404 döner.
- Codex'te `additionalContextLimit` davranışı belgede tam açık değil.
- Antigravity'de `invocationNum == 0`'ın her kullanıcı turunun ilk çağrısı olduğu doğrulanmadı.
