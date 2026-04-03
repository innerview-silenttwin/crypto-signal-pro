"""
驗證 TWSE T86 / MI_MARGN API 欄位解析是否正確
執行方式：python backend/verify_chip_api.py
"""

import requests
from datetime import datetime, timedelta


def get_recent_trading_date(days_back: int = 0) -> str:
    """往回找最近的交易日（跳過週末）"""
    d = datetime.now() - timedelta(days=days_back)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def fetch_raw(url: str) -> dict:
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        return resp.json()
    except requests.exceptions.SSLError:
        resp = requests.get(url, timeout=15, verify=False, headers={"User-Agent": "Mozilla/5.0"})
        return resp.json()


def verify_t86(date_str: str):
    print(f"\n{'='*60}")
    print(f"【T86 三大法人買賣超】日期: {date_str}")
    print("="*60)

    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALLBUT0999"
    data = fetch_raw(url)

    stat = data.get("stat")
    print(f"stat: {stat}")

    if stat != "OK":
        print("❌ 無資料（非交易日或 API 異常）")
        return False

    fields = data.get("fields", [])
    print(f"\n欄位列表（共 {len(fields)} 欄）：")
    for i, f in enumerate(fields):
        print(f"  [{i}] {f}")

    rows = data.get("data", [])
    print(f"\n總筆數: {len(rows)}")

    # 取台積電 (2330) 當樣本
    sample = None
    for row in rows:
        if row[0].strip() == "2330":
            sample = row
            break

    if sample:
        print(f"\n台積電 (2330) 原始資料：")
        for i, (f, v) in enumerate(zip(fields, sample)):
            print(f"  [{i}] {f} → {v}")
    else:
        print("\n（找不到 2330，顯示第一筆）")
        if rows:
            for i, (f, v) in enumerate(zip(fields, rows[0])):
                print(f"  [{i}] {f} → {v}")

    # 驗證動態欄位解析邏輯
    print("\n【欄位解析驗證】")
    field_map = {}
    for i, f in enumerate(fields):
        fl = f.strip()
        if "代號" in fl or "證券代號" in fl:
            field_map["code"] = i
        elif fl.startswith("外陸資") and ("買賣超" in fl or "淨" in fl):
            field_map["foreign_net"] = i
        elif "投信" in fl and ("買賣超" in fl or "淨" in fl):
            field_map["trust_net"] = i
        elif "自營商" in fl and ("買賣超" in fl or "淨" in fl) and "避險" not in fl and "自行" not in fl:
            field_map["dealer_net"] = i
        elif "三大法人" in fl and ("買賣超" in fl or "淨" in fl):
            field_map["total_net"] = i

    for key, idx in field_map.items():
        print(f"  {key} → [{idx}] {fields[idx]}", "✅" if idx is not None else "❌")

    missing = [k for k in ["code", "foreign_net", "trust_net", "dealer_net", "total_net"] if k not in field_map]
    if missing:
        print(f"\n❌ 未能解析的欄位: {missing}")
    else:
        print("\n✅ 所有欄位解析成功")

    return True


def verify_mi_margn(date_str: str):
    print(f"\n{'='*60}")
    print(f"【MI_MARGN 融資融券】日期: {date_str}")
    print("="*60)

    url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={date_str}&selectType=STOCK"
    data = fetch_raw(url)

    stat = data.get("stat")
    print(f"stat: {stat}")

    if stat != "OK":
        print("❌ 無資料（非交易日或 API 異常）")
        return False

    tables = data.get("tables", [])
    print(f"tables 數量: {len(tables)}")
    for i, t in enumerate(tables):
        print(f"  table[{i}] title: {t.get('title', '(無 title)')}, 筆數: {len(t.get('data', []))}")

    # 找個股資料 table
    stock_table = None
    for table in tables:
        title = table.get("title", "")
        if "融資融券" in title or "個股" in title:
            stock_table = table
            break
    if not stock_table and tables:
        stock_table = tables[-1] if len(tables) > 1 else tables[0]

    if not stock_table or not stock_table.get("data"):
        print("❌ 找不到個股資料 table")
        return False

    fields = stock_table.get("fields", [])
    print(f"\n使用 table: 「{stock_table.get('title', '')}」")
    print(f"欄位列表（共 {len(fields)} 欄）：")
    for i, f in enumerate(fields):
        print(f"  [{i}] {f}")

    rows = stock_table.get("data", [])
    print(f"\n總筆數: {len(rows)}")

    # 找台積電 (2330)
    sample = None
    idx_code = 0
    for row in rows:
        code = str(row[idx_code]).strip().replace('"', '').replace("=", "")
        if code == "2330":
            sample = row
            break

    if sample:
        print(f"\n台積電 (2330) 原始資料：")
        for i, (f, v) in enumerate(zip(fields, sample)):
            print(f"  [{i}] {f} → {v}")
    else:
        print("\n（找不到 2330，顯示第一筆）")
        if rows:
            for i, (f, v) in enumerate(zip(fields, rows[0])):
                print(f"  [{i}] {f} → {v}")

    # 驗證固定欄位索引（chipflow.py 中的假設）
    # 注意：MI_MARGN 欄位名稱是縮寫（無融資/融券前綴），靠位置區分
    # 融資區塊: [2]買進 [3]賣出 [4]現金償還 [5]前日餘額 [6]今日餘額 [7]限額
    # 融券區塊: [8]買進 [9]賣出 [10]現券償還 [11]前日餘額 [12]今日餘額 [13]限額
    print("\n【固定欄位索引驗證（chipflow.py 的假設）】")
    expected = {
        2: ("買進", "融資買進"),
        3: ("賣出", "融資賣出"),
        5: ("前日餘額", "融資前日餘額"),
        6: ("今日餘額", "融資今日餘額"),
        8: ("買進", "融券買進"),
        9: ("賣出", "融券賣出"),
        12: ("今日餘額", "融券今日餘額"),
    }
    all_ok = True
    for idx, (actual_name, semantic) in expected.items():
        if idx < len(fields):
            actual = fields[idx].strip()
            match = actual == actual_name
            status = "✅" if match else "❌ 不符"
            print(f"  [{idx}] {semantic} → 實際「{actual}」 {status}")
            if not match:
                all_ok = False
        else:
            print(f"  [{idx}] {semantic} → 欄位不存在 ❌")
            all_ok = False

    if all_ok:
        print("\n✅ 固定欄位索引完全正確")
    else:
        print("\n❌ 有欄位對應錯誤，需要修正 chipflow.py")

    return True


if __name__ == "__main__":
    # 往回找最近 5 個交易日，找到第一個有資料的
    for days_back in range(0, 10):
        date_str = get_recent_trading_date(days_back)
        print(f"\n嘗試日期: {date_str}（往回第 {days_back} 天）")

        t86_ok = verify_t86(date_str)
        mi_ok = verify_mi_margn(date_str)

        if t86_ok and mi_ok:
            break
        print("\n--- 此日期無完整資料，嘗試前一個交易日 ---")
