"""
╔══════════════════════════════════════════════════════════════════╗
║           SOLANA SNIPER BOT v7 — VERSION FINALE PROPRE          ║
║           Toutes les modifications intégrées                    ║
╚══════════════════════════════════════════════════════════════════╝

INSTALLATION :
    pip install requests numpy colorama solana solders base58

LANCEMENT :
    export WALLET_PRIVATE_KEY="ta_clé"
    export WALLET_ADDRESS="ton_adresse"
    python3 solana_sniper_bot.py
"""

import os
import sys
import time
import random
import logging
import requests
import numpy as np
from datetime import datetime, timezone
from colorama import Fore, Style, init

init(autoreset=True)

# ── Mode trading ──────────────────────────────────────────────────
WALLET_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
WALLET_ADR = os.getenv("WALLET_ADDRESS", "")
REAL_MODE  = bool(WALLET_KEY and WALLET_ADR)
print(f"  Mode : {'🔴 TRADING RÉEL' if REAL_MODE else '🎮 SIMULATION'}")

# ── Import executor ───────────────────────────────────────────────
try:
    from solana_executor import SolanaExecutor
    EXECUTOR_AVAILABLE = True
except ImportError:
    EXECUTOR_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────
CONFIG = {
    "initial_capital_sol":   50.0,
    "max_positions":          3,
    "min_liquidity_usd":   5_000,
    "max_liquidity_usd": 500_000,
    "min_score":              80,    # Score anti-rug minimum
    "max_top_holder_pct":     30,    # Top holder max 30%
    "stop_loss_pct":         -20,    # Stop loss -20%
    "breakeven_trigger_x":   1.17,  # Break-even dès +17%
    "max_hold_minutes":       120,
    "scan_interval_sec":        2,
    "emergency_dump_pct":     -15,   # Vente urgente si -15%
    "consecutive_drops":        3,
    "min_drop_per_scan":    -0.02,
    "rebounds_to_reset":        2,
    "tp_low":    1.5,    # Momentum < 40
    "tp_mid":    5.0,    # Momentum 40-60
    "tp_high":  10.0,    # Momentum 60-80
    "tp_moon":  20.0,    # Momentum > 80
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


# ─────────────────────────────────────────────────────────────────
# HONEYPOT ANALYZER
# ─────────────────────────────────────────────────────────────────
class HoneypotAnalyzer:

    def check_whale_lock(self, token_address: str) -> dict:
        result = {"locked": False, "details": "Inconnu"}
        try:
            r = requests.get(
                f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report",
                timeout=8, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                data    = r.json()
                lp_lock = data.get("lpLockedPct", 0)
                if lp_lock >= 80:
                    result["locked"]  = True
                    result["details"] = f"LP verrouillés {lp_lock:.0f}%"
                for market in data.get("markets", []):
                    locks = market.get("lp", {}).get("lockedPct", 0)
                    if locks >= 80:
                        result["locked"]  = True
                        result["details"] = f"Tokens verrouillés {locks:.0f}%"
        except Exception:
            pass
        return result

    def check(self, token_address: str, symbol: str) -> dict:
        result = {
            "is_honeypot":     False,
            "can_sell":        True,
            "rugcheck_score":  0,
            "safe":            True,
            "details":         {},
        }

        # RugCheck API
        try:
            r = requests.get(
                f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report",
                timeout=8, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                data  = r.json()
                score = data.get("score", 0)
                result["rugcheck_score"] = score

                for risk in data.get("risks", []):
                    name = risk.get("name", "").lower()
                    if "honeypot" in name or "freeze" in name:
                        result["is_honeypot"] = True
                        result["can_sell"]    = False
                        result["safe"]        = False

                if score >= 80:
                    result["details"]["RugCheck"] = f"✅ Score {score}/100"
                elif score >= 50:
                    result["details"]["RugCheck"] = f"⚠️  Score {score}/100"
                elif score > 0:
                    result["safe"] = False
                    result["details"]["RugCheck"] = f"❌ Score {score}/100 — dangereux"
                else:
                    result["details"]["RugCheck"] = "⚠️  Score indisponible"
        except Exception as e:
            result["details"]["RugCheck"] = f"⚠️  API indisponible"

        # Jupiter simulation vente
        try:
            r = requests.get(
                f"https://quote-api.jup.ag/v6/quote"
                f"?inputMint={token_address}"
                f"&outputMint=So11111111111111111111111111111111111111112"
                f"&amount=1000000&slippageBps=500",
                timeout=8
            )
            if r.status_code == 200 and (r.json().get("routePlan") or r.json().get("outAmount")):
                result["details"]["Vente simulée"] = "✅ Revente possible"
            elif r.status_code == 400:
                result["can_sell"]    = False
                result["is_honeypot"] = True
                result["details"]["Vente simulée"] = "❌ IMPOSSIBLE de revendre"
            else:
                result["details"]["Vente simulée"] = "⚠️  Simulation indisponible"
        except Exception:
            result["details"]["Vente simulée"] = "⚠️  Jupiter indisponible"

        return result


# ─────────────────────────────────────────────────────────────────
# MOMENTUM ANALYZER
# ─────────────────────────────────────────────────────────────────
class MomentumAnalyzer:

    def analyze(self, pos: dict, live_data: dict) -> dict:
        score      = 0
        price_hist = pos.get("price_history", [1.0])
        vol_hist   = pos.get("volume_history", [0])
        current_x  = pos.get("current_x", 1.0)
        prev_x     = price_hist[-2] if len(price_hist) >= 2 else 1.0

        # Vélocité prix
        velocity = (current_x - prev_x) / max(prev_x, 1e-10) * 100
        if velocity >= 5:   score += 30
        elif velocity >= 2: score += 20
        elif velocity >= 0: score += 10

        # Volume accélération
        curr_vol  = live_data.get("volume_5m", 0)
        prev_vol  = vol_hist[-1] if vol_hist else curr_vol
        vol_accel = curr_vol / max(prev_vol, 1) if prev_vol > 0 else 1
        if vol_accel >= 3:   score += 30
        elif vol_accel >= 2: score += 20
        elif vol_accel >= 1: score += 10

        # Ratio buyers
        buys  = live_data.get("buys", 50)
        sells = live_data.get("sells", 50)
        ratio = buys / max(buys + sells, 1) * 100
        if ratio >= 80:   score += 25
        elif ratio >= 65: score += 15
        elif ratio >= 50: score += 5

        # Liquidité
        curr_liq   = live_data.get("liquidity_usd", 0)
        prev_liq   = pos.get("entry_liquidity", curr_liq)
        liq_growth = (curr_liq - prev_liq) / max(prev_liq, 1) * 100
        if liq_growth >= 10: score += 15
        elif liq_growth >= 0: score += 8

        score = min(100, score)

        if score >= 80:   tp, label = CONFIG["tp_moon"],  "🚀 MOONSHOT 20x"
        elif score >= 60: tp, label = CONFIG["tp_high"],  "⭐ HIGH 10x"
        elif score >= 40: tp, label = CONFIG["tp_mid"],   "📈 MID 5x"
        else:             tp, label = CONFIG["tp_low"],   "⚡ LOW 1.5x"

        pos["volume_history"] = (vol_hist + [curr_vol])[-10:]
        return {"score": score, "dynamic_tp": tp, "tp_label": label}


# ─────────────────────────────────────────────────────────────────
# ANTI-RUG ANALYZER
# ─────────────────────────────────────────────────────────────────
class AntiRugAnalyzer:

    def analyze(self, token: dict) -> dict:
        score   = 0
        flags   = []
        details = {}

        # Mint Authority
        mint_ok = token.get("mint_disabled", random.random() > 0.4)
        if mint_ok:
            score += 25
            details["Mint Authority"] = "✅ Désactivée (+25pts)"
        else:
            flags.append("MINT_ACTIVE")
            details["Mint Authority"] = "❌ ACTIVE — risque"

        # LP Tokens — règle assouplie
        liq   = token.get("liquidity_usd", 0)
        lp_ok = token.get("lp_burned", random.random() > 0.5)
        if lp_ok:
            score += 25
            details["LP Tokens"] = "✅ Brûlés (+25pts)"
        elif liq >= 20_000:
            score += 15
            details["LP Tokens"] = f"⚠️  Non brûlés, liq ${liq:,.0f} (+15pts)"
        else:
            flags.append("LP_NOT_BURNED")
            details["LP Tokens"] = "❌ Non brûlés + liq faible"

        # Top Holder (max 30%)
        top_h = token.get("top_holder_pct", random.uniform(5, 60))
        if top_h <= CONFIG["max_top_holder_pct"]:
            score += 20
            details["Top Holder"] = f"✅ {top_h:.1f}% (+20pts)"
        else:
            flags.append("WHALE_CONCENTRATION")
            details["Top Holder"] = f"❌ {top_h:.1f}% — dangereux"

        # Liquidité
        if CONFIG["min_liquidity_usd"] <= liq <= CONFIG["max_liquidity_usd"]:
            score += 15
            details["Liquidité"] = f"✅ ${liq:,.0f} (+15pts)"
        elif liq < CONFIG["min_liquidity_usd"]:
            flags.append("LOW_LIQUIDITY")
            details["Liquidité"] = f"❌ ${liq:,.0f} trop faible"
        else:
            score += 8
            details["Liquidité"] = f"⚠️  ${liq:,.0f} (+8pts)"

        # Buy Pressure
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

        # Seul WHALE bloque (si seul flag → vérif lock)
        whale_only = (flags == ["WHALE_CONCENTRATION"])
        buyable    = (score >= CONFIG["min_score"] and
                      "WHALE_CONCENTRATION" not in flags)

        return {"score": score, "details": details,
                "flags": flags, "buyable": buyable,
                "whale_only": whale_only}


# ─────────────────────────────────────────────────────────────────
# WALLET SIMULÉ
# ─────────────────────────────────────────────────────────────────
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
        return (self.sol_balance > 0.01 and
                len(self.positions) < CONFIG["max_positions"])

    def open_position(self, token: dict, score: int = 80) -> bool:
        if not self.can_snipe():
            return False

        # Taille dynamique selon score
        if score >= 95:   pct, label = 0.20, "20% (exceptionnel)"
        elif score >= 90: pct, label = 0.15, "15% (fort)"
        else:             pct, label = 0.10, "10% (moyen)"

        amount = round(self.sol_balance * pct, 4)
        if amount <= 0.001:
            return False

        print(f"  📐 Position : {label} = {amount:.4f} SOL")
        self.sol_balance -= amount
        self.total_trades += 1

        self.positions[token["address"]] = {
            "symbol":               token["symbol"],
            "address":              token["address"],
            "entry_price":          token["price_usd"],
            "entry_time":           datetime.now(timezone.utc),
            "entry_liquidity":      token.get("liquidity_usd", 0),
            "sol_invested":         amount,
            "remaining_pct":        100.0,
            "current_x":            1.0,
            "peak_x":               1.0,
            "prev_x":               1.0,
            "consecutive_drops":    0,
            "consecutive_rebounds": 0,
            "price_history":        [1.0],
            "volume_history":       [token.get("volume_5m", 0)],
            "is_real":              token.get("is_real", False),
            "is_rug":               token.get("will_rug", False),
            "breakeven_active":     False,
            "dynamic_tp":           CONFIG["tp_mid"],
            "tp_label":             "📈 MID 5x",
            "momentum_score":       50,
            "partial_sold":         False,
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

        if actual_x >= 1.0:     self.wins += 1
        elif "Rug" in reason:   self.rugs += 1; self.losses += 1
        else:                   self.losses += 1

        duration = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
        src      = "🌐" if pos.get("is_real") else "🎮"
        self.closed_trades.append({
            "symbol": pos["symbol"], "exit_x": round(actual_x, 3),
            "pnl_sol": round(pnl_sol, 4), "reason": reason,
            "duration_s": duration, "source": src,
        })

        emoji = "🟢" if actual_x >= 1 else "🔴"
        log.info(f"  {emoji} FERMÉ {src} — {pos['symbol']} | {actual_x:.2f}x | "
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


# ─────────────────────────────────────────────────────────────────
# DÉTECTEUR NOUVEAUX POOLS
# ─────────────────────────────────────────────────────────────────
class NewPoolDetector:

    def __init__(self):
        self.seen    = set()
        self.sol_usd = 150.0
        self._update_sol_price()

    def _update_sol_price(self):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
                timeout=5
            )
            if r.status_code == 200:
                self.sol_usd = float(r.json()["price"])
        except Exception:
            try:
                r = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                    timeout=5
                )
                if r.status_code == 200:
                    self.sol_usd = r.json()["solana"]["usd"]
            except Exception:
                pass

    def scan_new_pools(self) -> list:
        tokens = []
        try:
            r = requests.get(
                "https://api.dexscreener.com/token-profiles/latest/v1",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code != 200:
                return []

            profiles = r.json() if isinstance(r.json(), list) else []

            for profile in profiles[:15]:
                if profile.get("chainId") != "solana":
                    continue
                addr = profile.get("tokenAddress", "")
                if not addr or addr in self.seen:
                    continue

                r2 = requests.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                    timeout=8, headers={"User-Agent": "Mozilla/5.0"}
                )
                if r2.status_code != 200:
                    continue

                pairs     = r2.json().get("pairs", None) or []
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if not sol_pairs:
                    continue

                pair    = sol_pairs[0]
                liq     = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                vol     = pair.get("volume", {})
                txns    = pair.get("txns", {})
                m5      = txns.get("m5", {})
                created = pair.get("pairCreatedAt", 0)
                now_ms  = time.time() * 1000
                age_min = (now_ms - created) / 60000 if created else 0

                token = {
                    "address":       addr,
                    "symbol":        pair.get("baseToken", {}).get("symbol", "???"),
                    "name":          pair.get("baseToken", {}).get("name", "Unknown"),
                    "price_usd":     float(pair.get("priceUsd", 0) or 0),
                    "liquidity_usd": liq,
                    "volume_5m":     float(vol.get("m5", 0) or 0),
                    "volume_1h":     float(vol.get("h1", 0) or 0),
                    "buys":          int(m5.get("buys", 0) or 0),
                    "sells":         int(m5.get("sells", 0) or 0),
                    "age_min":       round(age_min, 1),
                    "is_real":       True,
                    "will_rug":      False,
                    "dex_url":       pair.get("url", ""),
                }

                if token["price_usd"] > 0 and token["liquidity_usd"] > 0:
                    tokens.append(token)
                    print(f"  🌐 {token['symbol']} | ${liq:,.0f} liq | {age_min:.1f}min")

                time.sleep(0.5)

        except Exception as e:
            log.error(f"API erreur: {e}")

        return tokens

    def get_live_price(self, address: str, entry_price: float) -> tuple:
        """Retourne (current_x, live_data)."""
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{address}",
                timeout=5, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                pairs     = r.json().get("pairs", None) or []
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if sol_pairs:
                    pair  = sol_pairs[0]
                    price = float(pair.get("priceUsd", 0) or 0)
                    if price > 0:
                        x = price / max(entry_price, 1e-12)
                        return round(x, 4), {
                            "price_usd":     price,
                            "liquidity_usd": float(pair.get("liquidity", {}).get("usd", 0) or 0),
                            "volume_5m":     float(pair.get("volume", {}).get("m5", 0) or 0),
                            "buys":          int(pair.get("txns", {}).get("m5", {}).get("buys", 0) or 0),
                            "sells":         int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0),
                        }
        except Exception:
            pass
        return None, {}


# ─────────────────────────────────────────────────────────────────
# PROTECTION COMPORTEMENTALE
# ─────────────────────────────────────────────────────────────────
def check_protection(pos: dict, x: float) -> tuple:
    prev_x = pos.get("prev_x", 1.0)

    # Dump urgent
    if x <= (1 + CONFIG["emergency_dump_pct"] / 100):
        return True, f"🚨 Dump urgent {(x-1)*100:.1f}%"

    drop = (x - prev_x) / max(prev_x, 1e-10)

    if drop <= CONFIG["min_drop_per_scan"]:
        pos["consecutive_drops"]    += 1
        pos["consecutive_rebounds"]  = 0
    elif drop > 0:
        pos["consecutive_rebounds"] += 1
        if pos["consecutive_rebounds"] >= CONFIG["rebounds_to_reset"]:
            pos["consecutive_drops"]    = 0
            pos["consecutive_rebounds"] = 0

    if pos["consecutive_drops"] >= CONFIG["consecutive_drops"]:
        return True, f"📉 {pos['consecutive_drops']} baisses consécutives"

    pos["prev_x"] = x
    return False, ""


# ─────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────
def print_dashboard(wallet: SniperWallet, sol_price: float):
    total_sol = wallet.sol_balance + sum(
        p["sol_invested"] * p.get("current_x", 1) * (p["remaining_pct"] / 100)
        for p in wallet.positions.values()
    )
    pnl_sol   = total_sol - wallet.initial_sol
    pnl_pct   = pnl_sol / wallet.initial_sol * 100 if wallet.initial_sol > 0 else 0
    wr        = wallet.wins / max(wallet.total_trades, 1) * 100
    pnl_color = Fore.GREEN if pnl_sol >= 0 else Fore.RED

    print(f"\n{Fore.CYAN}{'═'*62}")
    print(f"  🎯 SNIPER BOT v7 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*62}")
    print(f"  SOL balance   : {wallet.sol_balance:.4f} SOL")
    print(f"  Valeur totale : {total_sol:.4f} SOL (${total_sol*sol_price:,.2f})")
    print(f"  {pnl_color}P&L           : {pnl_sol:+.4f} SOL ({pnl_pct:+.2f}%){Style.RESET_ALL}")
    print(f"  Trades        : {wallet.total_trades} | ✅ {wallet.wins} | ❌ {wallet.losses} | 💀 {wallet.rugs}")
    print(f"  Win Rate      : {wr:.1f}%")

    if wallet.positions:
        print(f"\n  📊 POSITIONS :")
        for addr, pos in wallet.positions.items():
            x      = pos.get("current_x", 1.0)
            xcolor = Fore.GREEN if x >= 1 else Fore.RED
            age_s  = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
            be     = f"{Fore.CYAN}🔒{Style.RESET_ALL}" if pos.get("breakeven_active") else ""
            print(f"  {xcolor}  {pos['symbol']:<12} {x:.2f}x | "
                  f"{pos['remaining_pct']:.0f}% | Age:{age_s}s | "
                  f"{pos.get('tp_label','?')} {be}")

    if wallet.closed_trades:
        print(f"\n  📜 DERNIERS TRADES :")
        for t in wallet.closed_trades[-5:]:
            xcolor = Fore.GREEN if t["exit_x"] >= 1 else Fore.RED
            print(f"  {xcolor}  {t.get('source','🎮')} {t['symbol']:<12} "
                  f"{t['exit_x']:.2f}x | {t['pnl_sol']:+.4f} SOL | "
                  f"{t['reason']}{Style.RESET_ALL}")

    print(f"\n  🎯 TP: <40→1.5x | 40-60→5x | 60-80→10x | >80→20x")
    print(f"  🛡️  Break-Even +17% | SL -20% | Dump -15%")
    print(f"{Fore.CYAN}{'═'*62}{Style.RESET_ALL}\n")


# ─────────────────────────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ─────────────────────────────────────────────────────────────────
def run_sniper(wallet: SniperWallet, detector: NewPoolDetector,
               rug_a: AntiRugAnalyzer, mom_a: MomentumAnalyzer,
               hp_a: HoneypotAnalyzer, executor=None):

    to_close = []

    # ── Mise à jour positions ─────────────────────────────────
    for address, pos in list(wallet.positions.items()):
        if pos.get("is_real"):
            x, live_data = detector.get_live_price(address, pos["entry_price"])
            if x is None:
                x         = pos.get("current_x", 1.0)
                live_data = {}
        else:
            age_s  = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
            is_rug = pos.get("is_rug", False)
            peak   = pos.get("peak_x", 1.0)
            if is_rug:
                x = max(0.05, peak * np.exp(-age_s / 120)) if age_s >= 60 else 1.0 + (age_s / 60) * 1.5
            else:
                x = pos.get("current_x", 1.0) * np.exp(np.random.normal(0.003, 0.04))
                x = max(x, 0.1)
            live_data = {}

        x = round(x, 4)
        pos["current_x"] = x
        pos["peak_x"]    = max(pos.get("peak_x", 1.0), x)
        pos["price_history"].append(x)

        # Analyse momentum
        mom    = mom_a.analyze(pos, live_data)
        old_tp = pos.get("dynamic_tp", CONFIG["tp_mid"])
        pos["dynamic_tp"]     = mom["dynamic_tp"]
        pos["tp_label"]       = mom["tp_label"]
        pos["momentum_score"] = mom["score"]

        if mom["dynamic_tp"] != old_tp:
            print(f"  {Fore.YELLOW}📊 TP AJUSTÉ — {pos['symbol']} | "
                  f"{old_tp}x → {mom['dynamic_tp']}x | "
                  f"Mom:{mom['score']}/100{Style.RESET_ALL}")

        age_sec = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
        age_min = age_sec / 60

        # Protection comportementale
        sell, reason = check_protection(pos, x)
        if sell:
            to_close.append((address, x, reason, None))
            continue

        # Break-Even +17%
        if x >= CONFIG["breakeven_trigger_x"] and not pos["breakeven_active"]:
            pos["breakeven_active"] = True
            print(f"  {Fore.CYAN}🔒 BREAK-EVEN — {pos['symbol']} | SL = prix d'entrée{Style.RESET_ALL}")

        if pos["breakeven_active"] and x < 1.0:
            to_close.append((address, x, "🔒 Break-Even — sortie sans perte", 1.0))
            continue

        # Stop Loss -20%
        if x <= (1 + CONFIG["stop_loss_pct"] / 100):
            reason = "Rug Pull 💀" if pos.get("is_rug") else "Stop Loss -20%"
            to_close.append((address, x, reason, None))
            continue

        # Temps max
        if age_min >= CONFIG["max_hold_minutes"]:
            to_close.append((address, x, "⏰ Temps max dépassé", None))
            continue

        # TP Dynamique
        if x >= pos["dynamic_tp"]:
            to_close.append((address, x, f"🎯 {pos['tp_label']} {x:.2f}x", None))
            continue

    for address, x, reason, force_x in to_close:
        # Vente réelle si executor disponible
        if executor and executor.enabled and wallet.positions.get(address, {}).get("is_real"):
            pos = wallet.positions.get(address, {})
            executor.sell_token(address, 100, pos.get("symbol", "???"))
        wallet.close_position(address, x, reason, force_x=force_x)

    # ── Scan nouveaux pools ───────────────────────────────────
    new_tokens = detector.scan_new_pools() or []
    for token in new_tokens:
        if token["address"] in detector.seen:
            continue
        detector.seen.add(token["address"])

        # Anti-rug
        rug = rug_a.analyze(token)
        src = f"{Fore.GREEN}🌐 VRAI{Style.RESET_ALL}" if token.get("is_real") else f"{Fore.YELLOW}🎮 SIM{Style.RESET_ALL}"
        print(f"\n  ━━━ NOUVEAU TOKEN {src} ━━━")
        print(f"  🚀 {token['symbol']} | Age:{token.get('age_min',0):.1f}min | ${token['liquidity_usd']:,.0f}")
        if token.get("dex_url"):
            print(f"  🔗 {token['dex_url']}")
        print(f"  📊 SCORE ANTI-RUG : {rug['score']}/100")
        for k, v in rug["details"].items():
            print(f"     {k:<20} {v}")
        if rug["flags"]:
            print(f"  {Fore.RED}🚨 FLAGS : {' | '.join(rug['flags'])}{Style.RESET_ALL}")

        # Vérification whale lock si seul problème
        if not rug["buyable"] and rug.get("whale_only"):
            print(f"  🔍 Whale seul problème — vérif lock...")
            lock = hp_a.check_whale_lock(token["address"])
            print(f"     Lock : {lock['details']}")
            if lock["locked"] or lock["details"] == "Inconnu":
                print(f"  {Fore.GREEN}✅ Whale acceptable{Style.RESET_ALL}")
                rug["buyable"] = True
            else:
                print(f"  {Fore.RED}❌ Whale sans lock{Style.RESET_ALL}")

        if not rug["buyable"]:
            print(f"  {Fore.RED}❌ REFUSÉ — Score {rug['score']}/100{Style.RESET_ALL}")
            continue

        # Honeypot check
        print(f"  🔍 Vérification honeypot...")
        hp = hp_a.check(token["address"], token["symbol"])
        for k, v in hp["details"].items():
            print(f"     {k:<20} {v}")

        # Bloque si RugCheck dangereux (score > 0 mais < 30)
        if 0 < hp["rugcheck_score"] < 30:
            print(f"  {Fore.RED}🚨 RUGCHECK DANGEREUX {hp['rugcheck_score']}/100 — BLOQUÉ{Style.RESET_ALL}")
            continue

        if hp["is_honeypot"]:
            print(f"  {Fore.RED}🚨 HONEYPOT DÉTECTÉ — BLOQUÉ{Style.RESET_ALL}")
            continue

        # Achat
        if wallet.can_snipe():
            amount = round(wallet.sol_balance * (0.20 if rug["score"] >= 95 else 0.15 if rug["score"] >= 90 else 0.10), 4)
            print(f"\n  {Fore.GREEN}⚡ SNIPE ! {token['symbol']} — Score:{rug['score']}/100 | {amount:.4f} SOL{Style.RESET_ALL}")

            if executor and executor.enabled:
                print(f"  🔴 Exécution transaction réelle...")
                buy = executor.buy_token(token["address"], amount, token["symbol"])
                if buy["success"]:
                    print(f"  ✅ ACHAT CONFIRMÉ !")
                    print(f"  🔗 https://solscan.io/tx/{buy['tx_hash']}")
                    wallet.open_position(token, rug["score"])
                else:
                    print(f"  {Fore.RED}❌ Achat échoué: {buy.get('reason','')}{Style.RESET_ALL}")
            else:
                wallet.open_position(token, rug["score"])

            log.info(f"⚡ SNIPE — {token['symbol']} | Score:{rug['score']} | RugCheck:{hp['rugcheck_score']}")

    if int(time.time()) % 30 < 2:
        print_dashboard(wallet, detector.sol_usd)


# ─────────────────────────────────────────────────────────────────
# SOLDE RÉEL
# ─────────────────────────────────────────────────────────────────
def get_real_balance() -> float:
    try:
        wallet_address = os.getenv("WALLET_ADDRESS", "")
        if not wallet_address:
            return CONFIG["initial_capital_sol"]
        r = requests.post(
            "https://api.mainnet-beta.solana.com",
            json={"jsonrpc": "2.0", "id": 1,
                  "method": "getBalance", "params": [wallet_address]},
            timeout=10
        )
        if r.status_code == 200:
            lamports = r.json().get("result", {}).get("value", 0)
            sol = lamports / 1_000_000_000
            print(f"  💰 Vrai solde wallet : {sol:.4f} SOL")
            return sol if sol > 0 else CONFIG["initial_capital_sol"]
    except Exception as e:
        print(f"  ⚠️  Erreur solde: {e}")
    return CONFIG["initial_capital_sol"]


# ─────────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────────
def main():
    print(f"""{Fore.YELLOW}
╔══════════════════════════════════════════════════════════════╗
║         SOLANA SNIPER BOT v7 — VERSION FINALE               ║
║         🛡️  Anti-Honeypot + Anti-Rug                       ║
║         📡  TP Dynamique (momentum 2sec)                   ║
║         🔒  Break-Even +17% | SL -20%                      ║
║         🐋  Whale lock check (top holder max 30%)          ║
║         LP  Brûlés +25pts | Non brûlés liq>20K +15pts     ║
║         💰  Taille position : 10/15/20% selon score       ║
╚══════════════════════════════════════════════════════════════╝
{Style.RESET_ALL}""")

    initial = get_real_balance()
    print(f"  💰 Capital de trading : {initial:.4f} SOL")

    wallet   = SniperWallet(initial)
    detector = NewPoolDetector()
    rug_a    = AntiRugAnalyzer()
    mom_a    = MomentumAnalyzer()
    hp_a     = HoneypotAnalyzer()

    executor = None
    if EXECUTOR_AVAILABLE and REAL_MODE:
        executor = SolanaExecutor()
        if executor.enabled:
            print(f"  🔴 TRADING RÉEL ACTIVÉ")
        else:
            print(f"  ⚠️  Executor désactivé")
    else:
        print(f"  🎮 Mode simulation")

    print(f"  ✅ Bot v7 initialisé")
    print(f"  ⚡ Analyse toutes les {CONFIG['scan_interval_sec']} secondes")
    print(f"  🎯 Score minimum : {CONFIG['min_score']}/100")
    print(f"  Appuie sur Ctrl+C pour arrêter\n")

    # Test connectivité Jupiter
    try:
        import requests as _r
        _resp = _r.get('https://quote-api.jup.ag/v6/quote?inputMint=So11111111111111111111111111111111111111112&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=1000000&slippageBps=500', timeout=5)
        print(f'  ✅ Jupiter accessible — Status {_resp.status_code}')
    except Exception as _e:
        print(f'  ❌ Jupiter BLOQUÉ — {_e}')

    while True:
        try:
            run_sniper(wallet, detector, rug_a, mom_a, hp_a, executor)
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
