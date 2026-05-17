"""
╔══════════════════════════════════════════════════════════════╗
║          SOLANA SNIPER BOT — BASE RÉELLE                     ║
║          TP1 +30%→20% | TP2 +50%→40% | TP3 +500%→reste      ║
╚══════════════════════════════════════════════════════════════╝

VARIABLES D'ENVIRONNEMENT REQUISES (Railway) :
    WALLET_PRIVATE_KEY   — clé privée base58
    WALLET_ADDRESS       — adresse publique
    JUPITER_API_KEY      — clé API Jupiter (portal.jup.ag)

DÉPENDANCES :
    pip install requests websocket-client solana solders base58
"""

import os
import json
import time
import logging
import threading
import requests

try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────
CONFIG = {
    # Capital & positions
    "stake_pct":             10,       # % du wallet par trade
    "max_positions":          3,

    # Filtres token
    "min_liquidity_usd":   5_000,
    "max_liquidity_usd": 500_000,
    "min_score":              60,      # Score anti-rug minimum
    "max_top_holder_pct":     20,      # % max du top holder

    # Gestion des positions
    "stop_loss_pct":         -25,      # Stop loss à -25%
    "breakeven_trigger_x":   1.17,     # Activation break-even à +17%
    "tp1_x":                 1.30,     # TP1 : vend 20% à +30%
    "tp1_sell_pct":          20,
    "tp2_x":                 1.50,     # TP2 : vend 40% à +50%
    "tp2_sell_pct":          40,
    "tp3_x":                 6.00,     # TP3 : vend le reste à +500%
    "max_hold_minutes":       120,
    "scan_interval_sec":        2,
}

# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.FileHandler("sniper_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("SNIPER")

# ─────────────────────────────────────────────────────────────────
# WALLET
# ─────────────────────────────────────────────────────────────────
class Wallet:
    """Wallet réel — lit le solde depuis la blockchain."""

    RPC = "https://api.mainnet-beta.solana.com"

    def __init__(self):
        self.address    = os.getenv("WALLET_ADDRESS", "")
        self.sol_balance = 0.0
        self.positions   = {}          # symbol → dict
        self.closed_trades = []
        self.total_trades  = 0
        self.wins = self.losses = 0
        self.refresh_balance()

    def refresh_balance(self):
        if not self.address:
            return
        try:
            resp = requests.post(
                self.RPC,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [self.address],
                },
                timeout=10,
            )
            lamports = resp.json().get("result", {}).get("value", 0)
            self.sol_balance = lamports / 1_000_000_000
        except Exception as e:
            log.warning(f"  ⚠️  Refresh balance : {e}")

    def can_trade(self) -> bool:
        return (
            self.sol_balance > 0.005
            and len(self.positions) < CONFIG["max_positions"]
        )


# ─────────────────────────────────────────────────────────────────
# EXECUTOR (import depuis solana_executor.py)
# ─────────────────────────────────────────────────────────────────
try:
    from solana_executor import SolanaExecutor
    _executor = SolanaExecutor()
    log.info("  ✅ SolanaExecutor chargé")
except Exception as e:
    _executor = None
    log.warning(f"  ⚠️  SolanaExecutor indisponible : {e}")


# ─────────────────────────────────────────────────────────────────
# ANTI-RUG ANALYZER
# ─────────────────────────────────────────────────────────────────
class AntiRugAnalyzer:
    """
    Score 0–100 :
      Mint authority désactivée   +25 pts
      LP tokens brûlés            +25 pts
      Top holder < 20%            +20 pts
      Liquidité > $5k             +15 pts
      Buy pressure > 55%          +15 pts
    """

    def analyze(self, token: dict) -> dict:
        score  = 0
        flags  = []
        detail = {}

        # ── RugCheck ──────────────────────────────────────────────
        rc = self._rugcheck(token["address"])
        mint_disabled = rc["mint_disabled"]
        lp_burned     = rc["lp_burned"]
        top_holder    = rc["top_holder_pct"]

        # 1. Mint authority
        if mint_disabled:
            score += 25
            detail["Mint Authority"] = "✅ Désactivée (+25pts)"
        else:
            flags.append("MINT_ACTIVE")
            detail["Mint Authority"] = "❌ ACTIVE — risque"

        # 2. LP tokens
        if lp_burned:
            score += 25
            detail["LP Tokens"] = "✅ Brûlés (+25pts)"
        else:
            flags.append("LP_NOT_BURNED")
            detail["LP Tokens"] = "❌ Non brûlés"

        # 3. Top holder
        if top_holder <= CONFIG["max_top_holder_pct"]:
            score += 20
            detail["Top Holder"] = f"✅ {top_holder:.1f}% (+20pts)"
        else:
            flags.append("WHALE_CONCENTRATION")
            detail["Top Holder"] = f"❌ {top_holder:.1f}% — dangereux"

        # 4. Liquidité
        liq = token["liq_usd"]
        if liq >= CONFIG["min_liquidity_usd"]:
            score += 15
            detail["Liquidité"] = f"✅ ${liq:,.0f} (+15pts)"
        else:
            flags.append("LOW_LIQUIDITY")
            detail["Liquidité"] = f"❌ ${liq:,.0f} — trop faible"

        # 5. Buy pressure
        buy_pct = token["buy_pct"]
        if buy_pct >= 55:
            score += 15
            detail["Buy Pressure"] = f"✅ {buy_pct:.0f}% (+15pts)"
        else:
            detail["Buy Pressure"] = f"⚠️  {buy_pct:.0f}% (+0pts)"

        return {
            "score":  score,
            "flags":  flags,
            "detail": detail,
        }

    # ── RugCheck API ──────────────────────────────────────────────
    def _rugcheck(self, addr: str) -> dict:
        defaults = {
            "mint_disabled":   False,
            "lp_burned":       False,
            "top_holder_pct":  100.0,
        }
        try:
            r = requests.get(
                f"https://api.rugcheck.xyz/v1/tokens/{addr}/report/summary",
                timeout=8,
            )
            data = r.json()
            risks      = data.get("risks", [])
            risk_names = [x.get("name", "").lower() for x in risks]

            # Mint authority
            mint_disabled = (
                "mint authority disabled" in risk_names
                or not any("mint" in n for n in risk_names)
            )

            # LP burned — champ API ou présence dans risks
            lp_burned = False
            if "lp burned" in risk_names:
                lp_burned = True
            elif data.get("markets"):
                lp_burned = (
                    data["markets"][0].get("lp", {}).get("lpBurned", False)
                )

            # ── Top holder — FIX PRINCIPAL ────────────────────────
            # L'API RugCheck ne retourne pas topHolders pour les tokens
            # pump.fun récents. La valeur est dans risks sous le nom
            # "Single holder ownership" avec value "XX.XX%"
            top_holder = 100.0
            for risk in risks:
                name = risk.get("name", "").lower()
                if "single holder" in name:
                    raw = risk.get("value", "").replace("%", "").strip()
                    try:
                        top_holder = float(raw)
                    except ValueError:
                        pass
                    break
            else:
                # Fallback : topHolders classique (tokens Raydium)
                holders = (
                    data.get("topHolders")
                    or data.get("top_holders")
                    or data.get("insiders")
                    or []
                )
                if holders:
                    raw = float(
                        holders[0].get("pct", holders[0].get("percentage", 1.0))
                    )
                    top_holder = raw * 100 if raw <= 1.0 else raw

            return {
                "mint_disabled":  mint_disabled,
                "lp_burned":      lp_burned,
                "top_holder_pct": top_holder,
            }

        except Exception as e:
            log.debug(f"RugCheck error {addr[:8]}: {e}")
            return defaults


# ─────────────────────────────────────────────────────────────────
# TOKEN DETECTOR — WebSocket PumpPortal
# ─────────────────────────────────────────────────────────────────
class TokenDetector:
    """Reçoit les nouveaux tokens pump.fun via WebSocket en temps réel."""

    WS_URL   = "wss://pumpportal.fun/api/data"
    SOL_PRICE = 150.0   # Prix SOL approximatif — mis à jour dynamiquement

    def __init__(self):
        self._queue     = []
        self._lock      = threading.Lock()
        self._connected = False
        self._ws        = None
        if WS_AVAILABLE:
            self._start_ws()
        else:
            log.warning("  ⚠️  websocket-client non installé")

    # ── WebSocket ─────────────────────────────────────────────────
    def _start_ws(self):
        def on_open(ws):
            self._connected = True
            log.info("  📡 WebSocket PumpPortal connecté")
            ws.send(json.dumps({"method": "subscribeNewToken"}))

        def on_message(ws, message):
            try:
                data = json.loads(message)
                mint = data.get("mint", "")
                if not mint:
                    return

                sol_reserves = float(data.get("vSolInBondingCurve", 0))
                liq          = sol_reserves * 2 * self.SOL_PRICE
                market_cap   = float(data.get("marketCapSol", 0)) * self.SOL_PRICE
                supply       = float(data.get("totalSupply", 1_000_000_000))
                price        = market_cap / supply if supply > 0 else 0

                # buy_pct : PumpPortal n'envoie pas buys/sells à la création
                # → 50.0 (neutre) au lieu d'une valeur hardcodée
                buys  = int(data.get("buys",  0) or data.get("buyCount",  0) or 0)
                sells = int(data.get("sells", 0) or data.get("sellCount", 0) or 0)
                buy_pct = (buys / (buys + sells) * 100) if (buys + sells) > 0 else 50.0

                token = {
                    "symbol":    data.get("symbol", "???"),
                    "address":   mint,
                    "pair_addr": mint,
                    "liq_usd":   liq,
                    "age_min":   0.1,
                    "price_usd": price,
                    "buy_pct":   buy_pct,
                }
                with self._lock:
                    self._queue.append(token)

            except Exception as e:
                log.debug(f"WS parse: {e}")

        def on_error(ws, error):
            log.info(f"  ⚠️  WebSocket erreur : {error}")
            self._connected = False

        def on_close(ws, *args):
            self._connected = False
            log.info("  ⚠️  WebSocket déconnecté — reconnexion dans 5s")
            time.sleep(5)
            self._start_ws()

        def run():
            try:
                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log.info(f"  ⚠️  WebSocket run: {e}")
                time.sleep(5)
                self._start_ws()

        t = threading.Thread(target=run, daemon=True)
        t.start()

    def get_new_tokens(self) -> list:
        with self._lock:
            tokens = self._queue.copy()
            self._queue.clear()
        if tokens:
            log.info(f"  📡 WebSocket : {len(tokens)} nouveau(x) token(s)")
        return tokens


# ─────────────────────────────────────────────────────────────────
# POSITION MANAGER
# ─────────────────────────────────────────────────────────────────
class PositionManager:
    """Gère l'ouverture, le suivi et la fermeture des positions."""

    def __init__(self, wallet: Wallet):
        self.wallet = wallet

    def open_position(self, token: dict, score: int) -> bool:
        w = self.wallet
        if not w.can_trade():
            return False

        size_sol = round(w.sol_balance * (CONFIG["stake_pct"] / 100), 4)
        if size_sol < 0.001:
            log.warning("  ⚠️  Solde insuffisant")
            return False

        # Exécution achat
        success = False
        if _executor:
            result = _executor.buy(token["address"], token["symbol"], size_sol)
            success = result.get("success", False)
            if not success:
                log.error(f"  ❌ Achat échoué : {result.get('reason', '?')}")
                return False
        else:
            # Mode simulation
            success = True

        w.refresh_balance()
        from datetime import datetime, timezone
        w.positions[token["symbol"]] = {
            "symbol":        token["symbol"],
            "address":       token["address"],
            "entry_price":   token["price_usd"],
            "current_price": token["price_usd"],
            "entry_time":    datetime.now(timezone.utc),
            "sol_invested":  size_sol,
            "remaining_pct": 100.0,
            "tp1_hit":       False,
            "tp2_hit":       False,
            "breakeven":     False,
            "score":         score,
        }
        w.total_trades += 1
        log.info(f"  ✅ Position ouverte — {token['symbol']} | {size_sol:.4f} SOL")
        return True

    def update_positions(self, prices: dict):
        """Met à jour et ferme les positions selon TP/SL."""
        from datetime import datetime, timezone
        to_close = []

        for sym, pos in list(self.wallet.positions.items()):
            current = prices.get(pos["address"], pos["entry_price"])
            pos["current_price"] = current

            if pos["entry_price"] == 0:
                continue
            x        = current / pos["entry_price"]
            age_min  = (datetime.now(timezone.utc) - pos["entry_time"]).seconds / 60

            # Break-even
            if x >= CONFIG["breakeven_trigger_x"] and not pos["breakeven"]:
                pos["breakeven"] = True
                log.info(f"  🔒 Break-even activé — {sym}")

            # Stop loss (ou break-even)
            sl_x = 1.0 if pos["breakeven"] else (1 + CONFIG["stop_loss_pct"] / 100)
            if x <= sl_x:
                to_close.append((sym, x, "Stop Loss / Break-even"))
                continue

            # Time out
            if age_min >= CONFIG["max_hold_minutes"]:
                to_close.append((sym, x, "Timeout"))
                continue

            # TP1 +30% → vend 20%
            if x >= CONFIG["tp1_x"] and not pos["tp1_hit"]:
                pos["tp1_hit"] = True
                self._partial_sell(pos, CONFIG["tp1_sell_pct"], f"TP1 +{(CONFIG['tp1_x']-1)*100:.0f}%")

            # TP2 +50% → vend 40%
            if x >= CONFIG["tp2_x"] and not pos["tp2_hit"]:
                pos["tp2_hit"] = True
                self._partial_sell(pos, CONFIG["tp2_sell_pct"], f"TP2 +{(CONFIG['tp2_x']-1)*100:.0f}%")

            # TP3 +500% → vend le reste
            if x >= CONFIG["tp3_x"]:
                to_close.append((sym, x, f"TP3 +{(CONFIG['tp3_x']-1)*100:.0f}%"))

        for sym, x, reason in to_close:
            self._close_position(sym, x, reason)

    def _partial_sell(self, pos: dict, sell_pct: int, label: str):
        actual_pct = sell_pct * pos["remaining_pct"] / 100
        pos["remaining_pct"] -= actual_pct
        if _executor:
            _executor.sell(pos["address"], pos["symbol"], int(actual_pct))
        log.info(f"  💰 {label} — {pos['symbol']} | vendu {actual_pct:.0f}% | reste {pos['remaining_pct']:.0f}%")

    def _close_position(self, sym: str, x: float, reason: str):
        pos = self.wallet.positions.pop(sym, None)
        if not pos:
            return
        if _executor:
            _executor.sell(pos["address"], sym, 100)
        pnl = (x - 1) * 100
        self.wallet.closed_trades.append({"symbol": sym, "x": x, "reason": reason})
        if x >= 1:
            self.wallet.wins += 1
        else:
            self.wallet.losses += 1
        self.wallet.refresh_balance()
        emoji = "✅" if x >= 1 else "❌"
        log.info(f"  {emoji} Fermé {sym} | x{x:.2f} ({pnl:+.0f}%) | {reason}")


# ─────────────────────────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ─────────────────────────────────────────────────────────────────
def main():
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║                SOLANA SNIPER BOT — BASE RÉELLE               ║")
    print(f"║  🎯 Score min : {CONFIG['min_score']}/100  |  Ctrl+C pour arrêter              ║")
    print(f"║  TP1 +30%→20% | TP2 +50%→40% | TP3 +500%→reste             ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    wallet   = Wallet()
    detector = TokenDetector()
    analyzer = AntiRugAnalyzer()
    manager  = PositionManager(wallet)

    log.info(f"  💰 Solde wallet : {wallet.sol_balance:.4f} SOL")
    log.info(f"  ✅ Bot actif — Capital : {wallet.sol_balance:.4f} SOL")

    try:
        while True:
            new_tokens = detector.get_new_tokens()

            for token in new_tokens:
                liq = token.get("liq_usd", 0)
                sym = token.get("symbol", "?")

                # Filtre liquidité rapide avant RugCheck
                if liq < CONFIG["min_liquidity_usd"]:
                    log.info(f"  ⏭️  {sym} rejeté — liq ${liq:.0f}")
                    continue

                print(f"\n  ━━━ NOUVEAU TOKEN ━━━")
                print(f"  🚀 {sym} | Age: {token.get('age_min', 0):.1f}min | Liq: ${liq:,.0f}")
                if token.get("address"):
                    print(f"  🔗 https://dexscreener.com/solana/{token['address']}")

                result = analyzer.analyze(token)
                score  = result["score"]
                flags  = result["flags"]
                detail = result["detail"]

                print(f"  📊 SCORE : {score}/100")
                for k, v in detail.items():
                    print(f"     {k:<20} {v}")

                if flags:
                    print(f"  🚨 FLAGS : {' | '.join(flags)}")

                if score >= CONFIG["min_score"] and not flags:
                    print(f"  ⚡ SNIPE ! {sym} — Score {score}/100")
                    manager.open_position(token, score)
                else:
                    print(f"  ❌ REFUSÉ (score {score} < {CONFIG['min_score']})")

            # Mise à jour positions (prix en temps réel à implémenter)
            if wallet.positions:
                manager.update_positions({})

            time.sleep(CONFIG["scan_interval_sec"])

    except KeyboardInterrupt:
        print("\n  🛑 Bot arrêté.")
        log.info(f"  📊 Bilan — Trades: {wallet.total_trades} | Wins: {wallet.wins} | Losses: {wallet.losses}")
        log.info(f"  💰 Solde final : {wallet.sol_balance:.4f} SOL")


if __name__ == "__main__":
    main()
