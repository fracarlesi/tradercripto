# Claude Code Resources

---
## ⚠️ AZIONE OBBLIGATORIA - INIZIO SESSIONE ⚠️

**CLAUDE: ESEGUI QUESTI COMANDI PRIMA DI QUALSIASI ALTRA AZIONE!**

### 1. Aggiorna conoscenze Claude Code
```
WebFetch: https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md
```

### 2. Verifica MCP Servers installati
Controlla nel system prompt la sezione "MCP Server Instructions" per vedere quali server sono attivi.
Oppure leggi il file di configurazione:
```
Read: .mcp.json
```

### 3. Verifica Skills disponibili
Guarda la sezione `<available_skills>` nel system prompt, oppure usa:
```
Skill tool → vedi parametro "skill" per lista disponibili
```

### 4. Verifica Task Agents disponibili
Guarda la sezione "Available agent types" nella descrizione del Task tool nel system prompt.

### 5. Leggi memorie Serena (se disponibile)
```
mcp__plugin_serena_serena__list_memories
```

**NON SALTARE QUESTI PASSAGGI.** Usa gli strumenti che hai effettivamente a disposizione.

---

## Fonti Ufficiali per Aggiornamenti

| Fonte | URL | Quando Usare |
|-------|-----|--------------|
| **GitHub Changelog** | `github.com/anthropics/claude-code/blob/main/CHANGELOG.md` | SEMPRE a inizio sessione |
| Platform Docs | `platform.claude.com/docs/en/release-notes/overview` | Per API, modelli, SDK |
| Help Center | `support.claude.com/en/articles/12138966-release-notes` | Per features utente |
| Anthropic News | `anthropic.com/news` | Per major releases |

---

## Come Scoprire Risorse Disponibili

### MCP Servers
```python
# Opzione 1: Leggi config
Read(".mcp.json")

# Opzione 2: Controlla system prompt sezione "MCP Server Instructions"

# Opzione 3: Prova a listare risorse
ListMcpResourcesTool()
```

### Skills
```python
# Le skills disponibili sono nel system prompt sotto <available_skills>
# Invocare con /skill-name o tool Skill(skill="nome")
```

### Task Agents
```python
# Gli agent types sono nella descrizione del Task tool
# Usare: Task(subagent_type="tipo", prompt="...")
```

### Serena Memories
```python
mcp__plugin_serena_serena__list_memories()
mcp__plugin_serena_serena__read_memory(memory_file_name="...")
```

---

## Versione di Riferimento

**v2.0.76** (Dec 2025) - Ultima versione nota al momento della scrittura.

⚠️ **NON FIDARTI** - Esegui sempre il WebFetch del changelog. Le release escono frequentemente.

---

## Note Progetto

Questo è il progetto **HLQuantBot** - trading bot per Hyperliquid DEX.

Per info specifiche del progetto, consulta:
- Serena memories (`.serena/memories/`)
- Codebase esistente
- Convenzioni già in uso nel codice
