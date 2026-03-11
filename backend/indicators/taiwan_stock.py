from .base import Indicator
from .rsi import RSI
from .macd import MACD
from .bollinger import BollingerBands
from .ema import EMA

class TaiwanStockAlgorithm(Indicator):
    """
    台股專屬演算法，根據多種技術指標計算分數。
    """

    def __init__(self, data):
        super().__init__(data)
        self.rsi = RSI(data)
        self.macd = MACD(data)
        self.bollinger = BollingerBands(data)
        self.ema = EMA(data)

    def calculate_score(self):
        """
        計算台股分數，根據技術指標的信號加權計算。
        """
        rsi_score = self._rsi_score()
        macd_score = self._macd_score()
        bollinger_score = self._bollinger_score()
        ema_score = self._ema_score()

        # 總分數加權計算
        total_score = (rsi_score * 0.3 +
                       macd_score * 0.3 +
                       bollinger_score * 0.2 +
                       ema_score * 0.2)
        return total_score

    def _rsi_score(self):
        rsi_value = self.rsi.calculate()
        if rsi_value < 30:
            return 1  # 超賣
        elif rsi_value > 70:
            return -1  # 超買
        return 0

    def _macd_score(self):
        macd_line, signal_line = self.macd.calculate()
        if macd_line > signal_line:
            return 1  # 多頭
        elif macd_line < signal_line:
            return -1  # 空頭
        return 0

    def _bollinger_score(self):
        upper, middle, lower = self.bollinger.calculate()
        price = self.data['close'][-1]
        if price > upper:
            return -1  # 超買
        elif price < lower:
            return 1  # 超賣
        return 0

    def _ema_score(self):
        ema_value = self.ema.calculate()
        price = self.data['close'][-1]
        if price > ema_value:
            return 1  # 多頭
        elif price < ema_value:
            return -1  # 空頭
        return 0