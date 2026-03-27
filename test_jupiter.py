import requests
try:
    r = requests.get(
        "https://quote-api.jup.ag/v6/quote?inputMint=So11111111111111111111111111111111111111112&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=37100000&slippageBps=500",
        timeout=10
    )
    print(f"Status: {r.status_code}")
    print(f"Jupiter accessible: {r.status_code == 200}")
except Exception as e:
    print(f"Erreur: {e}")
