# 全域配置
import os
from dataclasses import dataclass, field
from typing import Dict, List

@dataclass
class ExchangeConfig:
    """交易所配置"""
    name: str = "binance"
    rate_limit: int = 1200  # ms between requests

@dataclass 
class IndicatorWeights:
    """指標權重配置（滿分100）"""
    rsi: float = 15.0
    macd: float = 20.0
    bollinger: float = 15.0
    mfi: float = 10.0
    ema_cross: float = 15.0
    volume: float = 15.0
    adx: float = 10.0

    def to_dict(self) -> Dict[str, float]:
        return {
            'rsi': self.rsi,
            'macd': self.macd,
            'bollinger': self.bollinger,
            'mfi': self.mfi,
            'ema_cross': self.ema_cross,
            'volume': self.volume,
            'adx': self.adx,
        }

@dataclass
class SignalConfig:
    """信號等級門檻"""
    extreme_strong: float = 90.0   # 極強信號
    strong: float = 70.0          # 強信號
    moderate: float = 50.0        # 中等信號
    weak: float = 30.0            # 弱信號

@dataclass
class TimeframeConfig:
    """時間框架配置"""
    short_term: List[str] = field(default_factory=lambda: ['5m', '15m', '1h'])
    medium_term: List[str] = field(default_factory=lambda: ['4h', '1d'])
    long_term: List[str] = field(default_factory=lambda: ['1d', '1w'])

@dataclass
class AppConfig:
    """應用程式總配置"""
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    weights: IndicatorWeights = field(default_factory=IndicatorWeights)
    signal: SignalConfig = field(default_factory=SignalConfig)
    timeframe: TimeframeConfig = field(default_factory=TimeframeConfig)
    default_symbols: List[str] = field(default_factory=lambda: [
        'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT'
    ])

# 全域配置實例
config = AppConfig()
