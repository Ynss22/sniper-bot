"""
╔══════════════════════════════════════════════════════════════════╗
║           SOLANA EXECUTOR v2 — TRANSACTIONS RÉELLES             ║
║           PumpPortal → Raydium v3 → Jupiter (fallbacks)         ║
╚══════════════════════════════════════════════════════════════════╝

INSTALLATION :
    pip install solana solders base58 requests
"""

import os
import time
import base64
import logging
import requests

log = logging.getLogger("EXECUTOR")

SOL_MINT     = "So11111111111111111111111111111111111111112"
LAMPORTS_SOL = 1_000_000_000
RPC_ENDPOINT = "https://api.mainnet-beta.solana.com"


class SolanaExecutor:

    def __init__(self):
        self.private_key_str = os.getenv("WALLET_PRIVATE_KEY", "")
        self.wallet_address  = os.getenv("WALLET_ADDRESS", "")
        self.enabled         = bool(self.private_key_str and self.wallet_address)
        self.keypair         = None

        if self.enabled:
            self._init_wallet()
        else:
            log.warning("⚠️  Executor désactivé — clés manquantes")

    def _init_wallet(self):
        try:
            from solders.keypair import Keypair
            import base58

            key_str   = self.private_key_str.strip()
            log.info(f"🔑 Longueur clé: {len(key_str)} caractères")

            key_bytes = base58.b58decode(key_str)
            log.info(f"🔑 Bytes décodés: {len(key_bytes)}")

            if len(key_bytes) == 64:
                self.keypair = Keypair.from_bytes(key_bytes)
            elif len(key_bytes) == 32:
                self.keypair = Keypair.from_seed(key_bytes)
            else:
                # Essais alternatifs
                try:
                    self.keypair = Keypair.from_seed(key_bytes[:32])
                except Exception:
                    self.keypair = Keypair.from_bytes(key_bytes[:64])

            log.info(f"✅ Wallet initialisé : {str(self.keypair.pubkey())[:20]}...")

        except ImportError:
            log.error("❌ solders/base58 non installés")
            self.enabled = False
        except Exception as e:
            log.error(f"❌ Erreur init wallet: {e}")
            self.enabled = False

    def get_sol_balance(self) -> float:
        try:
            r = requests.post(
                RPC_ENDPOINT,
                json={"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                      "params": [self.wallet_address]},
                timeout=10
            )
            if r.status_code == 200:
                lamports = r.json().get("result", {}).get("value", 0)
                return lamports / LAMPORTS_SOL
        except Exception:
            pass
        return 0.0

    def get_token_balance(self, token_address: str) -> float:
        try:
            r = requests.post(
                RPC_ENDPOINT,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "getTokenAccountsByOwner",
                      "params": [self.wallet_address,
                                 {"mint": token_address},
                                 {"encoding": "jsonParsed"}]},
                timeout=10
            )
            if r.status_code == 200:
                accounts = r.json().get("result", {}).get("value", [])
                if accounts:
                    info = accounts[0]["account"]["data"]["parsed"]["info"]
                    return float(info["tokenAmount"]["uiAmount"] or 0)
        except Exception:
            pass
        return 0.0

    def _send_transaction(self, tx_bytes: bytes) -> str:
        """Signe et envoie une transaction Solana."""
        try:
            from solders.transaction import VersionedTransaction

            tx     = VersionedTransaction.from_bytes(tx_bytes)
            tx     = VersionedTransaction(tx.message, [self.keypair])
            tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")

            r = requests.post(
                RPC_ENDPOINT,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "sendTransaction",
                      "params": [tx_b64,
                                 {"encoding": "base64",
                                  "preflightCommitment": "confirmed",
                                  "skipPreflight": False}]},
                timeout=30
            )

            if r.status_code == 200:
                result = r.json()
                if "result" in result:
                    return result["result"]
                error = result.get("error", {})
                # Token migré de Pump.fun vers Raydium
                if "6005" in str(error) or "BondingCurve" in str(error):
                    return "MIGRATED_TO_RAYDIUM"
                log.error(f"❌ RPC erreur: {error}")
        except Exception as e:
            log.error(f"❌ Erreur envoi tx: {e}")
        return ""

    def _confirm_transaction(self, tx_hash: str, retries: int = 20) -> bool:
        for _ in range(retries):
            try:
                r = requests.post(
                    RPC_ENDPOINT,
                    json={"jsonrpc": "2.0", "id": 1,
                          "method": "getSignatureStatuses",
                          "params": [[tx_hash]]},
                    timeout=10
                )
                if r.status_code == 200:
                    statuses = r.json().get("result", {}).get("value", [])
                    if statuses and statuses[0]:
                        status = statuses[0]
                        if status.get("confirmationStatus") in ["confirmed", "finalized"]:
                            if status.get("err"):
                                log.error(f"❌ Transaction échouée: {status['err']}")
                                return False
                            return True
            except Exception:
                pass
            time.sleep(2)
        return False

    def _buy_pumpfun(self, token_address: str, sol_amount: float,
                     symbol: str) -> dict:
        """Achat via PumpPortal pour tokens Pump.fun."""
        try:
            r = requests.post(
                "https://pumpportal.fun/api/trade-local",
                json={"publicKey":        self.wallet_address,
                      "action":           "buy",
                      "mint":             token_address,
                      "amount":           sol_amount,
                      "denominatedInSol": "true",
                      "slippage":         10,
                      "priorityFee":      0.001,
                      "pool":             "pump" if token_address.endswith("pump") else "raydium"},
                timeout=15
            )

            if r.status_code != 200:
                return {"success": False, "reason": f"PumpPortal {r.status_code}"}

            tx_hash = self._send_transaction(r.content)

            if tx_hash == "MIGRATED_TO_RAYDIUM":
                return {"success": False, "reason": "migrated_to_raydium"}

            if tx_hash:
                confirmed = self._confirm_transaction(tx_hash)
                if confirmed:
                    log.info(f"✅ ACHAT PUMP.FUN — {symbol} | {tx_hash[:20]}...")
                    return {"success": True, "tx_hash": tx_hash,
                            "source": "pumpfun"}

        except Exception as e:
            log.error(f"❌ PumpPortal: {e}")

        return {"success": False, "reason": "pumpportal_failed"}

    def _buy_jupiter(self, token_address: str, sol_amount: float,
                     symbol: str) -> dict:
        """Achat via Jupiter — meilleur prix garanti."""
        try:
            lamports = int(sol_amount * LAMPORTS_SOL)

            # Quote
            r = requests.get(
                f"https://api.jup.ag/swap/v1/quote"
                f"?inputMint={SOL_MINT}"
                f"&outputMint={token_address}"
                f"&amount={lamports}"
                f"&slippageBps=500",
                timeout=10
            )

            if r.status_code != 200:
                return {"success": False, "reason": f"Jupiter quote {r.status_code}"}

            quote        = r.json()
            price_impact = float(quote.get("priceImpactPct", 0))

            if price_impact > 15:
                return {"success": False,
                        "reason": f"Impact prix trop élevé: {price_impact:.1f}%"}

            # Swap
            r2 = requests.post(
                "https://api.jup.ag/swap/v1/swap",
                json={"quoteResponse":           quote,
                      "userPublicKey":            self.wallet_address,
                      "wrapAndUnwrapSol":         True,
                      "dynamicComputeUnitLimit":  True,
                      "prioritizationFeeLamports": 100000},
                timeout=15
            )

            if r2.status_code != 200:
                return {"success": False, "reason": f"Jupiter swap {r2.status_code}"}

            swap_tx = r2.json().get("swapTransaction", "")
            if not swap_tx:
                return {"success": False, "reason": "Pas de transaction Jupiter"}

            tx_bytes = base64.b64decode(swap_tx)
            tx_hash  = self._send_transaction(tx_bytes)

            if tx_hash and tx_hash != "MIGRATED_TO_RAYDIUM":
                confirmed = self._confirm_transaction(tx_hash)
                if confirmed:
                    log.info(f"✅ ACHAT JUPITER — {symbol} | {tx_hash[:20]}...")
                    return {"success": True, "tx_hash": tx_hash,
                            "source": "jupiter"}

        except Exception as e:
            log.error(f"❌ Jupiter: {e}")

        return {"success": False, "reason": "jupiter_failed"}

    def _sell_pumpfun(self, token_address: str, token_amount_pct: float,
                      symbol: str) -> dict:
        """Vente via PumpPortal."""
        try:
            balance = self.get_token_balance(token_address)
            if balance <= 0:
                return {"success": False, "reason": "Pas de tokens"}

            amount_to_sell = balance * (token_amount_pct / 100)

            r = requests.post(
                "https://pumpportal.fun/api/trade-local",
                json={"publicKey":        self.wallet_address,
                      "action":           "sell",
                      "mint":             token_address,
                      "amount":           amount_to_sell,
                      "denominatedInSol": "false",
                      "slippage":         10,
                      "priorityFee":      0.001,
                      "pool":             "pump" if token_address.endswith("pump") else "raydium"},
                timeout=15
            )

            if r.status_code != 200:
                return {"success": False, "reason": f"PumpPortal sell {r.status_code}"}

            tx_hash = self._send_transaction(r.content)

            if tx_hash == "MIGRATED_TO_RAYDIUM":
                return {"success": False, "reason": "migrated_to_raydium"}

            if tx_hash:
                confirmed = self._confirm_transaction(tx_hash)
                if confirmed:
                    log.info(f"✅ VENTE PUMP.FUN — {symbol} | {tx_hash[:20]}...")
                    return {"success": True, "tx_hash": tx_hash}

        except Exception as e:
            log.error(f"❌ PumpPortal sell: {e}")

        return {"success": False, "reason": "pumpportal_sell_failed"}

    def buy_token(self, token_address: str, sol_amount: float,
                  symbol: str = "???") -> dict:
        """
        Point d'entrée principal pour acheter un token.
        Ordre de priorité : PumpPortal → Jupiter
        """
        if not self.enabled or not self.keypair:
            return {"success": False, "reason": "Executor désactivé"}

        # Vérification balance
        balance = self.get_sol_balance()
        if balance < sol_amount + 0.005:
            return {"success": False,
                    "reason": f"Balance insuffisante: {balance:.4f} SOL"}

        log.info(f"🛒 ACHAT RÉEL — {symbol}")
        log.info(f"   Montant : {sol_amount:.4f} SOL")
        log.info(f"   Token   : {token_address[:20]}...")

        # Essai 1 : PumpPortal (supporte pump.fun ET Raydium)
        if True:
            log.info("🎯 Token Pump.fun — PumpPortal")
            result = self._buy_pumpfun(token_address, sol_amount, symbol)
            if result["success"]:
                return result
            if result.get("reason") == "migrated_to_raydium":
                log.info("🔄 Token migré sur Raydium — Jupiter")
            else:
                log.warning(f"⚠️  PumpPortal échoué: {result.get('reason')}")

        # Essai 2 : Jupiter
        log.info("🔄 Tentative Jupiter...")
        result = self._buy_jupiter(token_address, sol_amount, symbol)
        if result["success"]:
            return result

        log.error("❌ Tous les providers ont échoué")
        return {"success": False, "reason": "Tous providers échoués"}

    def sell_token(self, token_address: str, sell_pct: float = 100,
                   symbol: str = "???") -> dict:
        """Vend un pourcentage de la position."""
        if not self.enabled or not self.keypair:
            return {"success": False, "reason": "Executor désactivé"}

        log.info(f"💰 VENTE RÉELLE — {symbol} | {sell_pct}%")

        # Essai PumpPortal
        if token_address.endswith("pump"):
            result = self._sell_pumpfun(token_address, sell_pct, symbol)
            if result["success"]:
                return result

        # TODO: Jupiter sell pour tokens Raydium
        return {"success": False, "reason": "Vente non implémentée pour ce token"}
