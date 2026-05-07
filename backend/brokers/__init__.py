"""券商抽象層

VirtualBroker 與 SinopacBroker 共用同一介面（base.Broker），由 factory 依環境變數選擇。

Factory 採延遲 import（避免在沒有 shioaji 套件的 dev 機 import brokers 時就崩潰）。
"""

from .base import Broker, BrokerResult, RiskDecision

__all__ = ["Broker", "BrokerResult", "RiskDecision"]
