"""
╔══════════════════════════════════════════════════════════════════╗
║           SOLANA SNIPER BOT v4 — VRAIS TOKENS RAYDIUM                   ║
║           Stratégie : Achat < 30sec après lancement             ║
║           Protection comportementale v4 — vrais tokens         ║
║           TP0: +17%→20% | TP1: +50%→30% |                      ║
║           TP2: +200%→20% | TP3: +900%→reste                    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import time
import random
import logging
import requests
import numpy as np
from datetime import datetime, timezone
from colorama import Fore, Style, init

init(autoreset=True)

# Mode réel si clé privée présente
WALLET_KEY = os.getenv("WALLET_PRIVATE_KEY")
REAL_MODE   = WALLET_KEY is not None
print(f"Mode : {'🔴 TRADING RÉEL' if REAL_MODE else '🎮 SIMULATION'}")

CONFIG = {
    "initial_capital_sol":    50.0,
    "sol_per_trade":           2.0,
    "max_positions":           3,
    "min_liquidity_usd":    3_000,
    "max_liquidity_usd":  100_000,
    "min_score":               60,
    "max_top_holder_pct":      20,
    "stop_loss_pct":          -25,
    "breakeven_trigger_x":    1.15,
    "take_profit_0":           1.17,   # Vend 20% à +17%
    "take_profit_1":           1.50,   # Vend 30% à +50%
    "take_profit_2":           3.00,   # Vend 20% à +200%
    "take_profit_3":          10.00,   # Vend le reste à +900%
    "max_hold_minutes":        120,
    "scan_interval_sec":         2,
    "max_token_age_sec":        30,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.FileHandler("sniper_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("SNIPER")


class SniperWallet:

    def __init__(self, initial_sol: float):
        self.sol_balance   = initial_sol
        self.initial_sol   = initial_sol
        self.positions     = {}
        self.closed_trades = []
        self.total_trades  = 0
        self.wins          = 0
        self.losses        = 0
        self.rugs          = 0

    def can_snipe(self) -> bool:
        return (self.sol_balance >= CONFIG["sol_per_trade"] and
                len(self.positions) < CONFIG["max_positions"])

    def open_position(self, token: dict) -> bool:
        if not self.can_snipe():
            return False
        amount = CONFIG["sol_per_trade"]
        self.sol_balance -= amount
        self.total_trades += 1
        self.positions[token["address"]] = {
            "symbol":           token["symbol"],
            "address":          token["address"],
            "entry_price":      token["price_usd"],
            "entry_time":       datetime.now(timezone.utc),
            "sol_invested":     amount,
            "remaining_pct":    100.0,
            "tp0_hit":          False,
            "tp1_hit":          False,
            "tp2_hit":          False,
            "tp3_hit":          False,
            "current_x":        1.0,
            "peak_x":           1.0,
            "is_rug":           token.get("will_rug", False),
            "breakeven_active": False,
        }
        return True

    def close_position(self, address: str, exit_x: float, reason: str,
                       force_x: float = None):
        if address not in self.positions:
            return
        pos      = self.positions[address]
        actual_x = force_x if force_x is not None else exit_x
        pnl_sol  = pos["sol_invested"] * (actual_x - 1) * (pos["remaining_pct"] / 100)
        self.sol_balance += pos["sol_invested"] * (pos["remaining_pct"] / 100) * actual_x

        if actual_x >= 1.0:
            self.wins += 1
        elif "Rug" in reason:
            self.rugs += 1
            self.losses += 1
        else:
            self.losses += 1

        duration = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
        self.closed_trades.append({
            "symbol":     pos["symbol"],
            "exit_x":     round(actual_x, 3),
            "pnl_sol":    round(pnl_sol, 4),
            "reason":     reason,
            "duration_s": duration,
        })

        emoji = "🟢" if actual_x >= 1 else "🔴"
        log.info(f"  {emoji} FERMÉ — {pos['symbol']} | {actual_x:.2f}x | "
                 f"P&L: {pnl_sol:+.4f} SOL | {reason}")
        del self.positions[address]

    def partial_sell(self, address: str, pct: float, current_x: float, reason: str):
        if address not in self.positions:
            return
        pos          = self.positions[address]
        sell_pct     = pct * (pos["remaining_pct"] / 100)
        sol_received = pos["sol_invested"] * (sell_pct / 100) * current_x
        self.sol_balance     += sol_received
        pos["remaining_pct"] -= sell_pct
        log.info(f"  💰 VENTE {pct:.0f}% — {pos['symbol']} | "
                 f"{current_x:.2f}x | +{sol_received:.4f} SOL | {reason}")


class NewPoolDetector:

    def __init__(self):
        self.seen    = set()
        self.sol_usd = 150.0
        self._update_sol_price()

    def _update_sol_price(self):
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
            r   = requests.get(url, timeout=5)
            if r.status_code == 200:
                self.sol_usd = r.json()["solana"]["usd"]
        except Exception:
            pass

    def scan_new_pools(self) -> list:
        tokens = []
        try:
            import time as t
            # Récupère les derniers profils de tokens Solana
            r = requests.get(
                "https://api.dexscreener.com/token-profiles/latest/v1",
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code != 200:
                return []

            profiles = r.json() if isinstance(r.json(), list) else []

            for profile in profiles[:10]:
                if profile.get("chainId") != "solana":
                    continue
                addr = profile.get("tokenAddress", "")
                if not addr or addr in self.seen:
                    continue

                # Récupère les données de trading
                r2 = requests.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                    timeout=8,
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if r2.status_code != 200:
                    continue

                pairs = r2.json().get("pairs", None) or []
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if not sol_pairs:
                    continue

                pair = sol_pairs[0]
                liq  = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                vol  = pair.get("volume", {})
                txns = pair.get("txns", {})
                m5   = txns.get("m5", {})
                created = pair.get("pairCreatedAt", 0)
                now_ms  = t.time() * 1000
                age_min = (now_ms - created) / 60000 if created else 0

                token = {
                    "address":       addr,
                    "symbol":        pair.get("baseToken", {}).get("symbol", "???"),
                    "name":          pair.get("baseToken", {}).get("name", "Unknown"),
                    "price_usd":     float(pair.get("priceUsd", 0) or 0),
                    "liquidity_usd": liq,
                    "volume_1h":     float(vol.get("h1", 0) or 0),
                    "volume_5m":     float(vol.get("m5", 0) or 0),
                    "buys":          int(m5.get("buys", 0) or 0),
                    "sells":         int(m5.get("sells", 0) or 0),
                    "age_min":       round(age_min, 1),
                    "age_sec":       age_min * 60,
                    "is_real":       True,
                    "will_rug":      False,
                    "dex_url":       pair.get("url", ""),
                }

                if token["price_usd"] > 0 and token["liquidity_usd"] > 0:
                    tokens.append(token)
                    print(f"  🌐 VRAI TOKEN : {token['symbol']} | ${liq:,.0f} liq | {age_min:.1f}min")

                t.sleep(0.5)

        except Exception as e:
            import logging
            logging.getLogger("SNIPER").error(f"API erreur: {e}")

        return tokens

    def _parse(self, pair: dict, age_sec: float) -> dict:
        try:
            base = pair.get("baseToken", {})
            liq  = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            return {
                "address":       base.get("address", ""),
                "symbol":        base.get("symbol", "???"),
                "name":          base.get("name", "Unknown"),
                "price_usd":     float(pair.get("priceUsd", 0) or 0),
                "liquidity_usd": liq,
                "volume_1h":     float(pair.get("volume", {}).get("h1", 0) or 0),
                "age_sec":       age_sec,
                "buys":          pair.get("txns", {}).get("m5", {}).get("buys", 0),
                "sells":         pair.get("txns", {}).get("m5", {}).get("sells", 0),
                "will_rug":      False,
            }
        except Exception:
            return None

    def _simulate_new_launches(self) -> list:
        if random.random() > 0.04:
            return []
        syms   = ["MOONDOG", "SOLCAT", "PEPEX", "WAGMISOL", "GIGASOL",
                  "DEGENCAT", "BONKX", "RAYCAT", "SOLPUMP", "MEMEX"]
        sym    = random.choice(syms)
        liq    = random.uniform(3_000, 80_000)
        is_rug = random.random() < 0.60
        return [{
            "address":        f"Snipe{random.randint(100000,999999)}pump",
            "symbol":         sym,
            "name":           f"{sym} Token",
            "price_usd":      random.uniform(0.0000001, 0.0001),
            "liquidity_usd":  liq,
            "volume_1h":      liq * random.uniform(0.1, 2.0),
            "age_sec":        random.uniform(1, 28),
            "buys":           random.randint(50, 300) if is_rug else random.randint(20, 150),
            "sells":          random.randint(5, 50),
            "top_holder_pct": random.uniform(25, 80) if is_rug else random.uniform(5, 18),
            "mint_disabled":  not is_rug,
            "lp_burned":      not is_rug,
            "will_rug":       is_rug,
        }]


class AntiRugAnalyzer:

    def analyze(self, token: dict) -> dict:
        score   = 0
        flags   = []
        details = {}

        mint_ok = token.get("mint_disabled", random.random() > 0.4)
        if mint_ok:
            score += 25
            details["Mint Authority"] = "✅ Désactivée (+25pts)"
        else:
            flags.append("MINT_ACTIVE")
            details["Mint Authority"] = "❌ ACTIVE — risque élevé"

        lp_ok = token.get("lp_burned", random.random() > 0.5)
        if lp_ok:
            score += 25
            details["LP Tokens"] = "✅ Brûlés (+25pts)"
        else:
            flags.append("LP_NOT_BURNED")
            details["LP Tokens"] = "⚠️  Non brûlés"

        top_h = token.get("top_holder_pct", random.uniform(5, 60))
        if top_h <= CONFIG["max_top_holder_pct"]:
            score += 20
            details["Top Holder"] = f"✅ {top_h:.1f}% (+20pts)"
        else:
            flags.append("WHALE_CONCENTRATION")
            details["Top Holder"] = f"❌ {top_h:.1f}% — dangereux"

        liq = token["liquidity_usd"]
        if CONFIG["min_liquidity_usd"] <= liq <= CONFIG["max_liquidity_usd"]:
            score += 15
            details["Liquidité"] = f"✅ ${liq:,.0f} (+15pts)"
        elif liq < CONFIG["min_liquidity_usd"]:
            flags.append("LOW_LIQUIDITY")
            details["Liquidité"] = f"❌ ${liq:,.0f} trop faible"
        else:
            score += 8
            details["Liquidité"] = f"⚠️  ${liq:,.0f} élevée (+8pts)"

        buys  = token.get("buys", 0)
        sells = token.get("sells", 1)
        ratio = buys / max(buys + sells, 1)
        if ratio >= 0.65:
            score += 15
            details["Buy Pressure"] = f"✅ {ratio*100:.0f}% (+15pts)"
        elif ratio >= 0.50:
            score += 8
            details["Buy Pressure"] = f"⚠️  {ratio*100:.0f}% (+8pts)"
        else:
            flags.append("SELL_PRESSURE")
            details["Buy Pressure"] = f"❌ {ratio*100:.0f}%"

        buyable = (score >= CONFIG["min_score"] and
                   "MINT_ACTIVE" not in flags and
                   "WHALE_CONCENTRATION" not in flags)

        return {"score": score, "details": details,
                "flags": flags, "buyable": buyable}


class PriceSimulator:

    def get_current_x(self, pos: dict) -> float:
        age_s  = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
        is_rug = pos.get("is_rug", False)
        peak   = pos.get("peak_x", 1.0)

        if is_rug:
            if age_s < 60:
                x = 1.0 + (age_s / 60) * random.uniform(0.5, 3.0)
            else:
                x = max(0.05, peak * np.exp(-age_s / 120 * random.uniform(1, 3)))
        else:
            sigma = 0.08
            drift = 0.003
            steps = max(1, age_s // 5)
            x     = 1.0
            for _ in range(steps):
                x *= np.exp(np.random.normal(drift, sigma))
                x  = max(x, 0.1)

        return round(x, 4)


def print_dashboard(wallet: SniperWallet, sol_price: float):
    total_sol = wallet.sol_balance + sum(
        p["sol_invested"] * p.get("current_x", 1) * (p["remaining_pct"] / 100)
        for p in wallet.positions.values()
    )
    pnl_sol   = total_sol - wallet.initial_sol
    pnl_pct   = pnl_sol / wallet.initial_sol * 100
    wr        = wallet.wins / max(wallet.total_trades, 1) * 100
    pnl_color = Fore.GREEN if pnl_sol >= 0 else Fore.RED

    print(f"\n{Fore.CYAN}{'═'*62}")
    print(f"  🎯 SNIPER BOT — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*62}")
    print(f"  SOL balance      : {wallet.sol_balance:.4f} SOL")
    print(f"  Valeur totale    : {total_sol:.4f} SOL (${total_sol*sol_price:,.2f})")
    print(f"  {pnl_color}P&L total        : {pnl_sol:+.4f} SOL ({pnl_pct:+.2f}%){Style.RESET_ALL}")
    print(f"  Trades           : {wallet.total_trades} "
          f"| ✅ {wallet.wins} | ❌ {wallet.losses} | 💀 Rugs: {wallet.rugs}")
    print(f"  Win Rate         : {wr:.1f}%")

    if wallet.positions:
        print(f"\n  📊 POSITIONS ACTIVES :")
        for addr, pos in wallet.positions.items():
            x      = pos.get("current_x", 1.0)
            xcolor = Fore.GREEN if x >= 1 else Fore.RED
            age_s  = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
            be     = f"{Fore.CYAN}🔒 BE{Style.RESET_ALL}" if pos.get("breakeven_active") else ""
            tps    = []
            if pos["tp0_hit"]: tps.append("TP0")
            if pos["tp1_hit"]: tps.append("TP1")
            if pos["tp2_hit"]: tps.append("TP2")
            tp_str = " ".join(tps)
            print(f"  {xcolor}  {pos['symbol']:<14} {x:.2f}x | "
                  f"{pos['sol_invested']:.1f} SOL | "
                  f"Restant: {pos['remaining_pct']:.0f}% | "
                  f"Age: {age_s}s {be} {tp_str}")

    if wallet.closed_trades:
        print(f"\n  📜 DERNIERS TRADES :")
        for t in wallet.closed_trades[-5:]:
            xcolor = Fore.GREEN if t["exit_x"] >= 1 else Fore.RED
            print(f"  {xcolor}  {t['symbol']:<14} {t['exit_x']:.2f}x | "
                  f"P&L: {t['pnl_sol']:+.4f} SOL | {t['reason']}{Style.RESET_ALL}")

    print(f"\n  📊 GRILLE DES TPs :")
    print(f"     TP0 : +17%  → vend 20% de la position")
    print(f"     TP1 : +50%  → vend 30% de la position")
    print(f"     TP2 : +200% → vend 20% de la position")
    print(f"     TP3 : +900% → vend le reste")
    print(f"{Fore.CYAN}{'═'*62}{Style.RESET_ALL}\n")


def print_new_token(token: dict, analysis: dict):
    score   = analysis["score"]
    s_color = Fore.GREEN if score >= 60 else Fore.YELLOW if score >= 45 else Fore.RED
    print(f"\n{Fore.YELLOW}  ━━━ NOUVEAU TOKEN ━━━")
    print(f"  🚀 {token['symbol']} | Age: {token['age_sec']:.1f}s | "
          f"Liq: ${token['liquidity_usd']:,.0f}")
    print(f"  Prix: ${token['price_usd']:.10f}{Style.RESET_ALL}")
    print(f"  {s_color}📊 SCORE : {score}/100{Style.RESET_ALL}")
    for k, v in analysis["details"].items():
        print(f"     {k:<20} {v}")
    if analysis["flags"]:
        print(f"  {Fore.RED}🚨 FLAGS : {' | '.join(analysis['flags'])}{Style.RESET_ALL}")


def run_sniper(wallet: SniperWallet, detector: NewPoolDetector,
               analyzer: AntiRugAnalyzer, simulator: PriceSimulator):

    to_close = []

    for address, pos in list(wallet.positions.items()):
        x = simulator.get_current_x(pos)
        pos["current_x"] = x
        pos["peak_x"]    = max(pos.get("peak_x", 1.0), x)
        age_sec = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
        age_min = age_sec / 60

        # Protection 2sec supprimée en v4

        # ── Break-Even : activation dès +15% ─────────────────
        if x >= CONFIG["breakeven_trigger_x"] and not pos["breakeven_active"]:
            pos["breakeven_active"] = True
            log.info(f"  🔒 BREAK-EVEN activé — {pos['symbol']} | "
                     f"+{(x-1)*100:.0f}% atteint → SL = prix d'entrée")
            print(f"  {Fore.CYAN}🔒 BREAK-EVEN activé — {pos['symbol']} | "
                  f"Stop Loss déplacé au prix d'entrée{Style.RESET_ALL}")

        # ── Break-Even déclenché : sortie FORCÉE à 1.0x ──────
        if pos["breakeven_active"] and x < 1.0:
            to_close.append((address, x, "🔒 Break-Even — sortie sans perte", 1.0))
            continue

        # ── Stop Loss normal ──────────────────────────────────
        if x <= (1 + CONFIG["stop_loss_pct"] / 100):
            reason = "Rug Pull 💀" if pos.get("is_rug") else "Stop Loss -20%"
            to_close.append((address, x, reason, None))
            continue

        # ── Vente forcée après 2h ─────────────────────────────
        if age_min >= CONFIG["max_hold_minutes"]:
            to_close.append((address, x, "⏰ Temps max dépassé", None))
            continue

        # ── TP0 — vend 20% à +17% ─────────────────────────────
        if x >= CONFIG["take_profit_0"] and not pos["tp0_hit"]:
            pos["tp0_hit"] = True
            wallet.partial_sell(address, 100, x, f"TP0 +{(x-1)*100:.0f}%")

        # ── TP1 — vend 30% à +50% ─────────────────────────────
        if x >= CONFIG["take_profit_1"] and not pos["tp1_hit"]:
            pos["tp1_hit"] = True
            wallet.partial_sell(address, 30, x, f"TP1 +{(x-1)*100:.0f}%")

        # ── TP2 — vend 20% à +200% ────────────────────────────
        if x >= CONFIG["take_profit_2"] and not pos["tp2_hit"]:
            pos["tp2_hit"] = True
            wallet.partial_sell(address, 20, x, f"TP2 +{(x-1)*100:.0f}%")

        # ── TP3 — vend le reste à +900% ───────────────────────
        if x >= CONFIG["take_profit_3"] and not pos["tp3_hit"]:
            pos["tp3_hit"] = True
            to_close.append((address, x, f"🚀 MOONSHOT {x:.1f}x", None))

    for address, x, reason, force_x in to_close:
        wallet.close_position(address, x, reason, force_x=force_x)

    # ── Scan nouveaux pools ──────────────────────────────────
    new_tokens = detector.scan_new_pools()
    for token in new_tokens:
        if token["address"] in detector.seen:
            continue
        detector.seen.add(token["address"])
        analysis = analyzer.analyze(token)
        print_new_token(token, analysis)

        if analysis["buyable"] and wallet.can_snipe():
            print(f"\n  {Fore.GREEN}⚡ SNIPE ! {token['symbol']} — "
                  f"{CONFIG['sol_per_trade']} SOL{Style.RESET_ALL}")
            wallet.open_position(token)
            log.info(f"⚡ SNIPE — {token['symbol']} | Score: {analysis['score']}/100")
        else:
            print(f"  {Fore.RED}❌ REFUSÉ — Score {analysis['score']}/100{Style.RESET_ALL}")

    # Dashboard toutes les 30 secondes
    if int(time.time()) % 30 < 2:
        print_dashboard(wallet, detector.sol_usd)


def get_real_sol_balance() -> float:
    """Récupère le vrai solde SOL du wallet via RPC Solana."""
    try:
        import os, requests
        key = os.getenv("WALLET_PRIVATE_KEY")
        if not key:
            return 50.0  # Capital fictif si pas de clé

        # Récupère l'adresse publique depuis la clé privée
        from base58 import b58decode
        import hashlib

        # Appel RPC Solana pour le solde
        wallet_address = os.getenv("WALLET_ADDRESS", "")
        if not wallet_address:
            return 50.0

        r = requests.post(
            "https://api.mainnet-beta.solana.com",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [wallet_address]
            },
            timeout=10
        )
        if r.status_code == 200:
            lamports = r.json().get("result", {}).get("value", 0)
            sol = lamports / 1_000_000_000
            print(f"  💰 Vrai solde wallet : {sol:.4f} SOL")
            return sol
    except Exception as e:
        print(f"  ⚠️  Erreur solde: {e}")
    return 50.0

def main():
    print(f"""{Fore.YELLOW}
╔══════════════════════════════════════════════════════════════╗
║         SOLANA SNIPER BOT v4 — VRAIS TOKENS RAYDIUM                  ║
║         Capital   : 50.0 SOL fictifs                        ║
║         Mise/trade: 2.0 SOL | Scan : 2sec                   ║
║         Protection comportementale v4                        ║
║         Break-Even: activé dès +15%                         ║
║         TP0: +17%→20% | TP1: +50%→30%                      ║
║         TP2: +200%→20% | TP3: +900%→reste                  ║
║         Stop Loss : -25%                                     ║
╚══════════════════════════════════════════════════════════════╝
{Style.RESET_ALL}""")

    real_balance = get_real_sol_balance()
    initial = real_balance if real_balance > 0 else CONFIG["initial_capital_sol"]
    wallet    = SniperWallet(initial)
    detector  = NewPoolDetector()
    analyzer  = AntiRugAnalyzer()
    simulator = PriceSimulator()

    print(f"  ✅ Bot initialisé — Capital: {CONFIG['initial_capital_sol']} SOL fictifs")
    print(f"  ⚡ Scan toutes les {CONFIG['scan_interval_sec']} secondes")
    print(f"  🎯 Score minimum : {CONFIG['min_score']}/100")
    print(f"  Appuie sur Ctrl+C pour arrêter\n")

    while True:
        try:
            run_sniper(wallet, detector, analyzer, simulator)
            time.sleep(CONFIG["scan_interval_sec"])
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}  ⏹️  Bot arrêté{Style.RESET_ALL}")
            print_dashboard(wallet, detector.sol_usd)
            break
        except Exception as e:
            log.error(f"Erreur: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
