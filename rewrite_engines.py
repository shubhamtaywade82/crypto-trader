import os

def rewrite_websocket_py():
    path = "/home/nemesis/project/trading-workspace/coindcx/crypto-trader/crypto_trader/websocket.py"
    with open(path, "r") as f:
        code = f.read()

    code = code.replace("self.wallet.get_open_position()", "self.wallet.get_open_position(self.symbol)")
    code = code.replace("self.wallet.close_position(data.mark_price", "self.wallet.close_position(self.symbol, data.mark_price")
    code = code.replace("self.wallet.close_position(price", "self.wallet.close_position(self.symbol, price")
    code = code.replace("self.wallet.close_position(price_sl_tp", "self.wallet.close_position(self.symbol, price_sl_tp")
    code = code.replace("self.wallet.close_position(price_trail", "self.wallet.close_position(self.symbol, price_trail")
    code = code.replace("self.wallet.partial_close(price_sl_tp", "self.wallet.partial_close(self.symbol, price_sl_tp")
    
    with open(path, "w") as f:
        f.write(code)

def rewrite_engine_ws_py():
    path = "/home/nemesis/project/trading-workspace/coindcx/crypto-trader/crypto_trader/engine_ws.py"
    with open(path, "r") as f:
        code = f.read()

    # update __init__
    code = code.replace("        log_llm: bool = False,\n    ):", "        log_llm: bool = False,\n        wallet = None,\n    ):")
    code = code.replace("        self.wallet = EnhancedFuturesWallet(\n            symbol=self.symbol,\n            initial_balance=initial_balance,\n            leverage=leverage,\n        )", "        if wallet:\n            self.wallet = wallet\n        else:\n            self.wallet = EnhancedFuturesWallet(\n                initial_balance=initial_balance,\n                leverage=leverage,\n            )")

    # update wallet calls
    code = code.replace("open_positions = [p.to_dict() for p in self.wallet.positions.values() if p.status == \"OPEN\"]", "pos = self.wallet.get_open_position(self.symbol)\n            open_positions = [pos.to_dict()] if pos else []")
    code = code.replace("if not self.wallet.get_open_position() and can_trade:", "if not self.wallet.get_open_position(self.symbol) and can_trade:")
    code = code.replace("pos = self.wallet.open_position(setup, entry_price, custom_margin=adjusted_margin)", "pos = self.wallet.open_position(self.symbol, setup, entry_price, custom_margin=adjusted_margin)")

    with open(path, "w") as f:
        f.write(code)


def rewrite_multi_engine_py():
    path = "/home/nemesis/project/trading-workspace/coindcx/crypto-trader/crypto_trader/multi_engine.py"
    with open(path, "r") as f:
        code = f.read()

    code = code.replace("from crypto_trader.engine_ws import WebSocketTradingEngine", "from crypto_trader.engine_ws import WebSocketTradingEngine\nfrom crypto_trader.wallet import EnhancedFuturesWallet")
    
    code = code.replace("    threads = []\n\n    for sym in symbols:", "    threads = []\n    global_wallet = EnhancedFuturesWallet(initial_balance=1000.0, leverage=args.leverage)\n\n    for sym in symbols:")
    
    code = code.replace("        engine = WebSocketTradingEngine(\n            symbol=sym,", "        engine = WebSocketTradingEngine(\n            symbol=sym,\n            wallet=global_wallet,")

    with open(path, "w") as f:
        f.write(code)

rewrite_websocket_py()
rewrite_engine_ws_py()
rewrite_multi_engine_py()
print("Done")
