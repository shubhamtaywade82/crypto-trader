import re

path = "/home/nemesis/project/trading-workspace/coindcx/crypto-trader/crypto_trader/wallet.py"
with open(path, "r") as f:
    code = f.read()

# 1. Add threading import
code = code.replace("import logging", "import logging\nimport threading")

# 2. Update __init__
init_old = """    def __init__(
        self,
        symbol: str,
        initial_balance: float = 1_000.0,
        leverage: int = 10,
        equity_utilization: float = 0.50,
        catastrophic_sl_pct: float = -0.50,
    ):
        self.symbol = symbol
        self.leverage = leverage
        self.equity_utilization = equity_utilization
        self.catastrophic_sl_pct = catastrophic_sl_pct
        self.state_file = DATA_DIR / f"wallet_{symbol}.json" """

init_new = """    def __init__(
        self,
        initial_balance: float = 1_000.0,
        leverage: int = 10,
        equity_utilization: float = 0.50,
        catastrophic_sl_pct: float = -0.50,
    ):
        self.lock = threading.RLock()
        self.leverage = leverage
        self.equity_utilization = equity_utilization
        self.catastrophic_sl_pct = catastrophic_sl_pct
        self.state_file = DATA_DIR / "wallet_global.json" """

code = code.replace(init_old, init_new)

# 3. Update Properties
code = code.replace("    @property\n    def margin_balance(self) -> float:\n        return self.wallet_balance + self.unrealized_pnl_total",
                    "    @property\n    def margin_balance(self) -> float:\n        with self.lock:\n            return self.wallet_balance + self.unrealized_pnl_total")

code = code.replace("    @property\n    def available_balance(self) -> float:\n        used = sum(p.margin_used for p in self.positions.values() if p.status == \"OPEN\")\n        return max(0.0, self.margin_balance - used)",
                    "    @property\n    def available_balance(self) -> float:\n        with self.lock:\n            used = sum(p.margin_used for p in self.positions.values() if p.status == \"OPEN\")\n            return max(0.0, self.margin_balance - used)")

# 4. get_open_position
old_get = """    def get_open_position(self) -> Optional[EnhancedPosition]:
        pos = self.positions.get(self.symbol)
        return pos if pos and pos.status == "OPEN" else None"""
new_get = """    def get_open_position(self, symbol: str) -> Optional[EnhancedPosition]:
        with self.lock:
            pos = self.positions.get(symbol)
            return pos if pos and pos.status == "OPEN" else None"""
code = code.replace(old_get, new_get)

# 5. can_open
old_can = """    def can_open(self) -> Tuple[bool, str]:
        if self.get_open_position():
            return False, f"Already have open position on {self.symbol}"
        if self.available_balance <= 0:
            return False, "No available balance"
        return True, "OK" """
new_can = """    def can_open(self, symbol: str) -> Tuple[bool, str]:
        with self.lock:
            if self.get_open_position(symbol):
                return False, f"Already have open position on {symbol}"
            if self.available_balance <= 0:
                return False, "No available balance"
            return True, "OK" """
code = code.replace(old_can, new_can)

# 6. open_position
code = code.replace("def open_position(\n        self,\n        setup: dict,", "def open_position(\n        self,\n        symbol: str,\n        setup: dict,")
code = code.replace("can, reason = self.can_open()", "with self.lock:\n            can, reason = self.can_open(symbol)")
code = code.replace("self.symbol", "symbol")
# Fix indentation for open_position
lines = code.split('\n')
in_open = False
for i, line in enumerate(lines):
    if line.startswith("    def open_position("):
        in_open = True
    elif in_open and line.startswith("    def partial_close("):
        in_open = False
    elif in_open and line.startswith("            can, reason = self.can_open(symbol)"):
        pass # already indented
    elif in_open and line.startswith("        ") and not line.startswith("        self,") and not line.startswith("        symbol: str,") and not line.startswith("        setup: dict,") and not line.startswith("        mark_price: float,") and not line.startswith("        custom_margin: Optional[float] = None,") and not line.startswith("    ) -> Optional[EnhancedPosition]:") and not line.startswith('        """Open a position'):
        lines[i] = "    " + line # add 4 spaces for the 'with self.lock:' block
code = '\n'.join(lines)
code = code.replace('        """Open a position. custom_margin overrides equity_utilization for LLM-adjusted sizing."""\n        with self.lock:\n', '        """Open a position. custom_margin overrides equity_utilization for LLM-adjusted sizing."""\n        with self.lock:\n')

# 7. partial_close
code = code.replace("def partial_close(self, mark_price: float, pct: float, reason: str) -> float:", "def partial_close(self, symbol: str, mark_price: float, pct: float, reason: str) -> float:\n        with self.lock:")
# indent partial close
lines = code.split('\n')
in_partial = False
for i, line in enumerate(lines):
    if line.startswith("    def partial_close("):
        in_partial = True
    elif in_partial and line.startswith("    def close_position("):
        in_partial = False
    elif in_partial and line.startswith("        ") and not line.startswith("        with self.lock:"):
        lines[i] = "    " + line
code = '\n'.join(lines)
code = code.replace("self.get_open_position()", "self.get_open_position(symbol)")
code = code.replace("self.close_position(mark_price", "self.close_position(symbol, mark_price")

# 8. close_position
code = code.replace("def close_position(self, mark_price: float, reason: str) -> Optional[EnhancedPosition]:", "def close_position(self, symbol: str, mark_price: float, reason: str) -> Optional[EnhancedPosition]:\n        with self.lock:")
lines = code.split('\n')
in_close = False
for i, line in enumerate(lines):
    if line.startswith("    def close_position("):
        in_close = True
    elif in_close and line.startswith("    def update_positions("):
        in_close = False
    elif in_close and line.startswith("        ") and not line.startswith("        with self.lock:"):
        lines[i] = "    " + line
code = '\n'.join(lines)
code = code.replace("self.get_open_position()", "self.get_open_position(symbol)")

# 9. update_positions
code = code.replace("def update_positions(\n        self,\n        mark_price: float,", "def update_positions(\n        self,\n        symbol: str,\n        mark_price: float,")
code = code.replace('        """Update PnL and check all exit conditions."""\n        pos = self.get_open_position(symbol)', '        """Update PnL and check all exit conditions."""\n        with self.lock:\n            pos = self.get_open_position(symbol)')
lines = code.split('\n')
in_update = False
for i, line in enumerate(lines):
    if line.startswith("    def update_positions("):
        in_update = True
    elif in_update and line.startswith("    def _check_intraday_exits("):
        in_update = False
    elif in_update and line.startswith("        ") and not line.startswith("        self,") and not line.startswith("        symbol: str,") and not line.startswith("        mark_price: float,") and not line.startswith("        candle_close_time: int,") and not line.startswith("        ema9_1h: Optional[float] = None,") and not line.startswith("    ):") and not line.startswith('        """Update PnL') and not line.startswith("        with self.lock:"):
        lines[i] = "    " + line
code = '\n'.join(lines)
code = code.replace("self.close_position(mark_price", "self.close_position(symbol, mark_price")
code = code.replace("self._check_intraday_exits(pos", "self._check_intraday_exits(symbol, pos")
code = code.replace("self._check_swing_exits(pos", "self._check_swing_exits(symbol, pos")

# 10. check exits
code = code.replace("def _check_intraday_exits(self, pos: EnhancedPosition, mark_price: float, candle_close_time: int):", "def _check_intraday_exits(self, symbol: str, pos: EnhancedPosition, mark_price: float, candle_close_time: int):")
code = code.replace("def _check_swing_exits(self, pos: EnhancedPosition, mark_price: float, candle_close_time: int, ema9_1h: Optional[float]):", "def _check_swing_exits(self, symbol: str, pos: EnhancedPosition, mark_price: float, candle_close_time: int, ema9_1h: Optional[float]):")
code = code.replace("self.partial_close(mark_price", "self.partial_close(symbol, mark_price")


# 11. print_summary
code = code.replace("CRYPTO TRADER v4 — WALLET SUMMARY ({self.symbol})", "CRYPTO TRADER v4 — WALLET SUMMARY (GLOBAL)")
code = code.replace("def get_summary(self) -> dict:\n        open_pos", "def get_summary(self) -> dict:\n        with self.lock:\n            open_pos")
lines = code.split('\n')
in_get_sum = False
for i, line in enumerate(lines):
    if line.startswith("    def get_summary("):
        in_get_sum = True
    elif in_get_sum and line.startswith("    def print_summary("):
        in_get_sum = False
    elif in_get_sum and line.startswith("        ") and not line.startswith("        with self.lock:"):
        lines[i] = "    " + line
code = '\n'.join(lines)

with open(path, "w") as f:
    f.write(code)
print("Done")
