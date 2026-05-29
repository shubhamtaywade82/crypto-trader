"""
Canonical event-type constants for the double-entry ledger.
All ledger_entries.event_type values must be one of these strings.
"""

# ── INR domain ───────────────────────────────────────────────
DEPOSIT             = "DEPOSIT"
WITHDRAWAL          = "WITHDRAWAL"
CONVERSION_DEBIT    = "CONVERSION_DEBIT"    # INR leaves INR account
CONVERSION_CREDIT   = "CONVERSION_CREDIT"   # USDT arrives in USDT account

# ── USDT trading domain ──────────────────────────────────────
MARGIN_RESERVE      = "MARGIN_RESERVE"      # margin locked on order/position open
MARGIN_RELEASE      = "MARGIN_RELEASE"      # margin freed on close
FEE_DEBIT           = "FEE_DEBIT"           # trading / funding fee
FUNDING_DEBIT       = "FUNDING_DEBIT"       # funding rate paid
FUNDING_CREDIT      = "FUNDING_CREDIT"      # funding rate received
REALIZED_PNL_CREDIT = "REALIZED_PNL_CREDIT" # profitable close
REALIZED_PNL_DEBIT  = "REALIZED_PNL_DEBIT"  # losing close

# ── System / operational ─────────────────────────────────────
RECON_ADJUST        = "RECON_ADJUST"        # reconciliation correction
OPENING_BALANCE     = "OPENING_BALANCE"     # initial seeding

# ── Reference-type constants ─────────────────────────────────
REF_ORDER      = "order"
REF_POSITION   = "position"
REF_CONVERSION = "conversion"
REF_FUNDING    = "funding"
REF_RECON      = "reconciliation"
REF_MANUAL     = "manual"
