import json
import os

fp = "data/btc_trading_account.json"
if os.path.exists(fp):
    with open(fp, 'r') as f:
        data = json.load(f)
    if "holdings" in data and "BTC/USDT" in data["holdings"]:
        hold = data["holdings"].get("BTC/USDT")
        strat_id = hold.get("buy_strategies", "S1").split(",")[0]
        data["holdings"][f"BTC/USDT_{strat_id}"] = hold
        del data["holdings"]["BTC/USDT"]
        with open(fp, 'w') as f:
            json.dump(data, f, indent=4)
        print(f"Migrated BTC/USDT to BTC/USDT_{strat_id}")
    else:
        print("No migration needed.")
