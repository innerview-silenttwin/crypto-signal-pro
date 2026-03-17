
import urllib.request
import ssl
import json

def fetch_stock_name(symbol: str):
    if '.' not in symbol:
        symbol = f"{symbol}.TW"
    # Try different Yahoo Finance API endpoints if one fails
    urls = [
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=price",
        f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
    ]
    
    for url in urls:
        try:
            print(f"Trying {url}...")
            context = ssl._create_unverified_context()
            headers = {'User-Agent': 'Mozilla/5.0'}
            req = urllib.request.Request(url, headers=headers)
            raw = urllib.request.urlopen(req, timeout=15, context=context).read().decode('utf-8')
            payload = json.loads(raw)
            
            if "quoteSummary" in url:
                name = payload.get('quoteSummary', {}).get('result', [{}])[0].get('price', {}).get('longName')
            else:
                name = payload.get('quoteResponse', {}).get('result', [{}])[0].get('longName')
                
            if name:
                return name
        except Exception as e:
            print(f"Error with {url}: {e}")
            
    return None

if __name__ == "__main__":
    print(f"2330: {fetch_stock_name('2330.TW')}")
    print(f"2454: {fetch_stock_name('2454.TW')}")
    print(f"3231: {fetch_stock_name('3231.TW')}")
    print(f"9999 (Invalid): {fetch_stock_name('9999.TW')}")
