"""Broker factory — 從環境變數 + YAML 建出每個類股的 broker 實例。

讀取順序：
  1. 環境變數（BROKER_MODE / SHIOAJI_* / ALLOWED_SECTORS）
  2. data/broker_config.yaml（風控閾值；缺檔用預設值）

設計原則：
- SinopacBroker 透過延遲 import；dev 機沒裝 shioaji 也能正常跑（會 fallback 為 virtual）
- 任何 simulation 解析疑慮一律當作 simulation=True（defense-in-depth）
- 對 ALLOWED_SECTORS 做嚴格白名單
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from .base import Broker
from .risk_gate import RiskConfig, RiskGate
from .state_store import BrokerStateStore
from .virtual import VirtualBroker

logger = logging.getLogger(__name__)

# ── 全域單例（每進程一份）──
_state_store_singleton: Optional[BrokerStateStore] = None


def _data_dir() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data")
    )


def _broker_state_path() -> str:
    return os.path.join(_data_dir(), "broker_state.json")


def _broker_config_path() -> str:
    return os.path.join(_data_dir(), "broker_config.yaml")


def get_state_store() -> BrokerStateStore:
    global _state_store_singleton
    if _state_store_singleton is None:
        _state_store_singleton = BrokerStateStore(_broker_state_path())
    return _state_store_singleton


# ── env 與 yaml ──

def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; %s ignored", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Failed to load %s: %s", path, e)
        return {}


def _is_truthy(s: Optional[str]) -> bool:
    """嚴格 truthy 解析：只有明確的 "true"/"1"/"yes" 才回 True。"""
    if not s:
        return False
    return s.strip().lower() in ("true", "1", "yes", "on")


def load_risk_config() -> RiskConfig:
    """從 YAML 讀風控閾值；缺值用 RiskConfig 的 dataclass 預設。"""
    raw = _load_yaml(_broker_config_path())
    risk = (raw.get("risk") or {}) if isinstance(raw, dict) else {}
    cfg = RiskConfig()
    for field_name in cfg.__dataclass_fields__:
        if field_name in risk:
            try:
                setattr(cfg, field_name, type(getattr(cfg, field_name))(risk[field_name]))
            except (TypeError, ValueError):
                logger.warning("Invalid risk.%s in broker_config.yaml; using default", field_name)
    return cfg


def get_allowed_sectors_for_sinopac() -> set[str]:
    """環境變數 ALLOWED_SECTORS（CSV）；若空 → broker_config.yaml 的 brokers.per_sector；都沒 → 空 set。

    僅當 BROKER_MODE=sinopac 才有意義。
    """
    env = os.environ.get("ALLOWED_SECTORS", "").strip()
    if env:
        return {s.strip() for s in env.split(",") if s.strip()}
    raw = _load_yaml(_broker_config_path())
    per_sector = ((raw.get("brokers") or {}).get("per_sector") or {})
    return {sid for sid, b in per_sector.items() if str(b).lower() == "sinopac"}


# ── 主要對外函式 ──

def build_broker_for_sector(sector_id: str) -> Broker:
    """依環境變數決定該 sector 用哪個 broker。

    決策樹：
      - BROKER_MODE != "sinopac" → VirtualBroker（dev 機預設）
      - sector_id 不在 ALLOWED_SECTORS → VirtualBroker（白名單外強制虛擬）
      - SHIOAJI_API_KEY/SECRET_KEY/PERSON_ID 任一缺 → VirtualBroker + warning
      - shioaji 套件 import 失敗 → VirtualBroker + warning
      - 任何疑慮 → simulation=True
    """
    mode = os.environ.get("BROKER_MODE", "virtual").strip().lower()
    if mode != "sinopac":
        return VirtualBroker()

    if sector_id not in get_allowed_sectors_for_sinopac():
        logger.info("sector %s 不在 ALLOWED_SECTORS 白名單 → VirtualBroker", sector_id)
        return VirtualBroker()

    # 取 credentials
    api_key = os.environ.get("SHIOAJI_API_KEY", "").strip()
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "").strip()
    person_id = os.environ.get("SHIOAJI_PERSON_ID", "").strip()
    if not (api_key and secret_key and person_id):
        logger.warning(
            "Sinopac credentials 缺漏（API_KEY/SECRET_KEY/PERSON_ID 任一空）"
            " → 該 sector fallback 為 VirtualBroker"
        )
        return VirtualBroker()

    # simulation 嚴格解析（defense-in-depth）：
    # 只有「完全小寫的 false」才會關掉模擬模式；任何 typo（"FALSE"/"0"/"off"/空字串）都當 True
    raw_sim = os.environ.get("SHIOAJI_SIMULATION", "true")
    simulation = raw_sim.strip() != "false"
    if not simulation:
        # 額外確認：必須同時提供 CA path 才允許正式環境（v1 應該永遠走不到這裡）
        ca_path = os.environ.get("SHIOAJI_CA_PATH", "").strip()
        ca_pwd = os.environ.get("SHIOAJI_CA_PASSWORD", "").strip()
        if not (ca_path and ca_pwd and os.path.exists(ca_path)):
            logger.warning(
                "SHIOAJI_SIMULATION=false 但 CA 設定不完整 → 強制改回 simulation=True"
            )
            simulation = True

    try:
        from .sinopac import SinopacBroker
    except Exception as e:
        logger.warning("Failed to import SinopacBroker (%s); fallback to VirtualBroker", e)
        return VirtualBroker()

    try:
        return SinopacBroker(
            api_key=api_key,
            secret_key=secret_key,
            person_id=person_id,
            simulation=simulation,
            ca_path=os.environ.get("SHIOAJI_CA_PATH") if not simulation else None,
            ca_password=os.environ.get("SHIOAJI_CA_PASSWORD") if not simulation else None,
            state_store=get_state_store(),
        )
    except Exception as e:
        logger.exception("SinopacBroker init failed (%s); fallback to VirtualBroker", e.__class__.__name__)
        return VirtualBroker()


@dataclass
class BrokerSetup:
    """sector_auto_trader 啟動時取得的整套 broker 配置。"""

    state_store: BrokerStateStore
    risk_gate: RiskGate
    brokers_by_sector: dict[str, Broker] = field(default_factory=dict)


def build_setup(
    *,
    sector_ids: list[str],
    equity_provider: Callable[[str], tuple[float, float]],
    position_provider: Callable[[str, str], dict],
    initial_balance_provider: Callable[[str], float],
) -> BrokerSetup:
    """一次建好整套 broker 配置（呼叫端：sector_auto_trader.start()）。"""
    state = get_state_store()
    cfg = load_risk_config()
    gate = RiskGate(
        config=cfg,
        state_store=state,
        equity_provider=equity_provider,
        position_provider=position_provider,
        initial_balance_provider=initial_balance_provider,
    )
    brokers = {sid: build_broker_for_sector(sid) for sid in sector_ids}
    return BrokerSetup(state_store=state, risk_gate=gate, brokers_by_sector=brokers)
