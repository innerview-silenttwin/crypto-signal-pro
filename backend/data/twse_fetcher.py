import requests
import pandas as pd
from datetime import datetime

class TWSEFetcher:
    """
    使用台灣證券交易所 (TWSE) API 抓取台股資料。
    """

    BASE_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"

    def fetch_stock_data(self, stock_id: str, year: int, month: int) -> pd.DataFrame:
        """
        從 TWSE API 抓取指定股票的月度資料。

        Args:
            stock_id (str): 股票代碼。
            year (int): 年份。
            month (int): 月份。

        Returns:
            pd.DataFrame: 包含日期、開盤價、收盤價等的 DataFrame。
        """
        params = {
            "response": "json",
            "date": f"{year}{month:02}01",
            "stockNo": stock_id
        }

        response = requests.get(self.BASE_URL, params=params)
        if response.status_code != 200:
            raise Exception(f"API 請求失敗，狀態碼: {response.status_code}")

        data = response.json()
        if data["stat"] != "OK":
            raise Exception(f"無法取得資料: {data['stat']}")

        # 解析資料
        records = data["data"]
        # 動態處理欄位名稱
        columns = data["fields"]  # 使用 API 返回的欄位名稱
        df = pd.DataFrame(records, columns=columns)

        # 處理民國年格式，轉換為西元年
        df["日期"] = df["日期"].apply(lambda x: f"{int(x.split('/')[0]) + 1911}/{x.split('/')[1]}/{x.split('/')[2]}")
        df["日期"] = pd.to_datetime(df["日期"], format="%Y/%m/%d")

        numeric_columns = [col for col in df.columns if col not in ["日期", "漲跌價差"]]
        df[numeric_columns] = df[numeric_columns].replace(",", "", regex=True).apply(pd.to_numeric)

        return df

if __name__ == "__main__":
    # 測試抓取中鋼資料
    fetcher = TWSEFetcher()
    stock_id = "2002"  # 中鋼
    year, month = 2026, 3
    data = fetcher.fetch_stock_data(stock_id, year, month)
    print("中鋼 2026 年 3 月資料:")
    print(data)