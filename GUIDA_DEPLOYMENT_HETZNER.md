# Guida Completa: Deployment su Hetzner VPS

Questa guida ti accompagna passo-passo nel deployment del tuo trading bot su un VPS Hetzner, in modo che possa operare 24/7 anche quando il tuo PC è spento.

## Panoramica

Al termine di questa guida avrai:
- Un VPS Hetzner attivo 24/7 con il tuo trading bot
- Costo: ~€5-7/mese (€3.79 VPS + ~€1-2 API DeepSeek)
- Sincronizzazione automatica con Hyperliquid ogni 60 secondi
- Decisioni AI di trading ogni 3 minuti
- Dashboard accessibile da browser

## Prerequisiti

Prima di iniziare, assicurati di avere:
- [ ] Un account Hyperliquid con fondi
- [ ] La tua private key e wallet address di Hyperliquid
- [ ] Un account DeepSeek con API key (https://platform.deepseek.com)
- [ ] Git installato sul tuo PC
- [ ] Un terminale (Terminal su Mac, PowerShell su Windows)

## Parte 1: Creazione VPS su Hetzner

### Step 1.1: Registrazione su Hetzner

1. Vai su https://www.hetzner.com
2. Clicca su "Sign Up" (in alto a destra)
3. Compila il form di registrazione
4. Conferma la tua email
5. Effettua il login

### Step 1.2: Creazione del VPS

1. Nel pannello Hetzner, clicca su **"Cloud"** nel menu a sinistra
2. Clicca su **"Add Server"**
3. Configura il server:

   **Location**: Falkenstein (Germania) - più vicino all'Italia

   **Image**: Ubuntu 22.04

   **Type**: **CPX11** (consigliato)
   - 2 vCPU
   - 2 GB RAM
   - 40 GB SSD
   - **Costo: €3.79/mese**

   **Networking**: Lascia predefinito (IPv4 + IPv6)

   **SSH Key**:
   - Se NON hai già una SSH key:
     - Sul tuo PC, apri il terminale
     - Esegui: `ssh-keygen -t rsa -b 4096`
     - Premi Enter 3 volte (accetta valori predefiniti)
     - Esegui: `cat ~/.ssh/id_rsa.pub` (Mac/Linux) o `type %USERPROFILE%\.ssh\id_rsa.pub` (Windows)
     - Copia l'output (inizia con "ssh-rsa ...")
   - Clicca su "Add SSH Key" nel pannello Hetzner
   - Incolla la chiave pubblica
   - Dai un nome (es. "my-laptop")
   - Clicca "Add SSH Key"

   **Volumes**: Nessuno (non necessario)

   **Firewalls**: Nessuno per ora (lo configureremo dopo)

   **Backups**: Opzionale (+20% del costo, consigliato per produzione)

   **Placement Groups**: Nessuno

   **Labels**: Opzionale (es. "trading-bot")

   **Cloud config**: Lascia vuoto

   **Name**: Dai un nome (es. "trading-bot-1")

4. Clicca su **"Create & Buy Now"**
5. Attendi 1-2 minuti per la creazione del server
6. **Copia l'indirizzo IP del server** (lo troverai nella dashboard, es. `95.217.123.45`)

### Step 1.3: Test Connessione SSH

1. Apri il terminale sul tuo PC
2. Esegui (sostituisci con il TUO IP):
   ```bash
   ssh root@95.217.123.45
   ```
3. Se richiesto, digita "yes" per accettare il fingerprint
4. Dovresti vedere il prompt del server: `root@trading-bot-1:~#`
5. Esci digitando: `exit`

Se la connessione funziona, sei pronto per il deployment!

## Parte 2: Configurazione Locale

### Step 2.1: Prepara il File .env.production

Sul tuo PC, nella cartella del progetto:

1. Copia il template:
   ```bash
   cd ~/Downloads/Progetti\ Python/trader_bitcoin
   cp .env.production.example .env.production
   ```

2. Apri `.env.production` con un editor:
   ```bash
   nano .env.production
   ```

3. Compila con i TUOI valori:
   ```bash
   # Hyperliquid (OBBLIGATORIO)
   HYPERLIQUID_PRIVATE_KEY=0xTUA_PRIVATE_KEY_QUI
   HYPERLIQUID_WALLET_ADDRESS=0xTUO_WALLET_ADDRESS_QUI
   MAX_CAPITAL_USD=53.0

   # DeepSeek AI (OBBLIGATORIO)
   DEEPSEEK_API_KEY=sk-TUA_DEEPSEEK_API_KEY_QUI
   DEEPSEEK_BASE_URL=https://api.deepseek.com/v1

   # Database (SQLite)
   DATABASE_URL=sqlite+aiosqlite:///./data/data.db

   # Applicazione
   DEBUG=false
   SQL_DEBUG=false

   # Sync (ogni quanto sincronizzare)
   SYNC_INTERVAL_SECONDS=60
   AI_DECISION_INTERVAL=180

   # Connection Pool
   DB_POOL_SIZE=5
   DB_MAX_OVERFLOW=2

   # CORS
   CORS_ORIGINS=*
   ```

4. Salva il file:
   - Su nano: Ctrl+O, poi Enter, poi Ctrl+X
   - Su VSCode/altro editor: Salva normalmente

5. **IMPORTANTE**: Verifica che il file `.env.production` NON sia versionato in git:
   ```bash
   # Il file .gitignore DEVE contenere:
   .env.production
   ```

### Step 2.2: Rendi Eseguibile lo Script di Deployment

```bash
chmod +x deploy_to_hetzner.sh
```

## Parte 3: Deployment Automatico

### Step 3.1: Esegui lo Script di Deployment

Ora eseguirai lo script che automatizza TUTTO il processo:
- Installa Docker sul VPS
- Copia i file del progetto
- Copia le variabili d'ambiente
- Builda l'immagine Docker
- Avvia i container
- Esegue le migrazioni del database

Esegui (sostituisci con il TUO IP):

```bash
./deploy_to_hetzner.sh 95.217.123.45
```

Lo script impiegherà circa **10-15 minuti** e mostrerà il progresso passo-passo.

Output atteso:
```
========================================
Bitcoin Trading Bot - Deployment to Hetzner VPS
========================================
Target VPS: 95.217.123.45
User: root
App Directory: /opt/trader_bitcoin

========================================
Step 1: Testing SSH Connection
========================================
✓ SSH connection to 95.217.123.45 successful

========================================
Step 2: Installing Docker and Dependencies
========================================
✓ Docker and dependencies installed on VPS

========================================
Step 3: Creating Application Directory
========================================
✓ App directory created: /opt/trader_bitcoin

========================================
Step 4: Copying Project Files
========================================
This may take a few minutes...
✓ Project files copied to VPS

========================================
Step 5: Setting Up Environment Variables
========================================
✓ .env.production copied to VPS as .env

========================================
Step 6: Building and Starting Docker Containers
========================================
Building Docker image (this will take 5-10 minutes)...
✓ Docker containers built and started

========================================
Step 7: Running Database Migrations
========================================
✓ Database migrations complete

========================================
Step 8: Verifying Deployment
========================================
✓ Application is running and responding to health checks

========================================
Deployment Complete! 🚀
========================================

Your Bitcoin Trading Bot is now running 24/7 on Hetzner VPS!

Access your bot at:
  http://95.217.123.45:5611
```

### Step 3.2: Configura il Firewall

Per sicurezza, configura il firewall per permettere solo le porte necessarie:

```bash
ssh root@95.217.123.45

# Configura firewall
ufw allow 22/tcp    # SSH (necessario per accedere al server!)
ufw allow 5611/tcp  # Trading bot dashboard
ufw enable          # Attiva il firewall

# Esci dal server
exit
```

### Step 3.3: Verifica il Deployment

1. **Verifica che il bot sia attivo**:
   ```bash
   ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml ps'
   ```

   Output atteso:
   ```
   NAME         IMAGE                   STATUS          PORTS
   trader_app   trader_bitcoin:latest   Up 2 minutes    0.0.0.0:5611->5611/tcp
   ```

2. **Apri il browser** e vai a:
   ```
   http://95.217.123.45:5611
   ```
   (sostituisci con il TUO IP)

3. **Visualizza i log in tempo reale**:
   ```bash
   ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs -f'
   ```

   Dovresti vedere:
   - Startup del backend
   - Sync con Hyperliquid ogni 60 secondi
   - AI decisions ogni 3 minuti

   Per uscire: Ctrl+C

## Parte 4: Monitoraggio e Manutenzione

### Comandi Utili

Tutti questi comandi si eseguono dal tuo PC (non devi collegarti al VPS):

**Visualizza i log**:
```bash
ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs -f'
```

**Visualizza stato container**:
```bash
ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml ps'
```

**Riavvia il bot**:
```bash
ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml restart'
```

**Ferma il bot**:
```bash
ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml stop'
```

**Avvia il bot**:
```bash
ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml start'
```

**Visualizza uso risorse**:
```bash
ssh root@95.217.123.45 'docker stats --no-stream'
```

### Aggiornare il Bot

Quando fai modifiche al codice in locale:

1. Commit le modifiche:
   ```bash
   git add .
   git commit -m "Descrizione modifiche"
   ```

2. Re-deploy (esegue SOLO il build e restart, più veloce):
   ```bash
   ./deploy_to_hetzner.sh 95.217.123.45
   ```

### Backup del Database

Il database SQLite è salvato nel volume Docker. Per fare backup:

```bash
# Backup del database
ssh root@95.217.123.45 'docker compose -f /opt/trader_bitcoin/docker-compose.simple.yml exec -T app sh -c "cd data && tar czf - data.db"' > backup_$(date +%Y%m%d).tar.gz

# Restore da backup (se necessario)
cat backup_20241104.tar.gz | ssh root@95.217.123.45 'docker compose -f /opt/trader_bitcoin/docker-compose.simple.yml exec -T app sh -c "cd data && tar xzf -"'
```

## Parte 5: Troubleshooting

### Bot non risponde

1. Verifica che il container sia attivo:
   ```bash
   ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml ps'
   ```

2. Guarda i log per errori:
   ```bash
   ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs --tail=100'
   ```

3. Verifica variabili d'ambiente:
   ```bash
   ssh root@95.217.123.45 'cat /opt/trader_bitcoin/.env'
   ```

### Errore "API Key invalid"

Verifica che le API key in `.env.production` siano corrette:
- HYPERLIQUID_PRIVATE_KEY inizia con `0x`
- DEEPSEEK_API_KEY inizia con `sk-`

Ri-deploy dopo aver corretto:
```bash
./deploy_to_hetzner.sh 95.217.123.45
```

### Errore "Connection refused"

1. Verifica che il firewall permetta la porta 5611:
   ```bash
   ssh root@95.217.123.45 'ufw status'
   ```

2. Se non è aperta:
   ```bash
   ssh root@95.217.123.45 'ufw allow 5611/tcp && ufw reload'
   ```

### Bot consuma troppa memoria

Il limite è impostato a 512 MB nel docker-compose.simple.yml. Se serve più memoria:

1. Modifica `docker-compose.simple.yml`:
   ```yaml
   mem_limit: 1g  # Aumenta a 1GB
   memswap_limit: 1g
   ```

2. Re-deploy:
   ```bash
   ./deploy_to_hetzner.sh 95.217.123.45
   ```

### AI API costs troppo alti

Verifica l'uso dell'API:

```bash
ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml exec -T app sh -c "cd backend && python3 scripts/maintenance/analyze_ai_costs.py"'
```

Per ridurre i costi, aumenta `AI_DECISION_INTERVAL` in `.env.production`:
```bash
AI_DECISION_INTERVAL=300  # 5 minuti invece di 3
```

## Parte 6: Sicurezza

### Raccomandazioni di Sicurezza

1. **Cambia la porta SSH** (opzionale ma consigliato):
   ```bash
   ssh root@95.217.123.45
   nano /etc/ssh/sshd_config
   # Cambia Port 22 in Port 2222
   systemctl restart sshd
   exit

   # Aggiorna firewall
   ssh root@95.217.123.45 -p 2222 'ufw allow 2222/tcp && ufw delete allow 22/tcp'
   ```

2. **Limita accesso SSH per IP** (se hai IP statico):
   ```bash
   ssh root@95.217.123.45 'ufw allow from TUO_IP_CASA to any port 22'
   ```

3. **Monitora i log di accesso**:
   ```bash
   ssh root@95.217.123.45 'tail -f /var/log/auth.log'
   ```

4. **Backup regolari**: Automatizza i backup settimanali (vedi sezione Backup)

5. **Aggiorna il sistema** (ogni mese):
   ```bash
   ssh root@95.217.123.45 'apt update && apt upgrade -y && reboot'
   ```

## Parte 7: Domande Frequenti (FAQ)

### Q: Il bot continua a tradare anche se chiudo il browser?
**A**: Sì! Il bot gira sul VPS, non sul tuo PC. Puoi spegnere il computer e il bot continuerà 24/7.

### Q: Come faccio a fermare il bot in caso di emergenza?
**A**: Esegui:
```bash
ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml stop'
```

### Q: Posso accedere alla dashboard da smartphone?
**A**: Sì! Apri il browser sullo smartphone e vai a `http://TUO_IP_VPS:5611`

### Q: I dati sono persistenti dopo un riavvio?
**A**: Sì, il database è salvato in un volume Docker che persiste anche dopo restart.

### Q: Quanto traffico consuma al mese?
**A**: Molto poco, circa 1-2 GB/mese (sync API + decisioni AI).

### Q: Posso usare un dominio invece dell'IP?
**A**: Sì! Puoi configurare un dominio (es. `trading.tuodominio.com`) puntando al tuo IP VPS. Poi configura un reverse proxy con SSL (non coperto in questa guida, ma disponibile se necessario).

### Q: Cosa succede se il VPS si riavvia?
**A**: Docker è configurato con `restart: unless-stopped`, quindi il bot si riavvia automaticamente.

### Q: Posso avere più account di trading?
**A**: Sì, ma richiede modifiche al codice. Attualmente è configurato per un singolo account.

## Supporto

Se incontri problemi:

1. Controlla i log: `ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml logs --tail=200'`
2. Verifica lo stato: `ssh root@95.217.123.45 'cd /opt/trader_bitcoin && docker compose -f docker-compose.simple.yml ps'`
3. Controlla le variabili d'ambiente: `ssh root@95.217.123.45 'cat /opt/trader_bitcoin/.env'`

## Riepilogo Costi Mensili

- **VPS Hetzner CPX11**: €3.79/mese
- **DeepSeek API** (stimato): €1-2/mese
  - 480 decisioni/giorno × 30 giorni = 14,400 decisioni/mese
  - Con cache 10 min: ~4,320 chiamate API effettive/mese
  - A $0.0004/chiamata ≈ €1.73/mese

**Totale: ~€5-7/mese** per un bot di trading attivo 24/7!

---

**Congratulazioni!** Il tuo trading bot è ora operativo 24/7 su Hetzner! 🚀🎉
