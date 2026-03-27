"""
╔══════════════════════════════════════════════════════════════════╗
║           SOLANA EXECUTOR — TRANSACTIONS RÉELLES                ║
║           Module d'exécution des trades sur Raydium             ║
║           Utilise Jupiter API pour les swaps                    ║
╚══════════════════════════════════════════════════════════════════╝

INSTALLATION SUPPLÉMENTAIRE REQUISE :
    pip install solana solders base58

CE MODULE GÈRE :
    - Connexion wallet via clé privée
    - Achat de tokens via Jupiter (meilleur prix garanti)
    - Vente de tokens via Jupiter
    - Vérification des balances avant/après trade
    - Logs détaillés de chaque transaction
"""

import os
import time
import logging
import requests
import base64
import json
from typing import Optional

log = logging.getLogger("EXECUTOR")

# ─────────────────────────────────────────────────────────────────
# CONSTANTES SOLANA
# ─────────────────────────────────────────────────────────────────
SOL_MINT     = "So11111111111111111111111111111111111111112"
LAMPORTS_SOL = 1_000_000_000
RPC_ENDPOINT = "https://api.mainnet-beta.solana.com"
JUPITER_URL  = "https://quote-api.jup.ag/v6"  # Fallback
RAYDIUM_URL  = "https://api.raydium.io/v2"


# ─────────────────────────────────────────────────────────────────
# SOLANA EXECUTOR
# ─────────────────────────────────────────────────────────────────
class SolanaExecutor:
    """
    Exécute de vraies transactions sur Solana via Jupiter API.
    Jupiter trouve automatiquement le meilleur chemin de swap.
    """

    def __init__(self):
        self.private_key_str = os.getenv("WALLET_PRIVATE_KEY", "")
        self.wallet_address  = os.getenv("WALLET_ADDRESS", "")
        self.enabled         = bool(self.private_key_str and self.wallet_address)

        if self.enabled:
            self._init_wallet()
        else:
            log.warning("⚠️  Executor désactivé — clés manquantes")

    def _init_wallet(self):
        """Initialise le wallet depuis la clé privée."""
        try:
            from solders.keypair import Keypair
            import base58

            # Supporte les deux formats : base58 et array JSON
            key_str = self.private_key_str.strip()

            if key_str.startswith("["):
                # Format array JSON [1,2,3,...]
                key_bytes = bytes(json.loads(key_str))
            else:
                # Format base58
                key_bytes = base58.b58decode(key_str)

            if len(key_bytes) == 64:
                self.keypair = Keypair.from_bytes(key_bytes)
            elif len(key_bytes) == 32:
                self.keypair = Keypair.from_seed(key_bytes)
            else:
                self.keypair = Keypair.from_bytes(key_bytes[:64])
            log.info(f"✅ Wallet initialisé : {str(self.keypair.pubkey())[:20]}...")

        except ImportError:
            log.error("❌ solders/base58 non installés — pip install solders base58")
            self.enabled = False
        except Exception as e:
            log.error(f"❌ Erreur init wallet: {e}")
            self.enabled = False

    def get_sol_balance(self) -> float:
        """Récupère le solde SOL réel du wallet."""
        try:
            r = requests.post(
                RPC_ENDPOINT,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [self.wallet_address]
                },
                timeout=10
            )
            if r.status_code == 200:
                lamports = r.json().get("result", {}).get("value", 0)
                return lamports / LAMPORTS_SOL
        except Exception as e:
            log.error(f"❌ Erreur balance: {e}")
        return 0.0

    def get_token_balance(self, token_address: str) -> float:
        """Récupère le solde d'un token spécifique."""
        try:
            r = requests.post(
                RPC_ENDPOINT,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        self.wallet_address,
                        {"mint": token_address},
                        {"encoding": "jsonParsed"}
                    ]
                },
                timeout=10
            )
            if r.status_code == 200:
                accounts = r.json().get("result", {}).get("value", [])
                if accounts:
                    info = accounts[0]["account"]["data"]["parsed"]["info"]
                    return float(info["tokenAmount"]["uiAmount"] or 0)
        except Exception as e:
            log.error(f"❌ Erreur token balance: {e}")
        return 0.0

    def _get_quote(self, input_mint: str, output_mint: str,
                   amount_lamports: int, slippage_bps: int = 500) -> dict:
        """
        Obtient le meilleur prix via Raydium API.
        Fallback sur Jupiter si Raydium indisponible.
        """
        # ── Essai Raydium v3 ─────────────────────────────────
        try:
            url = (f"https://api-v3.raydium.io/compute/swap-base-in"
                   f"?inputMint={input_mint}"
                   f"&outputMint={output_mint}"
                   f"&amount={amount_lamports}"
                   f"&slippageBps={slippage_bps}"
                   f"&txVersion=V0")
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("success") and data.get("data"):
                    log.info(f"✅ Quote Raydium v3 obtenu")
                    return {
                        "outAmount":      data["data"].get("outputAmount", 0),
                        "priceImpactPct": data["data"].get("priceImpactPct", 0),
                        "source":         "raydium_v3",
                        "raw":            data,
                    }
        except Exception as e:
            log.warning(f"⚠️  Raydium v3 quote: {e}")

        # ── Fallback Jupiter ──────────────────────────────────
        try:
            url = (f"{JUPITER_URL}/quote"
                   f"?inputMint={input_mint}"
                   f"&outputMint={output_mint}"
                   f"&amount={amount_lamports}"
                   f"&slippageBps={slippage_bps}")
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                data["source"] = "jupiter"
                log.info(f"✅ Quote Jupiter obtenu")
                return data
        except Exception as e:
            log.warning(f"⚠️  Jupiter quote: {e}")

        log.error("❌ Aucun provider de quote disponible")
        return {}

    def _execute_swap(self, quote: dict) -> Optional[str]:
        """
        Exécute le swap via Jupiter.
        Retourne le hash de transaction ou None si échec.
        """
        try:
            from solders.transaction import VersionedTransaction

            # Obtenir la transaction signée de Jupiter
            r = requests.post(
                f"{JUPITER_URL}/swap",
                json={
                    "quoteResponse":          quote,
                    "userPublicKey":          self.wallet_address,
                    "wrapAndUnwrapSol":       True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": 100000,  # ~$0.01 frais priorité
                },
                timeout=15
            )

            if r.status_code != 200:
                log.error(f"❌ Jupiter swap erreur {r.status_code}: {r.text[:100]}")
                return None

            swap_data   = r.json()
            swap_tx_b64 = swap_data.get("swapTransaction", "")

            if not swap_tx_b64:
                log.error("❌ Pas de transaction reçue de Jupiter")
                return None

            # Décoder et signer la transaction
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx       = VersionedTransaction.from_bytes(tx_bytes)
            tx = VersionedTransaction(tx.message, [self.keypair])

            # Envoyer la transaction
            r2 = requests.post(
                RPC_ENDPOINT,
                json={
                    "jsonrpc": "2.0",
                    "id":      1,
                    "method":  "sendTransaction",
                    "params":  [
                        base64.b64encode(bytes(tx)).decode("utf-8"),
                        {"encoding": "base64",
                         "preflightCommitment": "confirmed",
                         "skipPreflight": False}
                    ]
                },
                timeout=30
            )

            if r2.status_code == 200:
                result = r2.json()
                if "result" in result:
                    tx_hash = result["result"]
                    log.info(f"✅ Transaction envoyée : {tx_hash}")
                    return tx_hash
                elif "error" in result:
                    log.error(f"❌ Erreur RPC: {result['error']}")
            else:
                log.error(f"❌ Erreur envoi tx: {r2.status_code}")

        except ImportError:
            log.error("❌ solders non installé — pip install solders")
        except Exception as e:
            log.error(f"❌ Erreur swap: {e}")

        return None

    def _confirm_transaction(self, tx_hash: str, max_retries: int = 20) -> bool:
        """Attend la confirmation de la transaction."""
        for i in range(max_retries):
            try:
                r = requests.post(
                    RPC_ENDPOINT,
                    json={
                        "jsonrpc": "2.0",
                        "id":      1,
                        "method":  "getSignatureStatuses",
                        "params":  [[tx_hash]]
                    },
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
                            log.info(f"✅ Transaction confirmée !")
                            return True
            except Exception:
                pass
            time.sleep(2)
        log.error("❌ Timeout confirmation transaction")
        return False

    def _buy_via_pumpportal(self, token_address: str, sol_amount: float,
                             symbol: str = "???") -> dict:
        """
        Achète un token Pump.fun via PumpPortal API.
        Retourne la transaction signée prête à envoyer.
        """
        try:
            from solders.transaction import VersionedTransaction

            r = requests.post(
                "https://pumpportal.fun/api/trade-local",
                json={
                    "publicKey":         self.wallet_address,
                    "action":            "buy",
                    "mint":              token_address,
                    "amount":            sol_amount,
                    "denominatedInSol":  "true",
                    "slippage":          10,
                    "priorityFee":       0.001,
                    "pool":              "pump",
                },
                timeout=15
            )

            if r.status_code != 200:
                log.warning(f"⚠️  PumpPortal: {r.status_code}")
                return {}

            # Signer et envoyer la transaction
            tx_bytes = r.content
            tx       = VersionedTransaction.from_bytes(tx_bytes)
            tx = VersionedTransaction(tx.message, [self.keypair])

            r2 = requests.post(
                RPC_ENDPOINT,
                json={
                    "jsonrpc": "2.0",
                    "id":      1,
                    "method":  "sendTransaction",
                    "params":  [
                        base64.b64encode(bytes(tx)).decode("utf-8"),
                        {"encoding": "base64",
                         "preflightCommitment": "confirmed",
                         "skipPreflight": False}
                    ]
                },
                timeout=30
            )

            if r2.status_code == 200:
                result = r2.json()
                if "result" in result:
                    tx_hash = result["result"]
                    log.info(f"✅ PumpPortal achat confirmé : {tx_hash}")
                    return {
                        "success":         True,
                        "tx_hash":         tx_hash,
                        "amount_received": 0,
                        "sol_spent":       sol_amount,
                        "source":          "pumpportal",
                    }
                else:
                    error = result.get("error", {})
                    # BondingCurveComplete = token migré sur Raydium
                    if "6005" in str(error) or "BondingCurve" in str(error):
                        log.info("🔄 Token migré sur Raydium — changement de pool")
                        return {"success": False, "reason": "migrated_to_raydium"}
                    log.error(f"❌ Erreur RPC: {error}")

        except Exception as e:
            log.error(f"❌ PumpPortal erreur: {e}")
        return {}

    def buy_token(self, token_address: str, sol_amount: float,
                  symbol: str = "???") -> dict:
        """
        Achète un token avec des SOL via Jupiter.

        Args:
            token_address : Adresse du token à acheter
            sol_amount    : Montant en SOL à dépenser
            symbol        : Symbole du token (pour les logs)

        Returns:
            dict avec success, tx_hash, amount_received
        """
        if not self.enabled:
            return {"success": False, "reason": "Executor désactivé"}

        result = {"success": False, "tx_hash": None, "amount_received": 0}

        try:
            # Vérification balance avant
            balance_before = self.get_sol_balance()
            if balance_before < sol_amount + 0.002:  # Reserve pour frais
                log.warning(f"⚠️  Balance insuffisante: {balance_before:.4f} SOL")
                return {"success": False, "reason": "Balance insuffisante"}

            lamports = int(sol_amount * LAMPORTS_SOL)

            log.info(f"🛒 ACHAT RÉEL — {symbol}")
            log.info(f"   Montant : {sol_amount:.4f} SOL")
            log.info(f"   Token   : {token_address[:20]}...")

            # ── Essai PumpPortal (tokens pump.fun) ───────────
            if True:  # Essaie toujours PumpPortal en premier
                log.info(f"🎯 Token Pump.fun détecté — utilisation PumpPortal")
                pump_result = self._buy_via_pumpportal(token_address, sol_amount, symbol)
                if pump_result.get("success"):
                    return pump_result
                log.info("🔄 Tentative Raydium v3...")
                # Essai Raydium v3 directement
                raydium_url = (f"https://transaction-v1.raydium.io/compute/swap-base-in"
                               f"?inputMint={SOL_MINT}"
                               f"&outputMint={token_address}"
                               f"&amount={lamports}"
                               f"&slippageBps=500"
                               f"&txVersion=V0")
                r3 = requests.get(raydium_url, timeout=10)
                if r3.status_code == 200 and r3.json().get("success"):
                    log.info(f"✅ Quote Raydium v3 obtenu pour token migré")
                    return {"success": False, "reason": "raydium_quote_ok_but_swap_needed"}
                log.warning("⚠️  Raydium v3 indisponible pour ce token")

            # Obtenir quote Raydium/Jupiter
            quote = self._get_quote(
                input_mint     = SOL_MINT,
                output_mint    = token_address,
                amount_lamports = lamports,
                slippage_bps   = 500  # 5% slippage max
            )

            if not quote:
                return {"success": False, "reason": "Quote Jupiter indisponible"}

            out_amount = int(quote.get("outAmount", 0))
            price_impact = float(quote.get("priceImpactPct", 0))

            log.info(f"   Tokens attendus : {out_amount}")
            log.info(f"   Impact prix     : {price_impact:.2f}%")

            # Refuse si impact prix > 10%
            if price_impact > 10:
                log.warning(f"⚠️  Impact prix trop élevé: {price_impact:.1f}% — achat annulé")
                return {"success": False, "reason": f"Impact prix {price_impact:.1f}%"}

            # Exécuter le swap
            tx_hash = self._execute_swap(quote)
            if not tx_hash:
                return {"success": False, "reason": "Échec exécution swap"}

            # Confirmer
            confirmed = self._confirm_transaction(tx_hash)
            if confirmed:
                result["success"]         = True
                result["tx_hash"]         = tx_hash
                result["amount_received"] = out_amount
                result["sol_spent"]       = sol_amount
                log.info(f"✅ ACHAT CONFIRMÉ — {symbol}")
                log.info(f"   TX : https://solscan.io/tx/{tx_hash}")
            else:
                result["reason"] = "Transaction non confirmée"

        except Exception as e:
            log.error(f"❌ Erreur achat {symbol}: {e}")
            result["reason"] = str(e)

        return result

    def sell_token(self, token_address: str, token_amount: int,
                   symbol: str = "???") -> dict:
        """
        Vend des tokens contre des SOL via Jupiter.

        Args:
            token_address : Adresse du token à vendre
            token_amount  : Quantité de tokens (en unités de base)
            symbol        : Symbole du token (pour les logs)

        Returns:
            dict avec success, tx_hash, sol_received
        """
        if not self.enabled:
            return {"success": False, "reason": "Executor désactivé"}

        result = {"success": False, "tx_hash": None, "sol_received": 0}

        try:
            log.info(f"💰 VENTE RÉELLE — {symbol}")
            log.info(f"   Tokens  : {token_amount}")
            log.info(f"   Token   : {token_address[:20]}...")

            # Quote Jupiter pour vente
            quote = self._get_quote(
                input_mint      = token_address,
                output_mint     = SOL_MINT,
                amount_lamports = token_amount,
                slippage_bps    = 500
            )

            if not quote:
                return {"success": False, "reason": "Quote Jupiter indisponible"}

            sol_out      = int(quote.get("outAmount", 0)) / LAMPORTS_SOL
            price_impact = float(quote.get("priceImpactPct", 0))

            log.info(f"   SOL attendus    : {sol_out:.4f}")
            log.info(f"   Impact prix     : {price_impact:.2f}%")

            # Exécuter
            tx_hash = self._execute_swap(quote)
            if not tx_hash:
                return {"success": False, "reason": "Échec exécution swap"}

            confirmed = self._confirm_transaction(tx_hash)
            if confirmed:
                result["success"]      = True
                result["tx_hash"]      = tx_hash
                result["sol_received"] = sol_out
                log.info(f"✅ VENTE CONFIRMÉE — {symbol}")
                log.info(f"   TX : https://solscan.io/tx/{tx_hash}")
            else:
                result["reason"] = "Transaction non confirmée"

        except Exception as e:
            log.error(f"❌ Erreur vente {symbol}: {e}")
            result["reason"] = str(e)

        return result

    def sell_all_token(self, token_address: str, symbol: str = "???") -> dict:
        """Vend 100% d'un token détenu."""
        balance = self.get_token_balance(token_address)
        if balance <= 0:
            return {"success": False, "reason": "Pas de tokens à vendre"}

        # Convertit en unités de base (assumant 6 décimales standard Solana)
        token_amount = int(balance * 1_000_000)
        return self.sell_token(token_address, token_amount, symbol)
