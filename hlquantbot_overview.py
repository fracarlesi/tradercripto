"""Generate HLQuantBot overview PDF for Dario."""

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.pdfgen.canvas import Canvas
from pathlib import Path
import datetime

# Colors
DARK_BG = HexColor("#1a1a2e")
ACCENT = HexColor("#0f3460")
HIGHLIGHT = HexColor("#e94560")
SOFT_BLUE = HexColor("#16213e")
LIGHT_TEXT = HexColor("#f5f5f5")
MID_GRAY = HexColor("#666666")
LIGHT_GRAY = HexColor("#e8e8e8")
TABLE_HEADER_BG = HexColor("#0f3460")
TABLE_ROW_ALT = HexColor("#f0f4f8")
GREEN = HexColor("#27ae60")
RED = HexColor("#e74c3c")

OUTPUT = Path(__file__).parent / "HLQuantBot_Overview_Dario.pdf"


# ─── Styles ───────────────────────────────────────────────────────────
def make_styles():
    s = {}
    s["title"] = ParagraphStyle(
        "title", fontName="Helvetica-Bold", fontSize=28,
        textColor=ACCENT, spaceAfter=4*mm, alignment=TA_LEFT,
    )
    s["subtitle"] = ParagraphStyle(
        "subtitle", fontName="Helvetica", fontSize=13,
        textColor=MID_GRAY, spaceAfter=10*mm,
    )
    s["h1"] = ParagraphStyle(
        "h1", fontName="Helvetica-Bold", fontSize=18,
        textColor=ACCENT, spaceBefore=8*mm, spaceAfter=4*mm,
    )
    s["h2"] = ParagraphStyle(
        "h2", fontName="Helvetica-Bold", fontSize=14,
        textColor=SOFT_BLUE, spaceBefore=6*mm, spaceAfter=3*mm,
    )
    s["h3"] = ParagraphStyle(
        "h3", fontName="Helvetica-Bold", fontSize=11,
        textColor=HIGHLIGHT, spaceBefore=4*mm, spaceAfter=2*mm,
    )
    s["body"] = ParagraphStyle(
        "body", fontName="Helvetica", fontSize=10,
        textColor=black, leading=14, spaceAfter=3*mm,
        alignment=TA_JUSTIFY,
    )
    s["bullet"] = ParagraphStyle(
        "bullet", fontName="Helvetica", fontSize=10,
        textColor=black, leading=14, spaceAfter=1.5*mm,
        leftIndent=12*mm, bulletIndent=6*mm,
    )
    s["code"] = ParagraphStyle(
        "code", fontName="Courier", fontSize=8.5,
        textColor=HexColor("#333333"), leading=12,
        spaceAfter=3*mm, leftIndent=6*mm,
        backColor=HexColor("#f4f4f4"),
    )
    s["caption"] = ParagraphStyle(
        "caption", fontName="Helvetica-Oblique", fontSize=8,
        textColor=MID_GRAY, spaceAfter=4*mm, alignment=TA_CENTER,
    )
    s["footer"] = ParagraphStyle(
        "footer", fontName="Helvetica", fontSize=7,
        textColor=MID_GRAY, alignment=TA_CENTER,
    )
    return s


# ─── Helpers ──────────────────────────────────────────────────────────
def header_footer(canvas: Canvas, doc):
    """Draw header line and footer on every page."""
    w, h = A4
    # Header accent line
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(2)
    canvas.line(20*mm, h - 18*mm, w - 20*mm, h - 18*mm)
    # Footer
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MID_GRAY)
    canvas.drawCentredString(w / 2, 12*mm, f"HLQuantBot v5 — Documento riservato — {datetime.date.today():%d/%m/%Y}")
    canvas.drawRightString(w - 20*mm, 12*mm, f"Pag. {doc.page}")


def make_table(headers, rows, col_widths=None):
    """Create a styled table."""
    data = [headers] + rows
    t = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, TABLE_ROW_ALT]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    t.setStyle(TableStyle(style))
    return t


def b(text):
    return f"<b>{text}</b>"


def hi(text):
    return f'<font color="{HIGHLIGHT.hexval()}">{text}</font>'


def bl(text):
    return f'<font color="{ACCENT.hexval()}">{text}</font>'


# ─── Content ──────────────────────────────────────────────────────────
def build_pdf():
    s = make_styles()
    doc = SimpleDocTemplate(
        str(OUTPUT), pagesize=A4,
        topMargin=22*mm, bottomMargin=20*mm,
        leftMargin=20*mm, rightMargin=20*mm,
    )
    story = []

    # ── Cover ─────────────────────────────────────────────────────
    story.append(Spacer(1, 30*mm))
    story.append(Paragraph("HLQuantBot v5", s["title"]))
    story.append(Paragraph("FLAG-Trader: LLM-Driven Crypto Trading System", s["subtitle"]))
    story.append(Spacer(1, 8*mm))
    story.append(Paragraph(
        "Questo documento descrive l'architettura completa del bot di trading crypto "
        "HLQuantBot, che opera su Hyperliquid DEX utilizzando un modello LLM (Qwen 0.5B) "
        "fine-tuned con Reinforcement Learning per prendere decisioni di entrata e uscita.",
        s["body"],
    ))
    story.append(Spacer(1, 6*mm))

    cover_data = [
        ["Exchange", "Hyperliquid DEX (perpetual futures)"],
        ["Modello", "Qwen 2.5 0.5B Instruct + PPO heads"],
        ["Timeframe", "15 minuti"],
        ["Trigger", "Bollinger-Keltner Squeeze Expansion"],
        ["Exit", "LLM evaluation ogni 60 secondi"],
        ["Leverage", "3x"],
        ["Universo", "50-65 crypto liquide (dinamico)"],
        ["Infrastruttura", "Docker + Hetzner VPS"],
    ]
    story.append(make_table(["Parametro", "Valore"], cover_data, col_widths=[55*mm, 105*mm]))
    story.append(PageBreak())

    # ── 1. Overview architettura ──────────────────────────────────
    story.append(Paragraph("1. Architettura Generale", s["h1"]))
    story.append(Paragraph(
        "Il sistema segue un flusso lineare: il mercato viene monitorato in tempo reale "
        "alla ricerca di pattern di compressione della volatilita (squeeze). Quando il squeeze "
        "esplode, un modello LLM analizza le candele e decide se comprare, vendere o ignorare. "
        "Le posizioni aperte vengono rivalutate dal modello ogni 60 secondi.",
        s["body"],
    ))

    flow_data = [
        ["1", "Monitor", "Polling ogni 10s, detecta squeeze BB-KC su tutto l'universo"],
        ["2", "Trigger", "Squeeze fire su 1+ asset dopo 3+ barre di compressione"],
        ["3", "LLM Decision", "Qwen 0.5B analizza 20 candele 15m, decide BUY/SELL/HOLD"],
        ["4", "Risk Check", "Position sizing, exposure limits, daily trade cap"],
        ["5", "Execution", "Ordine maker (limit post-only, reprice ogni 5s)"],
        ["6", "Position Eval", "Ogni 60s: LLM rivaluta, chiude se segnale inverso"],
    ]
    story.append(make_table(
        ["Step", "Componente", "Descrizione"], flow_data,
        col_widths=[12*mm, 32*mm, 116*mm],
    ))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph(
        f"Il principio fondamentale: {hi('il LLM e l\'unico decisore')}. Non ci sono stop loss meccanici, "
        "take profit fissi o trailing stop. Il modello decide sia quando entrare che quando uscire, "
        "basandosi sulle condizioni di mercato aggiornate.",
        s["body"],
    ))

    # ── 2. Squeeze Trigger ────────────────────────────────────────
    story.append(Paragraph("2. Entry: Squeeze Trigger", s["h1"]))
    story.append(Paragraph(
        "Il trigger di ingresso e basato sulla Range Expansion, specificamente sul "
        "pattern di Bollinger-Keltner Squeeze. L'idea: periodi di bassa volatilita "
        "(compressione) precedono SEMPRE movimenti direzionali significativi.",
        s["body"],
    ))

    story.append(Paragraph("Come funziona", s["h2"]))
    story.append(Paragraph(
        f"{b('Bollinger Bands')}: SMA(20) +/- 2 deviazioni standard. Misurano la volatilita statistica.",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Keltner Channels')}: EMA(20) +/- 1.5 x ATR(14). Misurano la volatilita basata sul range.",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Squeeze')}: quando le Bollinger Bands stanno DENTRO i Keltner Channels. "
        "Significa che la volatilita e cosi bassa che le bande statistiche sono piu strette del range medio.",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Fire')}: quando le BB escono fuori dai KC dopo almeno 3 barre consecutive in squeeze. "
        "La volatilita esplode, segnalando l'inizio di un potenziale move direzionale.",
        s["bullet"],
    ))

    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "Il monitor controlla tutti i ~60 asset dell'universo ogni 10 secondi (con cache candele "
        "da 5 minuti per non sovraccaricare l'API). Quando un asset fire, il suo simbolo viene "
        "passato al modello LLM per la decisione direzionale.",
        s["body"],
    ))

    sq_params = [
        ["bb_period", "20", "Periodo SMA per Bollinger Bands"],
        ["bb_std_mult", "2.0", "Moltiplicatore deviazione standard"],
        ["kc_ema_period", "20", "Periodo EMA per Keltner Channels"],
        ["kc_atr_period", "14", "Periodo ATR per Keltner width"],
        ["kc_atr_mult", "1.5", "Moltiplicatore ATR"],
        ["lookback_bars", "3", "Barre consecutive minime in squeeze prima del fire"],
        ["candle_interval", "15m", "Timeframe candele analizzate"],
    ]
    story.append(KeepTogether([
        Paragraph("Parametri configurati", s["h3"]),
        make_table(
            ["Parametro", "Valore", "Descrizione"], sq_params,
            col_widths=[35*mm, 18*mm, 107*mm],
        ),
    ]))

    # ── 3. FLAG-Trader LLM ────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("3. Il Cervello: FLAG-Trader LLM", s["h1"]))
    story.append(Paragraph(
        "FLAG-Trader e un modello linguistico (Qwen 2.5 0.5B Instruct) modificato per il trading. "
        "L'80% dei layer e congelato; sopra vengono aggiunte 4 teste specializzate, "
        "addestrate con PPO (Proximal Policy Optimization) su dati storici.",
        s["body"],
    ))

    story.append(Paragraph("Le 4 teste del modello", s["h2"]))
    heads_data = [
        ["Policy Head", "BUY / SELL / HOLD", "Decisione direzionale principale"],
        ["Value Head", "Confidence score", "Quanto il modello e sicuro (filtra segnali deboli)"],
        ["TP Head", "Take Profit %", "Predice il target ottimale (0.5% - 5.0%)"],
        ["SL Head", "Stop Loss %", "Predice lo stop ottimale (0.3% - 2.0%)"],
    ]
    story.append(make_table(
        ["Testa", "Output", "Funzione"], heads_data,
        col_widths=[30*mm, 35*mm, 95*mm],
    ))

    story.append(Paragraph("Cosa vede il modello", s["h2"]))
    story.append(Paragraph(
        "Ad ogni valutazione, il modello riceve un prompt strutturato contenente:",
        s["body"],
    ))
    story.append(Paragraph(
        f"{b('20 candele 15m')} normalizzate come variazione percentuale dal primo close "
        "(open, high, low, close, volume per ogni candela)",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Stato del portfolio')}: cash disponibile, valore posizione, equity totale, PnL non realizzato",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Storico decisioni')}: le ultime 10 azioni prese e i reward ottenuti",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Trade simili passati')} (RAG): il sistema cerca nel database trade precedenti "
        "con condizioni di mercato simili e li inietta nel prompt come contesto",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Costi di trading')}: fee maker/taker esplicitate (break-even a 0.07%), "
        "cosi il modello non apre trade su micro-movimenti",
        s["bullet"],
    ))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        f"Il modello produce un segnale solo se la {b('confidence supera la soglia')} (0.6). "
        "Segnali deboli vengono scartati automaticamente.",
        s["body"],
    ))

    # ── 4. Exit ───────────────────────────────────────────────────
    story.append(Paragraph("4. Exit: Valutazione Continua", s["h1"]))
    story.append(Paragraph(
        "Non ci sono stop loss meccanici. Ogni 60 secondi, il modello rivaluta ogni posizione aperta "
        "con candele aggiornate. Se il modello inverte il segnale (es. era LONG e ora dice SELL), "
        "la posizione viene chiusa.",
        s["body"],
    ))
    story.append(Paragraph(
        "Quando il modello valuta una posizione aperta, riceve nel prompt anche il "
        f"{b('contesto di entry')}: perche e entrato (squeeze fire), con quale confidence, "
        "e i dettagli del trigger. Questo gli permette di decidere se la tesi originale "
        "e ancora valida.",
        s["body"],
    ))

    story.append(Paragraph("Protezioni anti-churn", s["h2"]))
    story.append(Paragraph(
        f"{b('Eta minima')}: una posizione non puo essere chiusa prima di 30 minuti dall'apertura, "
        "per evitare flip rapidi su rumore di mercato.",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Profitto minimo')}: se il PnL e positivo ma inferiore a 0.15%, non chiude "
        "(le commissioni non sarebbero coperte).",
        s["bullet"],
    ))

    # ── 5. Risk Management ────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("5. Risk Management", s["h1"]))
    story.append(Paragraph(
        "Il sistema applica diversi livelli di protezione del capitale prima di ogni trade.",
        s["body"],
    ))

    risk_data = [
        ["Risk per trade", "25% dell'equity", "Quanto capitale rischiare su ogni operazione"],
        ["Max posizioni", "1", "Una sola posizione aperta (fase di validazione)"],
        ["Max posizione %", "85% equity", "Hard cap sul notional di una singola posizione"],
        ["Leverage", "3x", "Amplificazione del notional"],
        ["Max trade giornalieri", "3", "Limite operazioni per giorno"],
        ["Exposure massima", "100%", "Limite esposizione totale del portfolio"],
    ]
    story.append(make_table(
        ["Regola", "Valore", "Scopo"], risk_data,
        col_widths=[40*mm, 30*mm, 90*mm],
    ))

    story.append(Paragraph("Kill Switch", s["h2"]))
    story.append(Paragraph(
        "Sistema di protezione automatico che monitora il drawdown:",
        s["body"],
    ))
    ks_data = [
        ["Perdita giornaliera > 6%", "Pausa fino a domani"],
        ["Perdita settimanale > 15%", "Pausa 3 giorni"],
        ["Max drawdown > 25%", "Stop totale del bot"],
    ]
    story.append(make_table(
        ["Condizione", "Azione"], ks_data,
        col_widths=[65*mm, 95*mm],
    ))

    # ── 6. Execution ──────────────────────────────────────────────
    story.append(Paragraph("6. Esecuzione Ordini", s["h1"]))
    story.append(Paragraph(
        "Gli ordini vengono eseguiti in modalita maker (limit post-only) per ottenere "
        "il miglior prezzo e pagare commissioni ridotte (0.02% vs 0.05% taker).",
        s["body"],
    ))
    story.append(Paragraph(
        f"{b('Repricing')}: se l'ordine non viene eseguito entro 5 secondi, viene cancellato "
        "e riposizionato al nuovo mid price. Massimo 6 repricing (30 secondi totali).",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Spread filter')}: se lo spread bid-ask supera 0.08%, l'ordine viene sospeso "
        "per evitare slippage eccessivo.",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Slippage max')}: 0.15%. Se il prezzo si muove troppo durante l'esecuzione, "
        "l'ordine viene annullato.",
        s["bullet"],
    ))

    # ─�� 7. Universo ───────────────────────────────────────────────
    story.append(Paragraph("7. Universo di Trading", s["h1"]))
    story.append(Paragraph(
        "L'universo degli asset e dinamico e viene aggiornato ad ogni avvio del bot:",
        s["body"],
    ))
    story.append(Paragraph(
        f"1. Scarica tutti i simboli disponibili su Hyperliquid (~229)",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"2. Esclude stablecoin (USDC, USDT, DAI...) e token delisted/migrati",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"3. Filtra per volume minimo: solo asset con volume 24h > $500.000",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"4. Risultato: tipicamente {b('50-65 asset liquidi')} monitorati in tempo reale",
        s["bullet"],
    ))

    # ── 8. Training ───────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("8. Training del Modello", s["h1"]))
    story.append(Paragraph(
        "Il modello viene addestrato con Reinforcement Learning (PPO) su dati storici di candele.",
        s["body"],
    ))

    story.append(Paragraph("Ambiente di simulazione", s["h2"]))
    story.append(Paragraph(
        "Un ambiente Gymnasium simula il mercato barra per barra. Il modello riceve lo stato "
        "(candele, portfolio, storico) e sceglie un'azione. L'ambiente calcola il reward "
        "basato sulla variazione dello Sharpe Ratio.",
        s["body"],
    ))

    story.append(Paragraph("Reward: Sharpe Delta", s["h2"]))
    story.append(Paragraph(
        "Il reward non e il semplice profitto, ma la {variazione dello Sharpe Ratio}. "
        "Questo incentiva il modello a cercare rendimenti consistenti con bassa volatilita, "
        "non singoli trade fortunati ad alto rischio.".replace("{", "<b>").replace("}", "</b>"),
        s["body"],
    ))

    story.append(Paragraph("PPO (Proximal Policy Optimization)", s["h2"]))
    ppo_data = [
        ["Learning rate", "1e-5", "Velocita di apprendimento"],
        ["Gamma", "0.99", "Discount factor (peso futuro vs presente)"],
        ["GAE Lambda", "0.95", "Smoothing dell'advantage estimation"],
        ["Clip range", "0.2", "Limita aggiornamenti troppo aggressivi"],
        ["Entropy coef", "0.01", "Incentiva esplorazione"],
        ["Layers congelati", "80%", "Solo le teste e i layer superiori vengono aggiornati"],
    ]
    story.append(make_table(
        ["Iperparametro", "Valore", "Funzione"], ppo_data,
        col_widths=[35*mm, 18*mm, 107*mm],
    ))

    # ── 9. Infrastruttura ─────────────────────────────────────────
    story.append(Paragraph("9. Infrastruttura", s["h1"]))

    infra_data = [
        ["Runtime", "Python 3.11, asyncio, Docker"],
        ["Server", "Hetzner VPS (Linux)"],
        ["Deploy", "rsync + Docker Compose rebuild"],
        ["Modello", "PyTorch CPU (Qwen 0.5B ~ 1GB RAM)"],
        ["Notifiche", "WhatsApp via ntfy.sh (trade open/close, errori)"],
        ["Dati", "Hyperliquid SDK (REST + WebSocket)"],
        ["Persistenza", "SQLite locale per trade log e RAG memory"],
        ["Quality", "Pyright (types), Ruff (lint), pytest (370+ test)"],
    ]
    story.append(make_table(
        ["Componente", "Dettaglio"], infra_data,
        col_widths=[35*mm, 125*mm],
    ))

    # ── 10. Esempio operazione ────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("10. Esempio: Ciclo di Vita di un Trade", s["h1"]))

    timeline = [
        ["T=0s", "Squeeze detect", "BTC: BB dentro KC da 5 barre. Alla barra 6, BB esce fuori KC. FIRE."],
        ["T=10s", "Trigger queued", "Monitor accumula il trigger, aspetta cooldown 60s."],
        ["T=60s", "LLM evaluation", "Il modello riceve 20 candele BTC 15m + portfolio + trade simili passati."],
        ["T=62s", "Decisione", "Output: BUY, confidence=0.75, TP=2.5%, SL=1.0%"],
        ["T=64s", "Risk check", "OK: 1 posizione, equity $10k, risk 25% = $2.5k, size calcolata."],
        ["T=66s", "Ordine maker", "Limit post-only a mid price. Reprice ogni 5s se non filled."],
        ["T=96s", "Fill", "Eseguito a $45.050 (slippage +0.11%). Posizione LONG aperta."],
        ["T=156s", "Eval #1", "Timer 60s: modello dice HOLD (confidence 0.65). Posizione mantenuta."],
        ["T=216s", "Eval #2", "Modello dice SELL ma posizione troppo giovane (< 30 min). Skip."],
        ["T=1.896s", "Eval #N", "Modello dice SELL, eta > 30 min, PnL +1.5% > 0.15%. CHIUDE."],
        ["T=1.900s", "Close", "Market sell a $45.200. Profitto: +$150 lordo."],
    ]
    story.append(make_table(
        ["Tempo", "Evento", "Dettaglio"], timeline,
        col_widths=[18*mm, 30*mm, 112*mm],
    ))

    # ── 11. Backtest Results ──────────────────────────────────────
    story.append(Spacer(1, 6*mm))
    story.append(Paragraph("11. Risultati Backtest (BTC, 52 giorni, 15m)", s["h1"]))
    story.append(Paragraph(
        "Risultati del backtest comparativo tra il segnale EMA High (rule-based) e "
        "il FLAG-Trader LLM, entrambi triggerati su squeeze fire:",
        s["body"],
    ))

    bt_data = [
        ["Squeeze Fires", "45", "45"],
        ["Trade eseguiti", "2", "44"],
        ["Win Rate", "0.0%", "65.9%"],
        ["Profit Factor", "0.00", "0.96"],
        ["Expectancy (R)", "-1.00R", "-0.02R"],
        ["Max Drawdown", "-0.5%", "-6.1%"],
        ["Total Return", "-0.5%", "-1.2%"],
        ["Avg Duration", "1 bar", "11.9 bars"],
        ["Direzione", "LONG only", "LONG + SHORT"],
    ]
    story.append(make_table(
        ["Metrica", "EMA High (regole)", "FLAG-Trader (LLM, conf 0.8)"], bt_data,
        col_widths=[35*mm, 50*mm, 75*mm],
    ))
    story.append(Paragraph(
        "Il LLM con confidence threshold 0.8 raggiunge un'expectancy quasi a break-even (-0.02R) "
        "con win rate 65.9%. Il sistema e in fase di ottimizzazione dell'exit management.",
        s["caption"],
    ))

    # ── 12. Roadmap ───────────────────────────────────────────────
    story.append(Paragraph("12. Prossimi Passi", s["h1"]))
    story.append(Paragraph(
        f"{b('Ottimizzazione exit')}: migliorare il profit factor portando le loss medie "
        "piu vicine alle win medie (attualmente il modello tiene troppo a lungo i perdenti).",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Multi-asset')}: estendere la validazione da BTC a un basket piu ampio, "
        "calibrando la confidence threshold per asset class.",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Retraining continuo')}: utilizzare i trade log del bot live come dati di "
        "training per migliorare il modello iterativamente.",
        s["bullet"],
    ))
    story.append(Paragraph(
        f"{b('Position sizing dinamico')}: passare dal 25% fisso a un Kelly fraction "
        "adattivo basato sulla confidence del modello.",
        s["bullet"],
    ))

    # Build
    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    print(f"PDF generato: {OUTPUT}")


if __name__ == "__main__":
    build_pdf()
