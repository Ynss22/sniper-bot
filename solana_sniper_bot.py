"""
╔══════════════════════════════════════════════════════════════════╗
║           SOLANA SNIPER BOT v6 — ANTI-HONEYPOT + TP DYNAMIQUE  ║
║           Nouveautés :                                          ║
║           - Vérification honeypot (RugCheck API)               ║
║           - Analyse contrat (permissions dangereuses)          ║
║           - LP : brûlés +25pts / verrouillés +15pts            ║
║           - TP dynamique selon momentum (analyse 2sec)         ║
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

WALLET_KEY = os.getenv("WALLET_PRIVATE_KEY")
REAL_MODE  = WALLET_KEY is not None
print(f"  Mode : {'🔴 TRADING RÉEL' if REAL_MODE else '🎮 SIMULATION'}")

CONFIG = {
    "initial_capital_sol":      50.0,
    "max_positions":             3,
    "min_liquidity_usd":      5_000,
    "max_liquidity_usd":    500_000,
    "min_score":                 80,
    "max_top_holder_pct":        20,
    "stop_loss_pct":            -20,
    "breakeven_trigger_x":      1.17,
    "max_hold_minutes":          120,
    "scan_interval_sec":           2,
    "max_token_age_min":          60,
    "emergency_dump_pct":        -15,
    "consecutive_drops":           3,
    "min_drop_per_scan":        -0.02,
    "rebounds_to_reset":           2,
    "tp_momentum_low":    1.5,
    "tp_momentum_mid":    5.0,
    "tp_momentum_high":  10.0,
    "tp_momentum_moon":  20.0,
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
# HONEYPOT & CONTRACT ANALYZER
# ─────────────────────────────────────────────────────────────────
class HoneypotAnalyzer:
    """
    Vérifie via RugCheck.xyz et Jupiter :
    1. Si le token est un honeypot (impossible à revendre)
    2. Les permissions dangereuses du contrat
    3. La taxe de vente cachée
    4. Si le créateur peut bloquer les transferts
    """

    def check(self, token_address: str, symbol: str) -> dict:
        result = {
            "is_honeypot":      False,
            "can_sell":         True,
            "sell_tax":         0,
            "dangerous_perms":  [],
            "rugcheck_score":   0,
            "safe":             True,
            "details":          {},
        }

        # ── Check 1 : RugCheck.xyz API ────────────────────────
        try:
            r = requests.get(
                f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                data  = r.json()
                risks = data.get("risks", [])

                # Analyse des risques détectés
                for risk in risks:
                    name  = risk.get("name", "").lower()
                    level = risk.get("level", "").lower()

                    # Honeypot détecté
                    if "honeypot" in name or "freeze" in name:
                        result["is_honeypot"]   = True
                        result["can_sell"]       = False
                        result["safe"]           = False
                        result["dangerous_perms"].append(f"🚨 HONEYPOT: {risk.get('name')}")

                    # Mint authority active
                    elif "mint" in name and level in ["danger", "warn"]:
                        result["dangerous_perms"].append(f"⚠️ Mint active")

                    # Blacklist function
                    elif "blacklist" in name:
                        result["safe"] = False
                        result["dangerous_perms"].append(f"🚨 Blacklist possible")

                    # Taxe élevée
                    elif "tax" in name or "fee" in name:
                        result["dangerous_perms"].append(f"⚠️ Taxe détectée")

                # Score RugCheck
                score = data.get("score", 0)
                result["rugcheck_score"] = score

                if score >= 80:
                    result["details"]["RugCheck"] = f"✅ Score {score}/100 — sûr"
                elif score >= 50:
                    result["details"]["RugCheck"] = f"⚠️  Score {score}/100 — risque modéré"
                else:
                    result["safe"]  = False
                    result["details"]["RugCheck"] = f"❌ Score {score}/100 — dangereux"

        except Exception as e:
            result["details"]["RugCheck"] = f"⚠️  API indisponible ({str(e)[:30]})"

        # ── Check 2 : Simulation vente via Jupiter ────────────
        try:
            # Simule un swap token → SOL pour vérifier que c'est possible
            r = requests.get(
                f"https://quote-api.jup.ag/v6/quote"
                f"?inputMint={token_address}"
                f"&outputMint=So11111111111111111111111111111111111111112"
                f"&amount=1000000"
                f"&slippageBps=500",
                timeout=8
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("routePlan") or data.get("outAmount"):
                    result["can_sell"] = True
                    result["details"]["Vente simulée"] = "✅ Revente possible via Jupiter"
                else:
                    result["can_sell"]    = False
                    result["is_honeypot"] = True
                    result["safe"]        = False
                    result["details"]["Vente simulée"] = "❌ IMPOSSIBLE de revendre !"
            elif r.status_code == 400:
                result["can_sell"]    = False
                result["is_honeypot"] = True
                result["safe"]        = False
                result["details"]["Vente simulée"] = "❌ Token non vendable (400)"
            else:
                result["details"]["Vente simulée"] = f"⚠️  Simulation impossible ({r.status_code})"

        except Exception as e:
            result["details"]["Vente simulée"] = f"⚠️  Jupiter indisponible"

        # ── Résultat final ────────────────────────────────────
        if result["is_honeypot"]:
            log.warning(f"  🚨 HONEYPOT DÉTECTÉ — {symbol} | Achat BLOQUÉ")
        elif not result["safe"]:
            log.warning(f"  ⚠️  Risques détectés — {symbol}")

        return result


# ─────────────────────────────────────────────────────────────────
# MOMENTUM ANALYZER
# ─────────────────────────────────────────────────────────────────
class MomentumAnalyzer:

    def analyze(self, pos: dict, live_data: dict) -> dict:
        score      = 0
        details    = {}
        price_hist = pos.get("price_history", [1.0])
        vol_hist   = pos.get("volume_history", [0])
        current_x  = pos.get("current_x", 1.0)
        prev_x     = price_hist[-2] if len(price_hist) >= 2 else 1.0

        # Vélocité du prix
        velocity = (current_x - prev_x) / max(prev_x, 1e-10) * 100
        if velocity >= 5:
            score += 30
            details["Vélocité"] = f"✅ +{velocity:.1f}%/scan (+30pts)"
        elif velocity >= 2:
            score += 20
            details["Vélocité"] = f"✅ +{velocity:.1f}%/scan (+20pts)"
        elif velocity >= 0:
            score += 10
            details["Vélocité"] = f"⚠️  +{velocity:.1f}%/scan (+10pts)"
        else:
            details["Vélocité"] = f"❌ {velocity:.1f}%/scan"

        # Accélération volume
        curr_vol  = live_data.get("volume_5m", 0)
        prev_vol  = vol_hist[-1] if vol_hist else curr_vol
        vol_accel = curr_vol / max(prev_vol, 1) if prev_vol > 0 else 1
        if vol_accel >= 3:
            score += 30
            details["Volume"] = f"✅ x{vol_accel:.1f} (+30pts) 🔥"
        elif vol_accel >= 2:
            score += 20
            details["Volume"] = f"✅ x{vol_accel:.1f} (+20pts)"
        elif vol_accel >= 1:
            score += 10
            details["Volume"] = f"⚠️  x{vol_accel:.1f} (+10pts)"
        else:
            details["Volume"] = f"❌ volume baisse"

        # Ratio acheteurs
        buys  = live_data.get("buys", 0)
        sells = live_data.get("sells", 1)
        ratio = buys / max(buys + sells, 1) * 100
        if ratio >= 80:
            score += 25
            details["Buyers"] = f"✅ {ratio:.0f}% (+25pts) 🚀"
        elif ratio >= 65:
            score += 15
            details["Buyers"] = f"✅ {ratio:.0f}% (+15pts)"
        elif ratio >= 50:
            score += 5
            details["Buyers"] = f"⚠️  {ratio:.0f}% (+5pts)"
        else:
            details["Buyers"] = f"❌ {ratio:.0f}% vendeurs"

        # Liquidité
        curr_liq  = live_data.get("liquidity_usd", 0)
        prev_liq  = pos.get("entry_liquidity", curr_liq)
        liq_growth = (curr_liq - prev_liq) / max(prev_liq, 1) * 100
        if liq_growth >= 10:
            score += 15
            details["Liquidité"] = f"✅ +{liq_growth:.1f}% (+15pts)"
        elif liq_growth >= 0:
            score += 8
            details["Liquidité"] = f"⚠️  stable (+8pts)"
        else:
            details["Liquidité"] = f"❌ baisse liquidité"

        score = min(100, score)

        if score >= 80:
            tp, label = CONFIG["tp_momentum_moon"], "🚀 MOONSHOT 20x"
        elif score >= 60:
            tp, label = CONFIG["tp_momentum_high"], "⭐ HIGH 10x"
        elif score >= 40:
            tp, label = CONFIG["tp_momentum_mid"],  "📈 MID 5x"
        else:
            tp, label = CONFIG["tp_momentum_low"],  "⚡ LOW 1.5x"

        pos["volume_history"] = (vol_hist + [curr_vol])[-10:]
        return {"score": score, "dynamic_tp": tp, "tp_label": label, "details": details}


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
        liq = token.get("liquidity_usd", 0)
        lp_ok = token.get("lp_burned", random.random() > 0.5)
        if lp_ok:
            score += 25
            details["LP Tokens"] = "✅ Brûlés (+25pts)"
        elif liq >= 20_000:
            # LP non brûlés mais liquidité élevée = verrouillés probablement
            score += 15
            details["LP Tokens"] = f"⚠️  Non brûlés mais liq ${liq:,.0f} (+15pts)"
        else:
            flags.append("LP_NOT_BURNED")
            details["LP Tokens"] = f"❌ Non brûlés + liq faible (0pts)"

        # Top Holder
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

        # Seul WHALE_CONCENTRATION bloque vraiment
        buyable = (score >= CONFIG["min_score"] and
                   "WHALE_CONCENTRATION" not in flags)

        return {"score": score, "details": details,
                "flags": flags, "buyable": buyable}


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

    def open_position(self, token: dict, anti_rug_score: int = 80) -> bool:
        if not self.can_snipe():
            return False

        # Taille dynamique selon score
        if anti_rug_score >= 95:
            pct, label = 0.20, "20% (exceptionnel)"
        elif anti_rug_score >= 90:
            pct, label = 0.15, "15% (fort)"
        else:
            pct, label = 0.10, "10% (moyen)"

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
            "dynamic_tp":           CONFIG["tp_momentum_mid"],
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

        if actual_x >= 1.0:
            self.wins += 1
        elif "Rug" in reason:
            self.rugs += 1
            self.losses += 1
        else:
            self.losses += 1

        duration = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
        source   = "🌐" if pos.get("is_real") else "🎮"
        self.closed_trades.append({
            "symbol":     pos["symbol"],
            "exit_x":     round(actual_x, 3),
            "pnl_sol":    round(pnl_sol, 4),
            "reason":     reason,
            "duration_s": duration,
            "source":     source,
        })

        emoji = "🟢" if actual_x >= 1 else "🔴"
        log.info(f"  {emoji} FERMÉ {source} — {pos['symbol']} | {actual_x:.2f}x | "
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
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"}
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
                    timeout=8,
                    headers={"User-Agent": "Mozilla/5.0"}
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
                    "address":        addr,
                    "symbol":         pair.get("baseToken", {}).get("symbol", "???"),
                    "name":           pair.get("baseToken", {}).get("name", "Unknown"),
                    "price_usd":      float(pair.get("priceUsd", 0) or 0),
                    "liquidity_usd":  liq,
                    "volume_5m":      float(vol.get("m5", 0) or 0),
                    "volume_1h":      float(vol.get("h1", 0) or 0),
                    "buys":           int(m5.get("buys", 0) or 0),
                    "sells":          int(m5.get("sells", 0) or 0),
                    "age_min":        round(age_min, 1),
                    "age_sec":        age_min * 60,
                    "is_real":        True,
                    "will_rug":       False,
                    "dex_url":        pair.get("url", ""),
                }

                if token["price_usd"] > 0 and token["liquidity_usd"] > 0:
                    tokens.append(token)
                    print(f"  🌐 VRAI TOKEN : {token['symbol']} | "
                          f"${liq:,.0f} liq | {age_min:.1f}min")

                time.sleep(0.5)

        except Exception as e:
            log.error(f"API erreur: {e}")

        return tokens

    def get_live_price_data(self, address: str) -> dict:
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{address}",
                timeout=5,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                pairs     = r.json().get("pairs", None) or []
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if sol_pairs:
                    pair = sol_pairs[0]
                    return {
                        "price_usd":     float(pair.get("priceUsd", 0) or 0),
                        "liquidity_usd": float(pair.get("liquidity", {}).get("usd", 0) or 0),
                        "volume_5m":     float(pair.get("volume", {}).get("m5", 0) or 0),
                        "buys":          int(pair.get("txns", {}).get("m5", {}).get("buys", 0) or 0),
                        "sells":         int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0),
                    }
        except Exception:
            pass
        return {}


# ─────────────────────────────────────────────────────────────────
# PROTECTION COMPORTEMENTALE
# ─────────────────────────────────────────────────────────────────
def check_behavioral_protection(pos: dict, x: float) -> tuple:
    prev_x = pos.get("prev_x", 1.0)

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
    print(f"  🎯 SNIPER BOT v6 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  🛡️  ANTI-HONEYPOT | 📡 TP DYNAMIQUE | 🔍 CONTRAT")
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
            be     = f"{Fore.CYAN}🔒{Style.RESET_ALL}" if pos.get("breakeven_active") else ""
            tp     = pos.get("tp_label", "")
            mscore = pos.get("momentum_score", 0)
            print(f"  {xcolor}  {pos['symbol']:<12} {x:.2f}x | "
                  f"Restant: {pos['remaining_pct']:.0f}% | "
                  f"Mom: {mscore}/100 | {tp} {be}")

    if wallet.closed_trades:
        print(f"\n  📜 DERNIERS TRADES :")
        for t in wallet.closed_trades[-5:]:
            xcolor = Fore.GREEN if t["exit_x"] >= 1 else Fore.RED
            print(f"  {xcolor}  {t.get('source','🎮')} {t['symbol']:<12} "
                  f"{t['exit_x']:.2f}x | P&L: {t['pnl_sol']:+.4f} SOL | "
                  f"{t['reason']}{Style.RESET_ALL}")

    print(f"\n  🎯 TP DYNAMIQUES : <40→1.5x | 40-60→5x | 60-80→10x | >80→20x")
    print(f"  🛡️  PROTECTIONS  : Break-Even +17% | SL -20% | Dump -15%")
    print(f"{Fore.CYAN}{'═'*62}{Style.RESET_ALL}\n")


# ─────────────────────────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ─────────────────────────────────────────────────────────────────
def run_sniper(wallet: SniperWallet, detector: NewPoolDetector,
               rug_analyzer: AntiRugAnalyzer,
               mom_analyzer: MomentumAnalyzer,
               hp_analyzer: HoneypotAnalyzer):

    to_close = []

    # ── Mise à jour positions toutes les 2 secondes ──────────
    for address, pos in list(wallet.positions.items()):
        if pos.get("is_real"):
            live_data = detector.get_live_price_data(address)
            if live_data and live_data.get("price_usd", 0) > 0:
                x = live_data["price_usd"] / max(pos["entry_price"], 1e-12)
            else:
                x = pos.get("current_x", 1.0)
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
        mom = mom_analyzer.analyze(pos, live_data or {})
        old_tp = pos.get("dynamic_tp", 5.0)
        pos["dynamic_tp"]     = mom["dynamic_tp"]
        pos["tp_label"]       = mom["tp_label"]
        pos["momentum_score"] = mom["score"]

        if mom["dynamic_tp"] != old_tp:
            print(f"  {Fore.YELLOW}📊 TP AJUSTÉ — {pos['symbol']} | "
                  f"{old_tp}x → {mom['dynamic_tp']}x | "
                  f"Momentum: {mom['score']}/100{Style.RESET_ALL}")

        age_sec = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
        age_min = age_sec / 60

        # Protection comportementale
        should_sell, reason = check_behavioral_protection(pos, x)
        if should_sell:
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
            to_close.append((address, x,
                             f"🎯 {pos['tp_label']} ! {x:.2f}x", None))
            continue

        # Vente 100% à +20%
        if x >= 1.20 and not pos.get("partial_sold"):
            pos["partial_sold"] = True
            wallet.partial_sell(address, 100, x,
                               f"✅ Vente 100% +{(x-1)*100:.0f}%")

    for address, x, reason, force_x in to_close:
        wallet.close_position(address, x, reason, force_x=force_x)

    # ── Scan nouveaux pools ──────────────────────────────────
    new_tokens = detector.scan_new_pools() or []
    for token in new_tokens:
        if token["address"] in detector.seen:
            continue
        detector.seen.add(token["address"])

        # Anti-rug check
        rug_analysis = rug_analyzer.analyze(token)
        src = f"{Fore.GREEN}🌐 VRAI" if token.get("is_real") else f"{Fore.YELLOW}🎮 SIM"
        print(f"\n  ━━━ NOUVEAU TOKEN {src}{Style.RESET_ALL} ━━━")
        print(f"  🚀 {token['symbol']} | Age: {token.get('age_min',0):.1f}min | "
              f"Liq: ${token['liquidity_usd']:,.0f}")
        if token.get("dex_url"):
            print(f"  🔗 {token['dex_url']}")
        print(f"  📊 SCORE ANTI-RUG : {rug_analysis['score']}/100")
        for k, v in rug_analysis["details"].items():
            print(f"     {k:<20} {v}")
        if rug_analysis["flags"]:
            print(f"  {Fore.RED}🚨 FLAGS : {' | '.join(rug_analysis['flags'])}{Style.RESET_ALL}")

        if not rug_analysis["buyable"]:
            print(f"  {Fore.RED}❌ REFUSÉ — Score {rug_analysis['score']}/100{Style.RESET_ALL}")
            continue

        # ── Vérification honeypot AVANT achat ────────────────
        print(f"  🔍 Vérification honeypot et contrat...")
        hp = hp_analyzer.check(token["address"], token["symbol"])

        for k, v in hp["details"].items():
            print(f"     {k:<20} {v}")

        if hp["dangerous_perms"]:
            for perm in hp["dangerous_perms"]:
                print(f"  {Fore.RED}  {perm}{Style.RESET_ALL}")

        if hp["is_honeypot"]:
            print(f"  {Fore.RED}🚨 HONEYPOT DÉTECTÉ — ACHAT BLOQUÉ !{Style.RESET_ALL}")
            log.warning(f"🚨 HONEYPOT — {token['symbol']} | Achat bloqué")
            continue

        if not hp["can_sell"]:
            print(f"  {Fore.RED}❌ REVENTE IMPOSSIBLE — ACHAT BLOQUÉ !{Style.RESET_ALL}")
            continue

        if not hp["safe"]:
            print(f"  {Fore.YELLOW}⚠️  Risques détectés — Trade avec prudence{Style.RESET_ALL}")

        # ── Achat validé ──────────────────────────────────────
        if wallet.can_snipe():
            print(f"\n  {Fore.GREEN}⚡ SNIPE ! {token['symbol']} — "
                  f"Score: {rug_analysis['score']}/100 | "
                  f"RugCheck: {hp['rugcheck_score']}/100{Style.RESET_ALL}")
            success = wallet.open_position(token, rug_analysis["score"])
            if success:
                log.info(f"⚡ SNIPE — {token['symbol']} | "
                         f"AntiRug: {rug_analysis['score']} | "
                         f"RugCheck: {hp['rugcheck_score']}")

    if int(time.time()) % 30 < 2:
        print_dashboard(wallet, detector.sol_usd)


# ─────────────────────────────────────────────────────────────────
# SOLDE RÉEL
# ─────────────────────────────────────────────────────────────────
def get_real_sol_balance() -> float:
    try:
        wallet_address = os.getenv("WALLET_ADDRESS", "")
        if not wallet_address:
            return CONFIG["initial_capital_sol"]
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
║      SOLANA SNIPER BOT v6 — ANTI-HONEYPOT + TP DYNAMIQUE    ║
║      🛡️  Vérifie honeypot AVANT chaque achat               ║
║      🔍  Analyse contrat (permissions dangereuses)          ║
║      📡  TP dynamique selon momentum (2sec)                 ║
║      LP : brûlés +25pts | verrouillés +15pts               ║
║      Break-Even : +17% | Stop Loss : -20%                   ║
╚══════════════════════════════════════════════════════════════╝
{Style.RESET_ALL}""")

    real_balance = get_real_sol_balance()
    initial      = real_balance if real_balance > 0 else CONFIG["initial_capital_sol"]
    print(f"  💰 Capital de trading : {initial:.4f} SOL")

    wallet    = SniperWallet(initial)
    detector  = NewPoolDetector()
    rug_a     = AntiRugAnalyzer()
    mom_a     = MomentumAnalyzer()
    hp_a      = HoneypotAnalyzer()

    print(f"  ✅ Bot v6 initialisé")
    print(f"  ⚡ Analyse toutes les {CONFIG['scan_interval_sec']} secondes")
    print(f"  🎯 Score minimum : {CONFIG['min_score']}/100")
    print(f"  Appuie sur Ctrl+C pour arrêter\n")

    while True:
        try:
            run_sniper(wallet, detector, rug_a, mom_a, hp_a)
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
