"""
╔══════════════════════════════════════════════════════════════════╗
║       SOLANA SNIPER BOT — VERSION BASE RÉELLE                   ║
║       PumpPortal (pump.fun) + Jupiter API (Raydium)             ║
╚══════════════════════════════════════════════════════════════════╝

VARIABLES D'ENVIRONNEMENT REQUISES :
    WALLET_PRIVATE_KEY  → clé privée Phantom base58
    WALLET_ADDRESS      → adresse publique Solana
    JUPITER_API_KEY     → clé gratuite sur portal.jup.ag
"""

import os
import time
import base64
import logging
import requests
from datetime import datetime, timezone
from colorama import Fore, Style, init

init(autoreset=True)

# ─────────────────────────────────────────────────────────────────
# MODE
# ─────────────────────────────────────────────────────────────────
WALLET_KEY     = os.getenv("WALLET_PRIVATE_KEY", "")
WALLET_ADR     = os.getenv("WALLET_ADDRESS", "")
JUPITER_APIKEY = os.getenv("JUPITER_API_KEY", "")
REAL_MODE      = bool(WALLET_KEY and WALLET_ADR and JUPITER_APIKEY)

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────
CONFIG = {
    "initial_capital_sol":  50.0,
    "stake_pct":             5.0,   # Mise = 5% du solde actualisé
    "max_positions":         3,

    "min_liquidity_usd":   5_000,
    "max_liquidity_usd": 200_000,
    "max_token_age_min":    30,
    "min_score":            80,
    "max_top_holder_pct":   20,

    "stop_loss_pct":        -30,
    "breakeven_trigger_pct": 15,

    "tp0_pct":  17,  "tp0_sell": 20,
    "tp1_pct":  50,  "tp1_sell": 30,
    "tp2_pct": 200,  "tp2_sell": 20,
    "tp3_pct": 900,  "tp3_sell":100,

    "scan_interval_sec":  2,
    "max_hold_minutes": 120,
    "slippage_bps":    1000,   # 10%
}

# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
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
# EXÉCUTEUR — transactions réelles
# ─────────────────────────────────────────────────────────────────
class SolanaExecutor:
    SOL_MINT = "So11111111111111111111111111111111111111112"

    def __init__(self):
        self.enabled = REAL_MODE
        self.keypair = None
        self.pub_key = WALLET_ADR
        if self.enabled:
            self._init_wallet()

    def _init_wallet(self):
        try:
            import base58
            from solders.keypair import Keypair
            raw = base58.b58decode(WALLET_KEY)
            self.keypair = Keypair.from_bytes(raw) if len(raw) == 64 else Keypair.from_seed(raw)
            self.pub_key = str(self.keypair.pubkey())
            log.info(f"✅ Wallet : {self.pub_key[:16]}...")
        except Exception as e:
            log.error(f"❌ Init wallet : {e}")
            self.enabled = False

    def get_sol_balance(self) -> float:
        try:
            r = requests.post(
                "https://api.mainnet-beta.solana.com",
                json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[self.pub_key]},
                timeout=10
            )
            return r.json()["result"]["value"] / 1_000_000_000
        except Exception:
            return 0.0

    def _get_token_balance(self, token_address: str) -> int:
        try:
            r = requests.post(
                "https://api.mainnet-beta.solana.com",
                json={
                    "jsonrpc":"2.0","id":1,
                    "method":"getTokenAccountsByOwner",
                    "params":[self.pub_key, {"mint": token_address}, {"encoding":"jsonParsed"}]
                },
                timeout=10
            )
            accounts = r.json()["result"]["value"]
            if not accounts:
                return 0
            return int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        except Exception:
            return 0

    def _sign_and_send(self, tx_b64: str) -> str:
        try:
            from solders.transaction import VersionedTransaction
            from solana.rpc.api import Client
            from solana.rpc.types import TxOpts

            tx_bytes = base64.b64decode(tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed = VersionedTransaction(tx.message, [self.keypair])
            client = Client("https://api.mainnet-beta.solana.com")
            resp = client.send_raw_transaction(
                bytes(signed),
                opts=TxOpts(skip_preflight=True, max_retries=3)
            )
            return str(resp.value)
        except Exception as e:
            log.error(f"  ❌ sign_and_send : {e}")
            return ""

    # ── Achat ────────────────────────────────────────────────────
    def buy(self, token_address: str, symbol: str, amount_sol: float) -> dict:
        if not self.enabled:
            return {"success": True, "simulated": True}

        lamports = int(amount_sol * 1_000_000_000)
        log.info(f"🛒 ACHAT RÉEL — {symbol} | {amount_sol:.4f} SOL")

        if token_address.endswith("pump"):
            r = self._buy_pumpportal(token_address, symbol, amount_sol)
            if r["success"]:
                return r
            log.warning("  ⚠️  PumpPortal échoué → Jupiter")

        return self._buy_jupiter(token_address, symbol, lamports)

    def _buy_pumpportal(self, token_address: str, symbol: str, amount_sol: float) -> dict:
        try:
            from solders.transaction import VersionedTransaction
            from solana.rpc.api import Client
            from solana.rpc.types import TxOpts

            r = requests.post(
                "https://pumpportal.fun/api/trade-local",
                headers={"Content-Type": "application/json"},
                json={
                    "publicKey": self.pub_key,
                    "action": "buy",
                    "mint": token_address,
                    "amount": amount_sol,
                    "denominatedInSol": "true",
                    "slippage": CONFIG["slippage_bps"] / 100,
                    "priorityFee": 0.0005,
                    "pool": "pump"
                },
                timeout=15
            )
            if r.status_code != 200:
                return {"success": False, "reason": f"HTTP {r.status_code}: {r.text[:100]}"}

            tx = VersionedTransaction.from_bytes(r.content)
            signed = VersionedTransaction(tx.message, [self.keypair])
            client = Client("https://api.mainnet-beta.solana.com")
            resp = client.send_raw_transaction(bytes(signed), opts=TxOpts(skip_preflight=True))
            sig = str(resp.value)
            log.info(f"  ✅ PumpPortal TX : {sig[:24]}...")
            return {"success": True, "signature": sig, "provider": "pumpportal"}
        except Exception as e:
            return {"success": False, "reason": str(e)}

    def _buy_jupiter(self, token_address: str, symbol: str, lamports: int) -> dict:
        try:
            h = {"Authorization": f"Bearer {JUPITER_APIKEY}"}
            r = requests.get(
                "https://api.jup.ag/swap/v1/quote",
                headers=h,
                params={
                    "inputMint": self.SOL_MINT,
                    "outputMint": token_address,
                    "amount": lamports,
                    "slippageBps": CONFIG["slippage_bps"],
                },
                timeout=10
            )
            if r.status_code != 200:
                return {"success": False, "reason": f"Jupiter quote HTTP {r.status_code}: {r.text[:100]}"}

            r2 = requests.post(
                "https://api.jup.ag/swap/v1/swap",
                headers={**h, "Content-Type": "application/json"},
                json={
                    "quoteResponse": r.json(),
                    "userPublicKey": self.pub_key,
                    "wrapAndUnwrapSol": True,
                    "prioritizationFeeLamports": 500_000,
                },
                timeout=15
            )
            if r2.status_code != 200:
                return {"success": False, "reason": f"Jupiter swap HTTP {r2.status_code}: {r2.text[:100]}"}

            sig = self._sign_and_send(r2.json().get("swapTransaction", ""))
            if sig:
                log.info(f"  ✅ Jupiter achat TX : {sig[:24]}...")
                return {"success": True, "signature": sig, "provider": "jupiter"}
            return {"success": False, "reason": "Signature échouée"}
        except Exception as e:
            return {"success": False, "reason": str(e)}

    # ── Vente ────────────────────────────────────────────────────
    def sell(self, token_address: str, symbol: str, sell_pct: float) -> dict:
        if not self.enabled:
            return {"success": True, "simulated": True}

        log.info(f"💰 VENTE RÉELLE — {symbol} | {sell_pct:.0f}%")

        if token_address.endswith("pump"):
            r = self._sell_pumpportal(token_address, symbol, sell_pct)
            if r["success"]:
                return r
            log.warning("  ⚠️  PumpPortal vente échouée → Jupiter")

        return self._sell_jupiter(token_address, symbol, sell_pct)

    def _sell_pumpportal(self, token_address: str, symbol: str, sell_pct: float) -> dict:
        try:
            from solders.transaction import VersionedTransaction
            from solana.rpc.api import Client
            from solana.rpc.types import TxOpts

            r = requests.post(
                "https://pumpportal.fun/api/trade-local",
                headers={"Content-Type": "application/json"},
                json={
                    "publicKey": self.pub_key,
                    "action": "sell",
                    "mint": token_address,
                    "amount": f"{sell_pct}%",
                    "denominatedInSol": "false",
                    "slippage": CONFIG["slippage_bps"] / 100,
                    "priorityFee": 0.0005,
                    "pool": "pump"
                },
                timeout=15
            )
            if r.status_code != 200:
                return {"success": False, "reason": f"HTTP {r.status_code}: {r.text[:100]}"}

            tx = VersionedTransaction.from_bytes(r.content)
            signed = VersionedTransaction(tx.message, [self.keypair])
            client = Client("https://api.mainnet-beta.solana.com")
            resp = client.send_raw_transaction(bytes(signed), opts=TxOpts(skip_preflight=True))
            sig = str(resp.value)
            log.info(f"  ✅ PumpPortal vente TX : {sig[:24]}...")
            return {"success": True, "signature": sig, "provider": "pumpportal"}
        except Exception as e:
            return {"success": False, "reason": str(e)}

    def _sell_jupiter(self, token_address: str, symbol: str, sell_pct: float) -> dict:
        try:
            balance = self._get_token_balance(token_address)
            if balance <= 0:
                return {"success": False, "reason": "Solde token nul"}

            amount = int(balance * (sell_pct / 100))
            h = {"Authorization": f"Bearer {JUPITER_APIKEY}"}

            r = requests.get(
                "https://api.jup.ag/swap/v1/quote",
                headers=h,
                params={
                    "inputMint": token_address,
                    "outputMint": self.SOL_MINT,
                    "amount": amount,
                    "slippageBps": CONFIG["slippage_bps"],
                },
                timeout=10
            )
            if r.status_code != 200:
                return {"success": False, "reason": f"Jupiter sell quote HTTP {r.status_code}"}

            r2 = requests.post(
                "https://api.jup.ag/swap/v1/swap",
                headers={**h, "Content-Type": "application/json"},
                json={
                    "quoteResponse": r.json(),
                    "userPublicKey": self.pub_key,
                    "wrapAndUnwrapSol": True,
                    "prioritizationFeeLamports": 500_000,
                },
                timeout=15
            )
            if r2.status_code != 200:
                return {"success": False, "reason": f"Jupiter sell swap HTTP {r2.status_code}"}

            sig = self._sign_and_send(r2.json().get("swapTransaction", ""))
            if sig:
                log.info(f"  ✅ Jupiter vente TX : {sig[:24]}...")
                return {"success": True, "signature": sig, "provider": "jupiter"}
            return {"success": False, "reason": "Signature vente échouée"}
        except Exception as e:
            return {"success": False, "reason": str(e)}


# ─────────────────────────────────────────────────────────────────
# WALLET
# ─────────────────────────────────────────────────────────────────
class Wallet:
    def __init__(self, executor: SolanaExecutor):
        self.executor    = executor
        self.sol_usd     = self._get_sol_price()
        self.positions   = {}
        self.trades      = []
        self.wins        = 0
        self.losses      = 0

        if REAL_MODE:
            bal = executor.get_sol_balance()
            self.sol_balance = bal if bal > 0 else CONFIG["initial_capital_sol"]
            log.info(f"  💰 Solde wallet : {self.sol_balance:.4f} SOL")
        else:
            self.sol_balance = CONFIG["initial_capital_sol"]

    def refresh_balance(self):
        if REAL_MODE:
            bal = self.executor.get_sol_balance()
            if bal > 0:
                self.sol_balance = bal

    def _get_sol_price(self) -> float:
        try:
            r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", timeout=5)
            return float(r.json()["price"])
        except Exception:
            return 87.0

    @property
    def total_value_sol(self):
        return self.sol_balance + sum(p["current_value_sol"] for p in self.positions.values())

    @property
    def pnl_sol(self):
        return self.total_value_sol - CONFIG["initial_capital_sol"]

    @property
    def pnl_pct(self):
        return (self.pnl_sol / CONFIG["initial_capital_sol"]) * 100

    @property
    def win_rate(self):
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0.0


# ─────────────────────────────────────────────────────────────────
# DÉTECTEUR DE TOKENS (DexScreener)
# ─────────────────────────────────────────────────────────────────
class TokenDetector:
    def get_new_tokens(self) -> list:
        try:
            r = requests.get(
                "https://api.dexscreener.com/latest/dex/search?q=solana",
                timeout=10
            )
            pairs = r.json().get("pairs", [])
            tokens = []
            for pair in pairs:
                if pair.get("chainId") != "solana":
                    continue
                addr = pair.get("baseToken", {}).get("address", "")
                if not addr:
                    continue
                tokens.append({
                    "symbol":    pair.get("baseToken", {}).get("symbol", "???"),
                    "address":   addr,
                    "pair_addr": pair.get("pairAddress", ""),
                    "liq_usd":   float(pair.get("liquidity", {}).get("usd", 0)),
                    "age_min":   self._age_min(pair.get("pairCreatedAt")),
                    "price_usd": float(pair.get("priceUsd", 0) or 0),
                    "buy_pct":   self._buy_pct(pair),
                })
            return tokens
        except Exception as e:
            log.debug(f"DexScreener: {e}")
            return []

    def _get_pair(self, addr: str) -> dict:
        try:
            r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8)
            pairs = [p for p in r.json().get("pairs", []) if p.get("chainId") == "solana"]
            if not pairs:
                return {}
            return sorted(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0)), reverse=True)[0]
        except Exception:
            return {}

    def _age_min(self, created_at) -> float:
        if not created_at:
            return 9999
        try:
            return (datetime.now(timezone.utc).timestamp() * 1000 - float(created_at)) / 60_000
        except Exception:
            return 9999

    def _buy_pct(self, pair: dict) -> float:
        try:
            t = pair.get("txns", {}).get("m5", {})
            b, s = int(t.get("buys", 0)), int(t.get("sells", 0))
            return (b / (b + s) * 100) if (b + s) > 0 else 50.0
        except Exception:
            return 50.0


# ─────────────────────────────────────────────────────────────────
# ANALYSEUR ANTI-RUG (RugCheck)
# ─────────────────────────────────────────────────────────────────
class AntiRugAnalyzer:
    def analyze(self, token: dict) -> dict:
        score, flags, detail = 0, [], {}
        rc = self._rugcheck(token["address"])

        if rc.get("mint_disabled"):
            score += 25
            detail["Mint Authority"]  = "✅ Désactivée (+25pts)"
        else:
            flags.append("MINT_ACTIVE")
            detail["Mint Authority"]  = "❌ ACTIVE — risque"

        if rc.get("lp_burned"):
            score += 25
            detail["LP Tokens"] = "✅ Brûlés (+25pts)"
        else:
            flags.append("LP_NOT_BURNED")
            detail["LP Tokens"] = "❌ Non brûlés"

        th = rc.get("top_holder_pct", 100.0)
        if th <= CONFIG["max_top_holder_pct"]:
            score += 20
            detail["Top Holder"] = f"✅ {th:.1f}% (+20pts)"
        else:
            flags.append("WHALE_CONCENTRATION")
            detail["Top Holder"] = f"❌ {th:.1f}% — dangereux"

        if token["liq_usd"] >= CONFIG["min_liquidity_usd"]:
            score += 15
            detail["Liquidité"] = f"✅ ${token['liq_usd']:,.0f} (+15pts)"
        else:
            flags.append("LOW_LIQUIDITY")
            detail["Liquidité"] = f"❌ ${token['liq_usd']:,.0f} — faible"

        if token["buy_pct"] >= 55:
            score += 15
            detail["Buy Pressure"] = f"✅ {token['buy_pct']:.0f}% (+15pts)"
        else:
            detail["Buy Pressure"] = f"⚠️  {token['buy_pct']:.0f}% (+0pts)"

        return {"score": score, "flags": flags, "detail": detail}

    def _rugcheck(self, addr: str) -> dict:
        try:
            r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report/summary", timeout=8)
            data = r.json()
            risks = [x.get("name", "").lower() for x in data.get("risks", [])]
            markets = data.get("markets", [])
            lp_burned = markets[0].get("lp", {}).get("lpBurned", False) if markets else False
            holders = data.get("topHolders", [])
            top_holder = float(holders[0].get("pct", 1.0)) * 100 if holders else 100.0
            return {
                "mint_disabled": not any("mint" in r for r in risks) or "mint authority disabled" in risks,
                "lp_burned":     lp_burned or "lp burned" in risks,
                "top_holder_pct": top_holder,
            }
        except Exception:
            return {"mint_disabled": False, "lp_burned": False, "top_holder_pct": 100.0}


# ─────────────────────────────────────────────────────────────────
# GESTIONNAIRE DE POSITIONS
# ─────────────────────────────────────────────────────────────────
class PositionManager:
    def __init__(self, executor: SolanaExecutor):
        self.executor = executor

    def open_position(self, wallet: Wallet, token: dict, score: int) -> bool:
        if len(wallet.positions) >= CONFIG["max_positions"]:
            return False

        size_sol = round(wallet.sol_balance * (CONFIG["stake_pct"] / 100), 4)
        if size_sol < 0.01:
            log.warning("  ⚠️  Solde insuffisant")
            return False

        result = self.executor.buy(token["address"], token["symbol"], size_sol)
        if not result["success"]:
            log.error(f"  ❌ Achat échoué : {result.get('reason', '?')}")
            return False

        # Solde réel actualisé après achat
        wallet.refresh_balance()

        wallet.positions[token["symbol"]] = {
            "symbol":            token["symbol"],
            "address":           token["address"],
            "entry_price":       token["price_usd"],
            "current_price":     token["price_usd"],
            "size_sol":          size_sol,
            "remaining_pct":     100.0,
            "current_value_sol": size_sol,
            "open_time":         datetime.now(timezone.utc),
            "score":             score,
            "breakeven":         False,
            "tps_hit":           [],
        }

        mode_tag = f"{Fore.RED}RÉEL{Style.RESET_ALL}" if REAL_MODE else f"{Fore.GREEN}SIM{Style.RESET_ALL}"
        print(f"\n  [{mode_tag}] ⚡ SNIPE {token['symbol']} — {size_sol:.4f} SOL | Score {score}/100")
        return True

    def update_positions(self, wallet: Wallet):
        to_close = []
        for symbol, pos in list(wallet.positions.items()):
            price = self._current_price(pos["address"])
            if price <= 0:
                continue

            pos["current_price"] = price
            pnl = ((price - pos["entry_price"]) / pos["entry_price"]) * 100
            pos["current_value_sol"] = pos["size_sol"] * (pos["remaining_pct"] / 100) * (1 + pnl / 100)

            age = (datetime.now(timezone.utc) - pos["open_time"]).total_seconds() / 60
            if age >= CONFIG["max_hold_minutes"]:
                to_close.append((symbol, "TIMEOUT", pnl))
                continue

            if pnl <= CONFIG["stop_loss_pct"]:
                to_close.append((symbol, "STOP_LOSS", pnl))
                continue

            if not pos["breakeven"] and pnl >= CONFIG["breakeven_trigger_pct"]:
                pos["breakeven"] = True
                log.info(f"  🔒 Break-even activé — {symbol}")

            if pos["breakeven"] and pnl <= 0:
                to_close.append((symbol, "BREAKEVEN_SL", pnl))
                continue

            self._check_tp(wallet, pos, pnl, symbol)

        for symbol, reason, pnl in to_close:
            self._close(wallet, symbol, reason, pnl)

    def _check_tp(self, wallet: Wallet, pos: dict, pnl: float, symbol: str):
        tps = [
            (0, CONFIG["tp0_pct"], CONFIG["tp0_sell"]),
            (1, CONFIG["tp1_pct"], CONFIG["tp1_sell"]),
            (2, CONFIG["tp2_pct"], CONFIG["tp2_sell"]),
            (3, CONFIG["tp3_pct"], CONFIG["tp3_sell"]),
        ]
        for tp_id, threshold, sell_pct in tps:
            if tp_id in pos["tps_hit"] or pnl < threshold:
                continue

            result = self.executor.sell(pos["address"], symbol, sell_pct)
            if not result["success"]:
                log.error(f"  ❌ TP{tp_id} vente échouée : {result.get('reason', '?')}")
                continue

            pos["tps_hit"].append(tp_id)
            realized = pos["size_sol"] * (sell_pct / 100) * (pos["remaining_pct"] / 100) * (1 + pnl / 100)
            pos["remaining_pct"] *= (1 - sell_pct / 100)
            wallet.refresh_balance()

            log.info(f"  💰 TP{tp_id} — {symbol} | +{pnl:.0f}% | +{realized:.4f} SOL")

            if tp_id == 3 or pos["remaining_pct"] < 1:
                self._close(wallet, symbol, f"TP{tp_id}", pnl)

    def _close(self, wallet: Wallet, symbol: str, reason: str, pnl: float):
        if symbol not in wallet.positions:
            return
        pos = wallet.positions[symbol]

        if reason not in ("TP3", "BREAKEVEN_SL"):
            self.executor.sell(pos["address"], symbol, 100)

        wallet.refresh_balance()

        if pnl > 0:
            wallet.wins += 1
        else:
            wallet.losses += 1

        wallet.trades.append({"symbol": symbol, "pnl_pct": pnl, "reason": reason})
        del wallet.positions[symbol]

        c = Fore.GREEN if pnl > 0 else Fore.RED
        log.info(f"  {c}🔴 FERMÉ — {symbol} | {pnl:+.1f}% | {reason}{Style.RESET_ALL}")

    def _current_price(self, addr: str) -> float:
        try:
            r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=5)
            pairs = [p for p in r.json().get("pairs", []) if p.get("chainId") == "solana"]
            return float(pairs[0].get("priceUsd", 0)) if pairs else 0.0
        except Exception:
            return 0.0


# ─────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────
def print_dashboard(wallet: Wallet):
    usd = wallet.sol_usd
    print(f"\n{'═' * 62}")
    print(f"  🎯 SNIPER BOT — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Mode : {'🔴 TRADING RÉEL' if REAL_MODE else '🎮 SIMULATION'}")
    print(f"{'─' * 62}")
    print(f"  SOL balance   : {wallet.sol_balance:.4f} SOL  (${wallet.sol_balance * usd:,.0f})")
    print(f"  Valeur totale : {wallet.total_value_sol:.4f} SOL  (${wallet.total_value_sol * usd:,.0f})")
    c = Fore.GREEN if wallet.pnl_sol >= 0 else Fore.RED
    print(f"  P&L           : {c}{wallet.pnl_sol:+.4f} SOL ({wallet.pnl_pct:+.2f}%){Style.RESET_ALL}")
    print(f"  Trades        : {wallet.wins + wallet.losses} | ✅ {wallet.wins} | ❌ {wallet.losses} | WR {wallet.win_rate:.1f}%")

    if wallet.positions:
        print(f"{'─' * 62}")
        print(f"  POSITIONS ({len(wallet.positions)}/{CONFIG['max_positions']})")
        for sym, pos in wallet.positions.items():
            pnl = ((pos["current_price"] - pos["entry_price"]) / pos["entry_price"] * 100) if pos["entry_price"] else 0
            age = (datetime.now(timezone.utc) - pos["open_time"]).total_seconds() / 60
            c = Fore.GREEN if pnl >= 0 else Fore.RED
            print(f"    {sym:<12} {c}{pnl:+.1f}%{Style.RESET_ALL}  Age:{age:.0f}min  TPs:{pos['tps_hit']}")

    if wallet.trades:
        print(f"{'─' * 62}")
        print(f"  DERNIERS TRADES")
        for t in wallet.trades[-5:]:
            c = Fore.GREEN if t["pnl_pct"] > 0 else Fore.RED
            print(f"    {t['symbol']:<12} {c}{t['pnl_pct']:+.1f}%{Style.RESET_ALL}  {t['reason']}")

    print(f"{'─' * 62}")
    print(f"  TP0 +17%→20%  TP1 +50%→30%  TP2 +200%→20%  TP3 +900%→tout")
    print(f"  SL -30%  |  Break-even +15%  |  Mise 5% du solde")
    print(f"{'═' * 62}")


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'╔' + '═' * 62 + '╗'}")
    print(f"║{'  SOLANA SNIPER BOT — BASE RÉELLE':^62}║")
    print(f"║{'  Mise : 5% du solde  |  SL -30%  |  BE +15%':^62}║")
    print(f"║{'  Mode : ' + ('🔴 TRADING RÉEL' if REAL_MODE else '🎮 SIMULATION'):^62}║")
    print(f"{'╚' + '═' * 62 + '╝'}\n")

    if not REAL_MODE:
        print(f"  {Fore.YELLOW}⚠️  Variables manquantes — mode simulation activé")
        missing = []
        if not WALLET_KEY:     missing.append("WALLET_PRIVATE_KEY")
        if not WALLET_ADR:     missing.append("WALLET_ADDRESS")
        if not JUPITER_APIKEY: missing.append("JUPITER_API_KEY")
        print(f"  Manquantes : {', '.join(missing)}{Style.RESET_ALL}\n")

    executor    = SolanaExecutor()
    wallet      = Wallet(executor)
    detector    = TokenDetector()
    anti_rug    = AntiRugAnalyzer()
    pos_mgr     = PositionManager(executor)
    seen_tokens = set()
    scan_count  = 0

    print(f"  ✅ Bot actif — Capital : {wallet.sol_balance:.4f} SOL")
    print(f"  🎯 Score min : {CONFIG['min_score']}/100  |  Ctrl+C pour arrêter\n")

    while True:
        try:
            scan_count += 1

            # Positions — mise à jour à chaque cycle (2 sec)
            if wallet.positions:
                pos_mgr.update_positions(wallet)

            # Nouveaux tokens — scan à chaque cycle (2 sec)
            tokens = detector.get_new_tokens()
            for token in tokens:
                if token["address"] in seen_tokens:
                    continue
                seen_tokens.add(token["address"])

                if not (CONFIG["min_liquidity_usd"] <= token["liq_usd"] <= CONFIG["max_liquidity_usd"]):
                    continue
                if token["age_min"] > CONFIG["max_token_age_min"]:
                    continue
                if token["price_usd"] <= 0:
                    continue

                print(f"\n  ━━━ NOUVEAU TOKEN ━━━")
                print(f"  🚀 {token['symbol']} | Age: {token['age_min']:.1f}min | Liq: ${token['liq_usd']:,.0f}")
                print(f"  🔗 https://dexscreener.com/solana/{token['pair_addr']}")

                result = anti_rug.analyze(token)
                score  = result["score"]

                print(f"  📊 SCORE : {score}/100")
                for label, val in result["detail"].items():
                    print(f"     {label:<20} {val}")
                if result["flags"]:
                    print(f"  🚨 FLAGS : {' | '.join(result['flags'])}")

                if score < CONFIG["min_score"]:
                    print(f"  ❌ REFUSÉ (score {score} < {CONFIG['min_score']})")
                    continue
                if len(wallet.positions) >= CONFIG["max_positions"]:
                    print(f"  ⚠️  Max positions atteint ({CONFIG['max_positions']})")
                    continue

                pos_mgr.open_position(wallet, token, score)

            # Cache
            if len(seen_tokens) > 500:
                seen_tokens = set(list(seen_tokens)[-200:])

            # Dashboard toutes les 30 sec
            if scan_count % 15 == 0:
                wallet.sol_usd = wallet._get_sol_price()
                print_dashboard(wallet)

            time.sleep(CONFIG["scan_interval_sec"])

        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}  ⏹️  Arrêt{Style.RESET_ALL}")
            print_dashboard(wallet)
            break
        except Exception as e:
            log.error(f"Erreur : {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
