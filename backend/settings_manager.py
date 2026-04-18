import json
import os
import yfinance as yf

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

def _load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "telegram": {
            "chat_ids": ""
        },
        "custom_stocks": [] # {"symbol": "2330.TW", "name": "台積電", "sector": "半導體"}
    }

def _save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

def get_settings():
    return _load_settings()

def update_telegram_settings(chat_ids: str):
    settings = _load_settings()
    settings.setdefault("telegram", {})["chat_ids"] = chat_ids
    _save_settings(settings)
    return settings

def add_custom_stock(symbol: str, name: str, sector: str):
    settings = _load_settings()
    custom_stocks = settings.setdefault("custom_stocks", [])
    
    # Check if exists
    for stock in custom_stocks:
        if stock["symbol"] == symbol:
            stock["name"] = name
            stock["sector"] = sector
            _save_settings(settings)
            _sync_to_sector_trader(symbol, name, sector)
            return settings
            
    custom_stocks.append({
        "symbol": symbol,
        "name": name,
        "sector": sector
    })
    _save_settings(settings)
    _sync_to_sector_trader(symbol, name, sector)
    return settings
    
def _sync_to_sector_trader(symbol, name, sector_name):
    # This will inject the stock into sector_trader
    try:
        from sector_trader import sector_managers, SECTOR_STOCKS, SECTOR_IDS
        if sector_name in SECTOR_STOCKS:
            SECTOR_STOCKS[sector_name][symbol] = name
        
        # update manager specifically
        if sector_name in SECTOR_IDS:
            manager = sector_managers.get(SECTOR_IDS[sector_name])
            if manager:
                manager.stocks[symbol] = name
                if symbol not in manager.state.get("stocks", []):
                    manager.state.setdefault("stocks", []).append(symbol)
                    manager._save()
    except Exception as e:
        print(f"Failed to sync stock to sector_trader: {e}")

# Inject loaded config to os environ for fallback if needed
settings = _load_settings()
if settings.get("telegram", {}).get("chat_ids"):
    os.environ["TELEGRAM_CHAT_ID"] = settings["telegram"]["chat_ids"]

