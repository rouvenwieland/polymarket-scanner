"""
Plattform-Connectoren für den Multi-Platform-Scan
=====================================================

Jedes Untermodul (polymarket, kalshi, predictit) liefert Märkte in einem
gemeinsamen, normalisierten Schema zurück - eine Liste von dicts:

  {
      "platform": "polymarket" | "kalshi" | "predictit",
      "market_id": str,     # plattform-eindeutige ID
      "question": str,
      "yes_price": float,   # Kaufpreis Yes (USD)
      "no_price": float,    # Kaufpreis No (USD)
      "volume": float,
      "liquidity": float,   # grobe Proxy-Größe, nicht überall verfügbar (dann 0.0)
      "end_date": str|None, # ISO-Format, falls bekannt
      "url": str,
  }

Dieses einheitliche Schema erlaubt dem Cross-Platform-Fonds und der
Übersicht, mit allen Plattformen gleich umzugehen, ohne deren jeweilige
API-Eigenheiten zu kennen.
"""

PLATFORMS = ("polymarket", "kalshi", "predictit")
