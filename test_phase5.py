import os
import time
import subprocess
from crypto_trader.infra.redis_streams import RedisStreamBus
from crypto_trader.execution.signal_bus import SignalPublisher, Signal

def main():
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6382/0")
    db_url = os.environ.get("DATABASE_URL", "postgresql://trader:trader@localhost:5434/crypto_trader")
    bus = RedisStreamBus.from_url(redis_url)
    publisher = SignalPublisher(bus)
    
    print("Publishing valid synthetic signal...")
    signal = Signal(strategy_id="SMOKE", symbol="BTCUSDT", side="LONG", quantity=1.0, mode="paper", price=100.0)
    publisher.emit(signal)
    
    print("Waiting 5s for worker to process...")
    time.sleep(5)
    
    print("Checking Postgres rows...")
    output = subprocess.check_output(f'psql "{db_url}" -c "SELECT * FROM active_positions; SELECT * FROM orders; SELECT * FROM fills;"', shell=True)
    print(output.decode('utf-8'))
    
    print("Publishing DUPLICATE signal...")
    publisher.emit(signal)
    time.sleep(2)
    output2 = subprocess.check_output(f'psql "{db_url}" -c "SELECT count(*) FROM orders;"', shell=True)
    print("Orders count after duplicate:")
    print(output2.decode('utf-8'))
    
    print("Publishing MALFORMED signal...")
    # Malformed means maybe a bad JSON or invalid mode. The Signal class expects strict fields.
    # To bypass Pydantic validation (if it exists) and send malformed directly to bus:
    # Actually, we can just send a valid Signal object with an invalid mode?
    try:
        bad_signal = Signal(strategy_id="SMOKE", symbol="BTCUSDT", side="LONG", quantity=1.0, mode="UNKNOWN", price=100.0)
        publisher.emit(bad_signal)
    except Exception as e:
        print(f"Failed to create bad signal: {e}")
        # Try pushing raw to bus
        bus.publish("execution:signals", {"raw": "bad_data"})
        
    time.sleep(2)
    print("Checking DLQ...")
    # DLQ is execution:signals:dlq
    # redis-cli -p 6382 XRANGE execution:signals:dlq - +
    try:
        dlq = subprocess.check_output("redis-cli -p 6382 XRANGE execution:signals:dlq - +", shell=True)
        print(dlq.decode('utf-8'))
    except Exception as e:
        print(f"Error checking DLQ: {e}")

if __name__ == "__main__":
    main()
