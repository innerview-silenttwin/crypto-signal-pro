"""Quote provider abstraction (yfinance / Sinopac quote API)."""
from .base import QuoteProvider
from .factory import get_quote_provider

__all__ = ["QuoteProvider", "get_quote_provider"]
