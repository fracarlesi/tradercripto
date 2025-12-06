import json
import time
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Optional, List

import eth_account
from eth_account.signers.local import LocalAccount

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

import db_utils


class HyperLiquidTrader:
    def __init__(
        self,
        secret_key: str,
        account_address: str,
        testnet: bool = True,
        skip_ws: bool = True,
    ):
        self.secret_key = secret_key
        self.account_address = account_address

        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.base_url = base_url

        # crea account signer
        account: LocalAccount = eth_account.Account.from_key(secret_key)

        self.info = Info(base_url, skip_ws=skip_ws)
        self.exchange = Exchange(account, base_url, account_address=account_address)

        # cache meta per tick-size e min-size
        self.meta = self.info.meta()

    def _to_hl_size(self, size_decimal: Decimal) -> str:
        # HL accetta max 8 decimali
        size_clamped = size_decimal.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        return format(size_clamped, "f")   # HL vuole stringa decimale perfetta

    # ----------------------------------------------------------------------
    #                            VALIDAZIONE INPUT
    # ----------------------------------------------------------------------
    def _validate_order_input(self, order_json: Dict[str, Any]):
        required_fields = [
            "operation",
            "symbol",
            "direction",
            "target_portion_of_balance",
            "leverage",
            "reason",
        ]

        for f in required_fields:
            if f not in order_json:
                raise ValueError(f"Missing required field: {f}")

        if order_json["operation"] not in ("open", "close", "hold"):
            raise ValueError("operation must be 'open', 'close', or 'hold'")

        if order_json["direction"] not in ("long", "short"):
            raise ValueError("direction must be 'long' or 'short'")

        try:
            float(order_json["target_portion_of_balance"])
        except:
            raise ValueError("target_portion_of_balance must be a number")

        # Validazione opzionale SL/TP
        if "stop_loss_percent" in order_json:
            sl = float(order_json["stop_loss_percent"])
            if sl < 1 or sl > 15:
                print(f"⚠️ stop_loss_percent ({sl}) fuori range 1-15%, usando default 5%")
                order_json["stop_loss_percent"] = self.DEFAULT_STOP_LOSS_PERCENT

        if "take_profit_percent" in order_json:
            tp = float(order_json["take_profit_percent"])
            if tp < 1 or tp > 50:
                print(f"⚠️ take_profit_percent ({tp}) fuori range 1-50%, ignorato")
                del order_json["take_profit_percent"]

    # ----------------------------------------------------------------------
    #                           MIN SIZE / TICK SIZE
    # ----------------------------------------------------------------------
    def _get_min_tick_for_symbol(self, symbol: str) -> Decimal:
        """
        Hyperliquid definisce per ogni asset un tick size.
        Lo leggiamo da meta().
        """
        for perp in self.meta["universe"]:
            if perp["name"] == symbol:
                return Decimal(str(perp["szDecimals"]))
        return Decimal("0.00000001")  # fallback a 1e-8

    def _round_size(self, size: Decimal, decimals: int) -> float:
        """
        Hyperliquid accetta massimo 8 decimali.
        Inoltre dobbiamo rispettare il tick size.
        """
        # prima clamp a 8 decimali
        size = size.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

        # poi count of decimals per il tick
        fmt = f"{{0:.{decimals}f}}"
        return float(fmt.format(size))

    # ----------------------------------------------------------------------
    #                        GESTIONE LEVA
    # ----------------------------------------------------------------------
    def get_current_leverage(self, symbol: str) -> Dict[str, Any]:
        """Ottieni info sulla leva corrente per un simbolo"""
        try:
            user_state = self.info.user_state(self.account_address)
            
            # Cerca nelle posizioni aperte
            for position in user_state.get('assetPositions', []):
                pos = position.get('position', {})
                coin = pos.get('coin', '')
                if coin == symbol:
                    leverage_info = pos.get('leverage', {})
                    return {
                        'value': leverage_info.get('value', 0),
                        'type': leverage_info.get('type', 'unknown'),
                        'coin': coin
                    }
            
            # Se non c'è posizione aperta, controlla cross leverage default
            cross_leverage = user_state.get('crossLeverage', 20)
            return {
                'value': cross_leverage,
                'type': 'cross',
                'coin': symbol,
                'note': 'No open position, showing account default'
            }
            
        except Exception as e:
            print(f"Errore ottenendo leva corrente: {e}")
            return {'value': 20, 'type': 'unknown', 'error': str(e)}

    def set_leverage_for_symbol(self, symbol: str, leverage: int, is_cross: bool = True) -> Dict[str, Any]:
        """Imposta la leva per un simbolo specifico usando il metodo corretto"""
        try:
            print(f"🔧 Impostando leva {leverage}x per {symbol} ({'cross' if is_cross else 'isolated'} margin)")
            
            # Usa il metodo update_leverage con i parametri corretti
            result = self.exchange.update_leverage(
                leverage=leverage,      # int
                name=symbol,           # str - nome del simbolo come "BTC"
                is_cross=is_cross      # bool
            )
            
            if result.get('status') == 'ok':
                print(f"✅ Leva impostata con successo a {leverage}x per {symbol}")
            else:
                print(f"⚠️ Risposta dall'exchange: {result}")
                
            return result
            
        except Exception as e:
            print(f"❌ Errore impostando leva per {symbol}: {e}")
            return {"status": "error", "error": str(e)}

    # ----------------------------------------------------------------------
    #                        ESECUZIONE SEGNALE AI
    # ----------------------------------------------------------------------

    # Stop Loss di default (5%) - protezione minima obbligatoria
    DEFAULT_STOP_LOSS_PERCENT = 5.0
    # Take Profit default = 2x Stop Loss (risk/reward 1:2)
    TP_MULTIPLIER = 2.0

    def _calculate_sl_tp_prices(
        self,
        mark_price: Decimal,
        direction: str,
        stop_loss_percent: float,
        take_profit_percent: float = None
    ) -> Dict[str, float]:
        """
        Calcola i prezzi di Stop Loss e Take Profit basati sulla percentuale.

        Per LONG:  SL = mark_price * (1 - sl_percent/100)
                   TP = mark_price * (1 + tp_percent/100)
        Per SHORT: SL = mark_price * (1 + sl_percent/100)
                   TP = mark_price * (1 - tp_percent/100)
        """
        result = {}

        if direction == "long":
            # LONG: SL sotto il prezzo, TP sopra
            result["sl"] = float(mark_price * (Decimal("1") - Decimal(str(stop_loss_percent)) / Decimal("100")))
            if take_profit_percent:
                result["tp"] = float(mark_price * (Decimal("1") + Decimal(str(take_profit_percent)) / Decimal("100")))
        else:
            # SHORT: SL sopra il prezzo, TP sotto
            result["sl"] = float(mark_price * (Decimal("1") + Decimal(str(stop_loss_percent)) / Decimal("100")))
            if take_profit_percent:
                result["tp"] = float(mark_price * (Decimal("1") - Decimal(str(take_profit_percent)) / Decimal("100")))

        return result

    def execute_signal(
        self,
        order_json: Dict[str, Any],
        operation_id: Optional[int] = None
    ) -> Dict[str, Any]:
        from decimal import Decimal, ROUND_DOWN

        self._validate_order_input(order_json)

        op = order_json["operation"]
        symbol = order_json["symbol"]
        direction = order_json["direction"]
        portion = Decimal(str(order_json["target_portion_of_balance"]))
        leverage = int(order_json.get("leverage", 1))
        reason = order_json.get("reason", "")

        # Nuovi parametri SL/TP (opzionali, con default per SL)
        stop_loss_percent = float(order_json.get("stop_loss_percent", self.DEFAULT_STOP_LOSS_PERCENT))

        # TP: se LLM non specifica, usa default 2x SL (risk/reward 1:2)
        if "take_profit_percent" in order_json and order_json["take_profit_percent"] is not None:
            take_profit_percent = float(order_json["take_profit_percent"])
        else:
            take_profit_percent = stop_loss_percent * self.TP_MULTIPLIER
            print(f"📊 TP non specificato, usando default {take_profit_percent}% (2x SL)")

        if op == "hold":
            print(f"[HyperLiquidTrader] HOLD — nessuna azione per {symbol}.")
            return {"status": "hold", "message": "No action taken."}

        if op == "close":
            print(f"[HyperLiquidTrader] Market CLOSE per {symbol}")
            # Chiudi il trade nel database
            open_trade = db_utils.get_open_trade(symbol)
            result = self.exchange.market_close(symbol)

            if open_trade and result.get("status") == "ok":
                # Recupera fill data per prezzo e fee di uscita
                time.sleep(1)  # Attendi che il fill sia registrato
                exit_data = self._get_recent_fill(symbol)
                if exit_data:
                    db_utils.close_trade(
                        trade_id=open_trade["id"],
                        exit_price=exit_data["price"],
                        exit_size=exit_data["size"],
                        close_type="manual",
                        exit_operation_id=operation_id,
                        exit_reason=reason,
                        exit_order_id=exit_data.get("oid"),
                        exit_fee=exit_data.get("fee"),
                        exit_fill_data=exit_data.get("raw"),
                    )

            return result

        # OPEN --------------------------------------------------------
        # Prima di aprire la posizione, imposta la leva desiderata
        leverage_result = self.set_leverage_for_symbol(
            symbol=symbol,
            leverage=leverage,
            is_cross=True  # Puoi cambiare in False per isolated margin
        )

        if leverage_result.get('status') != 'ok':
            print(f"⚠️ Attenzione: impostazione leva potrebbe aver avuto problemi: {leverage_result}")

        # Piccola pausa per assicurarsi che la leva sia applicata
        time.sleep(0.5)

        # Verifica la leva attuale dopo l'aggiornamento
        current_leverage_info = self.get_current_leverage(symbol)
        print(f"📊 Leva attuale per {symbol}: {current_leverage_info}")

        # Ora procedi con l'apertura della posizione
        user = self.info.user_state(self.account_address)
        balance_usd = Decimal(str(user["marginSummary"]["accountValue"]))

        if balance_usd <= 0:
            raise RuntimeError("Balance account = 0")

        notional = balance_usd * portion * Decimal(str(leverage))

        mids = self.info.all_mids()
        if symbol not in mids:
            raise RuntimeError(f"Symbol {symbol} non presente su HL")

        mark_px = Decimal(str(mids[symbol]))
        raw_size = notional / mark_px

        # Ottieni info sul simbolo dalla meta
        symbol_info = None
        for perp in self.meta["universe"]:
            if perp["name"] == symbol:
                symbol_info = perp
                break

        if not symbol_info:
            raise RuntimeError(f"Symbol {symbol} non trovato nella meta universe")

        # IMPORTANTE: Ottieni il minimum order size (non szDecimals!)
        min_size = Decimal(str(symbol_info.get("minSz", "0.001")))
        sz_decimals = int(symbol_info.get("szDecimals", 8))
        max_leverage = symbol_info.get("maxLeverage", 100)

        # Verifica che la leva richiesta non superi il massimo
        if leverage > max_leverage:
            print(f"⚠️ Leva richiesta ({leverage}) supera il massimo per {symbol} ({max_leverage})")

        # Arrotonda secondo i decimali permessi
        quantizer = Decimal(10) ** -sz_decimals
        size_decimal = raw_size.quantize(quantizer, rounding=ROUND_DOWN)

        # Verifica che sia sopra il minimo
        if size_decimal < min_size:
            print(f"⚠️ Size calcolata ({size_decimal}) < minima richiesta ({min_size})")
            print(f"   Raw size: {raw_size}, Balance: {balance_usd}, Portion: {portion}, Leverage: {leverage}")
            print(f"   Notional: {notional}, Mark price: {mark_px}")

            # Usa direttamente il minimum size
            size_decimal = min_size

        # Converti a float per l'API
        size_float = float(size_decimal)

        is_buy = (direction == "long")

        # Calcola prezzi SL/TP
        sl_tp_prices = self._calculate_sl_tp_prices(
            mark_price=mark_px,
            direction=direction,
            stop_loss_percent=stop_loss_percent,
            take_profit_percent=take_profit_percent
        )

        print(
            f"\n[HyperLiquidTrader] Market {'BUY' if is_buy else 'SELL'} "
            f"{size_float} {symbol}\n"
            f"  💰 Prezzo: ${mark_px}\n"
            f"  📊 Notional: ${notional:.2f}\n"
            f"  🎯 Leva target: {leverage}x\n"
            f"  🛡️ Stop Loss: ${sl_tp_prices['sl']:.2f} (-{stop_loss_percent}%)\n"
            f"  🎯 Take Profit: ${sl_tp_prices['tp']:.2f} (+{take_profit_percent}%)\n"
        )

        # 1. Apri la posizione con market order
        res = self.exchange.market_open(
            symbol,
            is_buy,
            size_float,
            None,
            0.01
        )

        print(f"📈 Posizione aperta: {res}")

        # 3. Registra il trade nel database
        if res.get("status") == "ok":
            time.sleep(1)  # Attendi che il fill sia registrato
            entry_data = self._get_recent_fill(symbol)

            entry_price = float(mark_px)  # default
            entry_fee = None
            entry_order_id = None

            if entry_data:
                entry_price = entry_data.get("price", float(mark_px))
                entry_fee = entry_data.get("fee")
                entry_order_id = entry_data.get("oid")

            try:
                trade_id = db_utils.open_trade(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    entry_size=size_float,
                    leverage=float(leverage),
                    entry_operation_id=operation_id,
                    entry_reason=reason,
                    entry_order_id=entry_order_id,
                    entry_fee=entry_fee,
                    entry_fill_data=entry_data.get("raw") if entry_data else None,
                )
                print(f"📝 Trade registrato nel DB con id={trade_id}")
            except Exception as e:
                print(f"⚠️ Errore registrando trade nel DB: {e}")

        # 2. Piazza ordini SL e TP come trigger orders separati
        # Per LONG: SL è un SELL, TP è un SELL
        # Per SHORT: SL è un BUY, TP è un BUY
        sl_is_buy = not is_buy  # SL chiude la posizione (opposto della direzione)
        tp_is_buy = not is_buy  # TP chiude la posizione (opposto della direzione)

        # Calcola limit prices più aggressivi per garantire esecuzione
        # Per SL: se stiamo vendendo (closing long), limit sotto trigger; se comprando (closing short), limit sopra trigger
        # Per TP: opposto
        slippage_factor = Decimal("0.02")  # 2% slippage tolerance

        if is_buy:  # Posizione LONG, chiudiamo con SELL
            # SL: vendiamo quando prezzo scende, limit ancora più basso per garantire fill
            sl_limit = float(Decimal(str(sl_tp_prices["sl"])) * (Decimal("1") - slippage_factor))
            # TP: vendiamo quando prezzo sale, limit leggermente più basso del trigger
            tp_limit = float(Decimal(str(sl_tp_prices["tp"])) * (Decimal("1") - slippage_factor))
        else:  # Posizione SHORT, chiudiamo con BUY
            # SL: compriamo quando prezzo sale, limit ancora più alto per garantire fill
            sl_limit = float(Decimal(str(sl_tp_prices["sl"])) * (Decimal("1") + slippage_factor))
            # TP: compriamo quando prezzo scende, limit leggermente più alto del trigger
            tp_limit = float(Decimal(str(sl_tp_prices["tp"])) * (Decimal("1") + slippage_factor))

        print(f"  📋 SL trigger: ${sl_tp_prices['sl']:.2f}, limit: ${sl_limit:.2f}")
        print(f"  📋 TP trigger: ${sl_tp_prices['tp']:.2f}, limit: ${tp_limit:.2f}")

        # Stop Loss order
        try:
            sl_order = self.exchange.order(
                name=symbol,
                is_buy=sl_is_buy,
                sz=size_float,
                limit_px=sl_limit,
                order_type={"trigger": {"triggerPx": sl_tp_prices["sl"], "isMarket": False, "tpsl": "sl"}},
                reduce_only=True
            )
            print(f"🛡️ Stop Loss piazzato: {sl_order}")
        except Exception as e:
            print(f"⚠️ Errore piazzando Stop Loss: {e}")

        # Take Profit order
        try:
            tp_order = self.exchange.order(
                name=symbol,
                is_buy=tp_is_buy,
                sz=size_float,
                limit_px=tp_limit,
                order_type={"trigger": {"triggerPx": sl_tp_prices["tp"], "isMarket": False, "tpsl": "tp"}},
                reduce_only=True
            )
            print(f"🎯 Take Profit piazzato: {tp_order}")
        except Exception as e:
            print(f"⚠️ Errore piazzando Take Profit: {e}")

        return res

    # ----------------------------------------------------------------------
    #                           STATO ACCOUNT
    # ----------------------------------------------------------------------
    def get_account_status(self) -> Dict[str, Any]:
        data = self.info.user_state(self.account_address)
        balance = float(data["marginSummary"]["accountValue"])

        mids = self.info.all_mids()
        positions = []

        # Gestisci il formato corretto dei dati
        asset_positions = data.get("assetPositions", [])
        
        for p in asset_positions:
            # Estrai la posizione dal formato corretto
            if isinstance(p, dict) and "position" in p:
                pos = p["position"]
                coin = pos.get("coin", "")
            else:
                # Se il formato è diverso, prova ad adattarti
                pos = p
                coin = p.get("coin", p.get("symbol", ""))
                
            if not pos or not coin:
                continue
                
            size = float(pos.get("szi", 0))
            if size == 0:
                continue

            entry = float(pos.get("entryPx", 0))
            mark = float(mids.get(coin, entry))

            # Calcola P&L
            pnl = (mark - entry) * size
            
            # Estrai info sulla leva
            leverage_info = pos.get("leverage", {})
            leverage_value = leverage_info.get("value", "N/A")
            leverage_type = leverage_info.get("type", "unknown")

            positions.append({
                "symbol": coin,
                "side": "long" if size > 0 else "short",
                "size": abs(size),
                "entry_price": entry,
                "mark_price": mark,
                "pnl_usd": round(pnl, 4),
                "leverage": f"{leverage_value}x ({leverage_type})"
            })

        return {
            "balance_usd": balance,
            "open_positions": positions,
        }
    
    # ----------------------------------------------------------------------
    #                           UTILITY DEBUG
    # ----------------------------------------------------------------------
    def debug_symbol_limits(self, symbol: str = None):
        """Mostra i limiti di trading per un simbolo o tutti"""
        print("\n📊 LIMITI TRADING HYPERLIQUID")
        print("-" * 60)
        
        for perp in self.meta["universe"]:
            if symbol and perp["name"] != symbol:
                continue
                
            print(f"\nSymbol: {perp['name']}")
            print(f"  Min Size: {perp.get('minSz', 'N/A')}")
            print(f"  Size Decimals: {perp.get('szDecimals', 'N/A')}")
            print(f"  Price Decimals: {perp.get('pxDecimals', 'N/A')}")
            print(f"  Max Leverage: {perp.get('maxLeverage', 'N/A')}")
            print(f"  Only Isolated: {perp.get('onlyIsolated', False)}")

    # ----------------------------------------------------------------------
    #                           TRADE TRACKING
    # ----------------------------------------------------------------------
    def _get_recent_fill(self, symbol: str, lookback_ms: int = 60000) -> Optional[Dict[str, Any]]:
        """Recupera il fill più recente per un simbolo.

        Args:
            symbol: Il simbolo da cercare (es. "BTC", "SOL")
            lookback_ms: Finestra temporale in millisecondi (default 60s)

        Returns:
            Dict con price, size, fee, oid, raw oppure None
        """
        try:
            current_time = int(time.time() * 1000)
            start_time = current_time - lookback_ms

            fills = self.info.user_fills_by_time(
                self.account_address,
                start_time,
                current_time
            )

            # Filtra per simbolo e prendi il più recente
            symbol_fills = [f for f in fills if f.get("coin") == symbol]
            if not symbol_fills:
                return None

            # Ordina per timestamp decrescente
            symbol_fills.sort(key=lambda x: x.get("time", 0), reverse=True)
            fill = symbol_fills[0]

            return {
                "price": float(fill.get("px", 0)),
                "size": abs(float(fill.get("sz", 0))),
                "fee": abs(float(fill.get("fee", 0))) if fill.get("fee") else None,
                "oid": str(fill.get("oid", "")),
                "closed_pnl": float(fill.get("closedPnl", 0)) if fill.get("closedPnl") else None,
                "raw": fill,
            }
        except Exception as e:
            print(f"⚠️ Errore recuperando fill per {symbol}: {e}")
            return None

    def get_positions(self) -> List[Dict[str, Any]]:
        """Restituisce lista delle posizioni aperte con simbolo e size."""
        positions = []
        data = self.info.user_state(self.account_address)
        asset_positions = data.get("assetPositions", [])

        for p in asset_positions:
            if isinstance(p, dict) and "position" in p:
                pos = p["position"]
                coin = pos.get("coin", "")
            else:
                pos = p
                coin = p.get("coin", p.get("symbol", ""))

            if not pos or not coin:
                continue

            size = float(pos.get("szi", 0))
            if size == 0:
                continue

            positions.append({
                "symbol": coin,
                "size": size,
                "direction": "long" if size > 0 else "short",
                "entry_price": float(pos.get("entryPx", 0)),
            })

        return positions

    def sync_closed_trades(self) -> int:
        """Sincronizza trade chiusi da SL/TP on-chain.

        Controlla se ci sono trade aperti nel DB che non hanno più
        una posizione corrispondente su Hyperliquid (chiusi da SL/TP).

        Returns:
            Numero di trade sincronizzati
        """
        synced = 0
        try:
            open_trades = db_utils.get_all_open_trades()
            if not open_trades:
                return 0

            current_positions = {p["symbol"]: p for p in self.get_positions()}

            for trade in open_trades:
                symbol = trade["symbol"]

                # Se la posizione non esiste più, il trade è stato chiuso
                if symbol not in current_positions:
                    print(f"🔄 Trade {trade['id']} ({symbol}) chiuso on-chain, sincronizzando...")

                    # Recupera fill di chiusura (cerca negli ultimi 24h)
                    exit_data = self._get_recent_fill(symbol, lookback_ms=24 * 60 * 60 * 1000)

                    if exit_data:
                        # Determina se SL o TP in base al P&L
                        closed_pnl = exit_data.get("closed_pnl", 0)
                        close_type = "tp" if closed_pnl and closed_pnl > 0 else "sl"

                        db_utils.close_trade(
                            trade_id=trade["id"],
                            exit_price=exit_data["price"],
                            exit_size=exit_data["size"],
                            close_type=close_type,
                            exit_reason=f"Chiuso automaticamente via {close_type.upper()}",
                            exit_order_id=exit_data.get("oid"),
                            exit_fee=exit_data.get("fee"),
                            exit_fill_data=exit_data.get("raw"),
                        )
                        synced += 1
                    else:
                        # Nessun fill trovato, chiudi comunque con dati stimati
                        mids = self.info.all_mids()
                        exit_price = float(mids.get(symbol, trade["entry_price"]))

                        db_utils.close_trade(
                            trade_id=trade["id"],
                            exit_price=exit_price,
                            exit_size=trade["entry_size"],
                            close_type="unknown",
                            exit_reason="Posizione non trovata, chiusura stimata",
                        )
                        synced += 1
                        print(f"⚠️ Trade {trade['id']} chiuso con dati stimati (fill non trovato)")

            if synced > 0:
                print(f"✅ Sincronizzati {synced} trade chiusi on-chain")

        except Exception as e:
            print(f"⚠️ Errore durante sync trade: {e}")

        return synced