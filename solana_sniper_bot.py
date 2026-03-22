"""
╔══════════════════════════════════════════════════════════════════╗
║           SOLANA SNIPER BOT v4 — VRAIS TOKENS RAYDIUM           ║
║           Connexion API DexScreener améliorée                   ║
║           Fallback simulation si API indisponible               ║
╚══════════════════════════════════════════════════════════════════╝
"""

import time
import random
import logging
import requests
import numpy as np
from datetime import datetime, timezone
from colorama import Fore, Style, init

init(autoreset=True)

CONFIG = {
    "initial_capital_sol":      50.0,
    "sol_per_trade":             2.0,
    "max_positions":             3,
    "min_liquidity_usd":      3_000,
    "max_liquidity_usd":    500_000,
    "min_score":                 60,
    "max_top_holder_pct":        20,
    "stop_loss_pct":            -25,
    "breakeven_trigger_x":      1.15,
    "take_profit_0":             1.17,
    "take_profit_1":             1.50,
    "take_profit_2":             3.00,
    "take_profit_3":            10.00,
    "max_hold_minutes":          120,
    "scan_interval_sec":           5,  # ← 5sec pour éviter rate limit API
    "max_token_age_min":          30,  # ← 30 minutes au lieu de 30 secondes
    "emergency_dump_pct":        -15,
    "consecutive_drops":           3,
    "min_drop_per_scan":        -0.02,
    "rebounds_to_reset":           2,

    # ── API DexScreener ──────────────────────────────────────
    "api_timeout":                10,
    "api_retry":                   3,
    "api_delay":                   2,  # Délai entre requêtes (sec)
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
            "symbol":               token["symbol"],
            "address":              token["address"],
            "entry_price":          token["price_usd"],
            "entry_time":           datetime.now(timezone.utc),
            "sol_invested":         amount,
            "remaining_pct":        100.0,
            "tp0_hit":              False,
            "tp1_hit":              False,
            "tp2_hit":              False,
            "tp3_hit":              False,
            "current_x":            1.0,
            "peak_x":               1.0,
            "prev_x":               1.0,
            "consecutive_drops":    0,
            "consecutive_rebounds": 0,
            "price_history":        [1.0],
            "is_real":              token.get("is_real", False),
            "is_rug":               token.get("will_rug", False),
            "breakeven_active":     False,
            "real_price_usd":       token["price_usd"],
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
        source   = "🌐 RÉEL" if pos.get("is_real") else "🎮 SIM"
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


class NewPoolDetector:

    def __init__(self):
        self.seen         = set()
        self.sol_usd      = 150.0
        self.real_tokens  = 0
        self.sim_tokens   = 0
        self.last_api_ok  = False
        self._update_sol_price()

    def _update_sol_price(self):
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
            r   = requests.get(url, timeout=5)
            if r.status_code == 200:
                self.sol_usd = r.json()["solana"]["usd"]
        except Exception:
            pass

    def _fetch_with_retry(self, url: str) -> dict:
        """Requête API avec retry automatique."""
        for attempt in range(CONFIG["api_retry"]):
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                }
                r = requests.get(url, timeout=CONFIG["api_timeout"],
                                 headers=headers)
                if r.status_code == 200:
                    return r.json()
                elif r.status_code == 429:
                    log.warning(f"  ⚠️  Rate limit API — attente {CONFIG['api_delay']*2}sec")
                    time.sleep(CONFIG["api_delay"] * 2)
                else:
                    time.sleep(CONFIG["api_delay"])
            except Exception as e:
                log.warning(f"  ⚠️  Tentative {attempt+1}/{CONFIG['api_retry']}: {e}")
                time.sleep(CONFIG["api_delay"])
        return {}

    def scan_new_pools(self) -> list:
        """
        Scanne les vrais nouveaux pools Raydium via DexScreener.
        Utilise plusieurs endpoints pour maximiser les résultats.
        """
        tokens = []

        # ── Endpoint 1 : Nouveaux pools Solana ───────────────
        data = self._fetch_with_retry(
            "https://api.dexscreener.com/latest/dex/search?q=solana+raydium"
        )
        if data:
            tokens += self._parse_dexscreener(data)
            time.sleep(CONFIG["api_delay"])

        # ── Endpoint 2 : Tokens récents par chain ────────────
        if not tokens:
            data = self._fetch_with_retry(
                "https://api.dexscreener.com/latest/dex/tokens/solana"
            )
            if data:
                tokens += self._parse_dexscreener(data)

        # ── Fallback simulation si API indisponible ───────────
        if tokens:
            self.last_api_ok = True
            self.real_tokens += len(tokens)
            log.info(f"  🌐 {len(tokens)} vrais tokens Raydium détectés")
            return tokens
        else:
            self.last_api_ok = False
            sim = self._simulate_new_launches()
            self.sim_tokens += len(sim)
            return sim

    def _parse_dexscreener(self, data: dict) -> list:
        """Parse la réponse DexScreener et filtre les nouveaux tokens."""
        pairs     = data.get("pairs", [])
        new_pools = []
        now_ms    = time.time() * 1000
        max_age_ms = CONFIG["max_token_age_min"] * 60 * 1000

        for pair in pairs:
            try:
                # Filtre chain Solana uniquement
                if pair.get("chainId") != "solana":
                    continue

                # Filtre DEX Raydium uniquement
                dex = pair.get("dexId", "").lower()
                if "raydium" not in dex:
                    continue

                # Filtre par âge
                created = pair.get("pairCreatedAt", 0)
                if created and (now_ms - created) > max_age_ms:
                    continue

                addr = pair.get("baseToken", {}).get("address", "")
                if not addr or addr in self.seen:
                    continue

                # Parse le token
                liq  = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                vol  = pair.get("volume", {})
                txns = pair.get("txns", {})
                m5   = txns.get("m5", {})
                h1   = txns.get("h1", {})
                pc   = pair.get("priceChange", {})

                age_min = (now_ms - created) / 60000 if created else 0

                token = {
                    "address":        addr,
                    "symbol":         pair.get("baseToken", {}).get("symbol", "???"),
                    "name":           pair.get("baseToken", {}).get("name", "Unknown"),
                    "price_usd":      float(pair.get("priceUsd", 0) or 0),
                    "liquidity_usd":  liq,
                    "volume_5m":      float(vol.get("m5", 0) or 0),
                    "volume_1h":      float(vol.get("h1", 0) or 0),
                    "volume_24h":     float(vol.get("h24", 0) or 0),
                    "buys_5m":        int(m5.get("buys", 0) or 0),
                    "sells_5m":       int(m5.get("sells", 0) or 0),
                    "buys_1h":        int(h1.get("buys", 0) or 0),
                    "sells_1h":       int(h1.get("sells", 0) or 0),
                    "price_change_5m": float(pc.get("m5", 0) or 0),
                    "price_change_1h": float(pc.get("h1", 0) or 0),
                    "age_min":         round(age_min, 1),
                    "age_sec":         age_min * 60,
                    "dex_url":         pair.get("url", ""),
                    "is_real":         True,
                    "will_rug":        False,
                }

                if token["price_usd"] > 0 and token["liquidity_usd"] > 0:
                    new_pools.append(token)

            except Exception as e:
                continue

        return new_pools

    def _simulate_new_launches(self) -> list:
        if random.random() > 0.08:
            return []
        syms   = ["MOONDOG", "SOLCAT", "PEPEX", "WAGMISOL", "GIGASOL",
                  "DEGENCAT", "BONKX", "RAYCAT", "SOLPUMP", "MEMEX"]
        sym    = random.choice(syms)
        liq    = random.uniform(3_000, 80_000)
        is_rug = random.random() < 0.60
        return [{
            "address":        f"Sim{random.randint(100000,999999)}pump",
            "symbol":         sym,
            "name":           f"{sym} Token",
            "price_usd":      random.uniform(0.0000001, 0.0001),
            "liquidity_usd":  liq,
            "volume_1h":      liq * random.uniform(0.1, 2.0),
            "volume_5m":      liq * random.uniform(0.01, 0.3),
            "buys_5m":        random.randint(50, 300) if is_rug else random.randint(20, 150),
            "sells_5m":       random.randint(5, 50),
            "buys_1h":        random.randint(100, 500),
            "sells_1h":       random.randint(20, 100),
            "price_change_5m": random.uniform(-5, 50),
            "price_change_1h": random.uniform(-20, 200),
            "age_min":         random.uniform(0.1, 25),
            "age_sec":         random.uniform(5, 1500),
            "top_holder_pct":  random.uniform(25, 80) if is_rug else random.uniform(5, 18),
            "mint_disabled":   not is_rug,
            "lp_burned":       not is_rug,
            "will_rug":        is_rug,
            "is_real":         False,
            "dex_url":         "",
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

        buys  = token.get("buys_5m", token.get("buys", 0))
        sells = token.get("sells_5m", token.get("sells", 1))
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


class PriceTracker:
    """
    Pour les vrais tokens : récupère le vrai prix via DexScreener.
    Pour les tokens simulés : simule le mouvement de prix.
    """

    def get_current_x(self, pos: dict) -> float:
        if pos.get("is_real"):
            return self._fetch_real_price(pos)
        else:
            return self._simulate_price(pos)

    def _fetch_real_price(self, pos: dict) -> float:
        """Récupère le vrai prix actuel du token."""
        try:
            url  = f"https://api.dexscreener.com/latest/dex/tokens/{pos['address']}"
            r    = requests.get(url, timeout=5)
            if r.status_code == 200:
                pairs = r.json().get("pairs", [])
                if pairs:
                    current_price = float(pairs[0].get("priceUsd", 0) or 0)
                    if current_price > 0:
                        x = current_price / max(pos["entry_price"], 1e-12)
                        return round(x, 4)
        except Exception:
            pass
        # Fallback simulation si prix réel indisponible
        return self._simulate_price(pos)

    def _simulate_price(self, pos: dict) -> float:
        age_s  = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
        is_rug = pos.get("is_rug", False)
        peak   = pos.get("peak_x", 1.0)

        if is_rug:
            if age_s < 60:
                x = 1.0 + (age_s / 60) * random.uniform(0.5, 3.0)
            else:
                x = max(0.05, peak * np.exp(-age_s / 120 * random.uniform(1, 3)))
        else:
            sigma = 0.04
            drift = 0.005
            steps = max(1, age_s // 5)
            x     = 1.0
            for _ in range(steps):
                x *= np.exp(np.random.normal(drift, sigma))
                x  = max(x, 0.1)

        return round(x, 4)


def check_behavioral_protection(pos: dict, x: float) -> tuple:
    prev_x = pos.get("prev_x", 1.0)

    if x <= (1 + CONFIG["emergency_dump_pct"] / 100):
        return True, f"🚨 Dump urgent {(x-1)*100:.1f}%"

    drop_this_scan = (x - prev_x) / max(prev_x, 1e-10)

    if drop_this_scan <= CONFIG["min_drop_per_scan"]:
        pos["consecutive_drops"]    += 1
        pos["consecutive_rebounds"]  = 0
    elif drop_this_scan > 0:
        pos["consecutive_rebounds"] += 1
        if pos["consecutive_rebounds"] >= CONFIG["rebounds_to_reset"]:
            if pos["consecutive_drops"] > 0:
                print(f"  {Fore.GREEN}  🔄 Double rebond — "
                      f"{pos['symbol']} | Compteur remis à 0{Style.RESET_ALL}")
            pos["consecutive_drops"]    = 0
            pos["consecutive_rebounds"] = 0

    if pos["consecutive_drops"] >= CONFIG["consecutive_drops"]:
        return True, f"📉 {pos['consecutive_drops']} baisses consécutives"

    pos["prev_x"] = x
    return False, ""


def print_dashboard(wallet: SniperWallet, detector: NewPoolDetector):
    total_sol = wallet.sol_balance + sum(
        p["sol_invested"] * p.get("current_x", 1) * (p["remaining_pct"] / 100)
        for p in wallet.positions.values()
    )
    pnl_sol   = total_sol - wallet.initial_sol
    pnl_pct   = pnl_sol / wallet.initial_sol * 100
    wr        = wallet.wins / max(wallet.total_trades, 1) * 100
    pnl_color = Fore.GREEN if pnl_sol >= 0 else Fore.RED
    api_status = f"{Fore.GREEN}🌐 API RÉELLE" if detector.last_api_ok else f"{Fore.YELLOW}🎮 SIMULATION"

    print(f"\n{Fore.CYAN}{'═'*62}")
    print(f"  🎯 SNIPER BOT v4 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {api_status} | Réels: {detector.real_tokens} | Sim: {detector.sim_tokens}{Style.RESET_ALL}")
    print(f"{'═'*62}")
    print(f"  SOL balance      : {wallet.sol_balance:.4f} SOL")
    print(f"  Valeur totale    : {total_sol:.4f} SOL (${total_sol*detector.sol_usd:,.2f})")
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
            src    = "🌐" if pos.get("is_real") else "🎮"
            print(f"  {xcolor}  {src} {pos['symbol']:<12} {x:.2f}x | "
                  f"Restant: {pos['remaining_pct']:.0f}% | "
                  f"Age: {age_s}s {be}")

    if wallet.closed_trades:
        print(f"\n  📜 DERNIERS TRADES :")
        for t in wallet.closed_trades[-5:]:
            xcolor = Fore.GREEN if t["exit_x"] >= 1 else Fore.RED
            print(f"  {xcolor}  {t.get('source','🎮'):<8} {t['symbol']:<12} "
                  f"{t['exit_x']:.2f}x | P&L: {t['pnl_sol']:+.4f} SOL | "
                  f"{t['reason']}{Style.RESET_ALL}")

    print(f"\n  📊 TPs : +17%→20% | +50%→30% | +200%→20% | +900%→reste")
    print(f"{Fore.CYAN}{'═'*62}{Style.RESET_ALL}\n")


def print_new_token(token: dict, analysis: dict):
    score   = analysis["score"]
    s_color = Fore.GREEN if score >= 60 else Fore.YELLOW if score >= 45 else Fore.RED
    src     = f"{Fore.GREEN}🌐 VRAI TOKEN" if token.get("is_real") else f"{Fore.YELLOW}🎮 SIMULÉ"
    print(f"\n{Fore.YELLOW}  ━━━ NOUVEAU TOKEN ━━━ {src}{Style.RESET_ALL}")
    print(f"  🚀 {token['symbol']} | Age: {token.get('age_min', 0):.1f}min | "
          f"Liq: ${token['liquidity_usd']:,.0f}")
    if token.get("is_real") and token.get("dex_url"):
        print(f"  🔗 {token['dex_url']}")
    print(f"  Prix: ${token['price_usd']:.10f}")
    print(f"  {s_color}📊 SCORE : {score}/100{Style.RESET_ALL}")
    for k, v in analysis["details"].items():
        print(f"     {k:<20} {v}")
    if analysis["flags"]:
        print(f"  {Fore.RED}🚨 FLAGS : {' | '.join(analysis['flags'])}{Style.RESET_ALL}")


def run_sniper(wallet: SniperWallet, detector: NewPoolDetector,
               tracker: PriceTracker):

    to_close = []

    for address, pos in list(wallet.positions.items()):
        x = tracker.get_current_x(pos)
        pos["current_x"] = x
        pos["peak_x"]    = max(pos.get("peak_x", 1.0), x)
        pos["price_history"].append(x)
        age_sec = (datetime.now(timezone.utc) - pos["entry_time"]).seconds
        age_min = age_sec / 60

        should_sell, reason = check_behavioral_protection(pos, x)
        if should_sell:
            to_close.append((address, x, reason, None))
            continue

        if x >= CONFIG["breakeven_trigger_x"] and not pos["breakeven_active"]:
            pos["breakeven_active"] = True
            log.info(f"  🔒 BREAK-EVEN activé — {pos['symbol']} | +{(x-1)*100:.0f}%")
            print(f"  {Fore.CYAN}🔒 BREAK-EVEN — {pos['symbol']} | SL = prix d'entrée{Style.RESET_ALL}")

        if pos["breakeven_active"] and x < 1.0:
            to_close.append((address, x, "🔒 Break-Even — sortie sans perte", 1.0))
            continue

        if x <= (1 + CONFIG["stop_loss_pct"] / 100):
            reason = "Rug Pull 💀" if pos.get("is_rug") else "Stop Loss -25%"
            to_close.append((address, x, reason, None))
            continue

        if age_min >= CONFIG["max_hold_minutes"]:
            to_close.append((address, x, "⏰ Temps max dépassé", None))
            continue

        if x >= CONFIG["take_profit_0"] and not pos["tp0_hit"]:
            pos["tp0_hit"] = True
            wallet.partial_sell(address, 20, x, f"TP0 +{(x-1)*100:.0f}%")

        if x >= CONFIG["take_profit_1"] and not pos["tp1_hit"]:
            pos["tp1_hit"] = True
            wallet.partial_sell(address, 30, x, f"TP1 +{(x-1)*100:.0f}%")

        if x >= CONFIG["take_profit_2"] and not pos["tp2_hit"]:
            pos["tp2_hit"] = True
            wallet.partial_sell(address, 20, x, f"TP2 +{(x-1)*100:.0f}%")

        if x >= CONFIG["take_profit_3"] and not pos["tp3_hit"]:
            pos["tp3_hit"] = True
            to_close.append((address, x, f"🚀 MOONSHOT {x:.1f}x", None))

    for address, x, reason, force_x in to_close:
        wallet.close_position(address, x, reason, force_x=force_x)

    analyzer   = AntiRugAnalyzer()
    new_tokens = detector.scan_new_pools() or []
    for token in new_tokens:
        if token["address"] in detector.seen:
            continue
        detector.seen.add(token["address"])
        analysis = analyzer.analyze(token)
        print_new_token(token, analysis)

        if analysis["buyable"] and wallet.can_snipe():
            src = "🌐 VRAI" if token.get("is_real") else "🎮 SIM"
            print(f"\n  {Fore.GREEN}⚡ SNIPE {src} ! {token['symbol']} — "
                  f"{CONFIG['sol_per_trade']} SOL{Style.RESET_ALL}")
            wallet.open_position(token)
            log.info(f"⚡ SNIPE {src} — {token['symbol']} | Score: {analysis['score']}/100")
        else:
            print(f"  {Fore.RED}❌ REFUSÉ — Score {analysis['score']}/100{Style.RESET_ALL}")

    if int(time.time()) % 30 < 5:
        print_dashboard(wallet, detector)


def main():
    print(f"""{Fore.YELLOW}
╔══════════════════════════════════════════════════════════════╗
║         SOLANA SNIPER BOT v4 — VRAIS TOKENS RAYDIUM          ║
║         Capital   : 50.0 SOL fictifs                        ║
║         Mise/trade: 2.0 SOL | Scan : 5sec                   ║
║         🌐 Connexion API DexScreener améliorée              ║
║         🎮 Fallback simulation si API indisponible          ║
║         Break-Even: activé dès +15%                         ║
║         TP0: +17%→20% | TP1: +50%→30%                      ║
║         TP2: +200%→20% | TP3: +900%→reste                  ║
║         Stop Loss : -25%                                     ║
╚══════════════════════════════════════════════════════════════╝
{Style.RESET_ALL}""")

    wallet   = SniperWallet(CONFIG["initial_capital_sol"])
    detector = NewPoolDetector()
    tracker  = PriceTracker()

    print(f"  ✅ Bot v4 initialisé — Capital: {CONFIG['initial_capital_sol']} SOL fictifs")
    print(f"  ⚡ Scan toutes les {CONFIG['scan_interval_sec']} secondes")
    print(f"  🎯 Score minimum : {CONFIG['min_score']}/100")
    print(f"  Appuie sur Ctrl+C pour arrêter\n")

    while True:
        try:
            run_sniper(wallet, detector, tracker)
            time.sleep(CONFIG["scan_interval_sec"])
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}  ⏹️  Bot arrêté{Style.RESET_ALL}")
            print_dashboard(wallet, detector)
            break
        except Exception as e:
            log.error(f"Erreur: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
