# Futures Sockets

## Glossary

```
e - is the Event type
p - price (LTP)
q - quantity (trade quantity)
pr - product (futures)
f - futures
s - spot
T - timestamp
m - is maker. Boolean value that would be true if its maker and false if its taker
RT - range timestamp
ts - timestamp
vs - version
Ets - event timestamp as given by TPE (applicable to candlesticks data)
i - Interval
E - event timestamp (applicable to order book data)
pST - price sent time
v - volume 24h
ls - last price
pc - price change percent
btST - TPE Tick send time
mp - mark price
bmST - TPE mark price send time (The timestamp at which Third-Party exchange sent this event)
```

## ACCOUNT

```python
import socketio
import hmac
import hashlib
import json
socketEndpoint = 'wss://stream.coindcx.com'
sio = socketio.Client()

sio.connect(socketEndpoint, transports = 'websocket')

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

body = {"channel":"coindcx"}
json_body = json.dumps(body, separators = (',', ':'))
signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

# Join channel

sio.emit('join', { 'channelName': 'coindcx', 'authSignature': signature, 'apiKey' : key })

### Listen update on eventName

### Replace the <eventName> with the df-position-update, df-order-update, ###balance-update

@sio.on(<eventName>)
def on_message(response):
    print(response["data"])

# leave a channel

sio.emit('leave', { 'channelName' : 'coindcx' })
```

## Get Position Update

```python
@sio.on('df-position-update')
def on_message(response):
  print(response["data"])
Response:

[
   {
      "id":"571eae12-236a-11ef-b36f-83670ba609ec",
      "pair":"B-BNB_USDT",
      "active_pos":0,
      "inactive_pos_buy":0,
      "inactive_pos_sell":0,
      "avg_price":0,
      "liquidation_price":0,
      "locked_margin":0,
      "locked_user_margin":0,
      "locked_order_margin":0,
      "take_profit_trigger":null,
      "stop_loss_trigger":null,
      "leverage":10,
      "mark_price":0,
      "maintenance_margin":0,
      "updated_at":1717754279737,
      "margin_type": "isolated",
      "margin_currency_short_name" : "INR",
      "settlement_currency_avg_price" : 89.0,

   }
]
```

## Definitions

```
Channel: coindcx
Event: df-position-update
```

## Get Order Update

```python
@sio.on('df-order-update')
def on_message(response):
  print(response["data"])
Response:

[
   {
      "id":"ff5a645f-84b7-4d63-b513-9e2f960855fc",
      "pair":"B-ID_USDT",
      "side":"sell",
      "status":"cancelled",
      "order_type":"take_profit_limit",
      "stop_trigger_instruction":"last_price",
      "notification":"email_notification",
      "leverage":1,
      "maker_fee":0.025,
      "taker_fee":0.075,
      "fee_amount":0,
      "price":0.9,
      "stop_price":1,
      "avg_price":0,
      "total_quantity":0,
      "remaining_quantity":0,
      "cancelled_quantity":0,
      "ideal_margin":0,
      "order_category":"complete_tpsl",
      "stage":"tpsl_exit",
      "created_at":1705915012812,
      "updated_at":1705999727686,
      "take_profit_price": 64000.0,
        "stop_loss_price": 61000.0,
      "trades":[

      ],
      "display_message":null,
      "group_status":null,
      "group_id":null,
        "metatags": null,
      "margin_currency_short_name" : "INR",
      "settlement_currency_conversion_price" : 89.0,

   }
]
```

## Definitions

```
Channel: coindcx
Event: df-order-update
```

## Get Balance Update

```python
@sio.on('balance-update')
def on_message(response):
  print(response["data"])
Response:

[
   {
      "id":"026ef0f2-b5d8-11ee-b182-570ad79469a2",
      "balance":"1.0221449",
      "locked_balance":"0.99478995",
      "currency_id":"c19c38d1-3ebb-47ab-9207-62d043be7447",
      "currency_short_name":"USDT"
   }
]
```

## Definitions

```
Channel: coindcx
Event: balance-update
```

## Get Candlestick Data

```python
@sio.on('candlestick')
def on_message(response):
  print(response["data"])
Response:

{
   "data":[
      {
         "open":"0.3524000",
         "close":"0.3472000",
         "high":"0.3531000",
         "low":"0.3466000",
         "volume":"5020395",
         "open_time":1705514400,
         "close_time":1705517999.999,
         "pair":"B-ID_USDT",
         "duration":"1h",
         "symbol":"IDUSDT",
         "quote_volume":"1753315.2309000"
      }
   ],
   "Ets":1705516366626,
   "i":"1h",
   "channel":"B-ID_USDT_1h-futures",
   "pr":"futures"
}
```

## Definitions

```json
The set of candlestick resolutions available are ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d", "3d", "1w", "1M"]. For example for 15 minute candle please connect to channel [instrument_name]_15m-futures

Channel: "[instrument_name]_1m-future" , "[instrument_name]_1h-futures", "[instrument_name]_1d-futures" etc.Here [instrument_name] can be derived from Get active instruments.
Example to join channel : ["join",{"channelName": "B-BTC_USDT_1m-futures" }]
Event: candlestick
```

## Get Orderbook

```python
@sio.on('depth-snapshot')
def on_message(response):
  print(response["data"])
Response:

{
   "ts":1705913767265,
   "vs":53727235,
   "asks":{
      "2410":"112.442",
      "2409.77":"55.997",
      "2409.78":"5.912"
   },
   "bids":{
      "2409.76":"12.417",
      "2409.75":"1.516",
      "2409.74":"15.876"
   },
   "pr":"futures"
}
```

## Definitions

```
Channel: "[instrument_name]@orderbook@50-futures. Here [instrument_name] can be derived from Get active instruments.Here 50 denotes, the depth of the order book the other possible values are 10 and 20.
Example to join channel : ['join', {'channelName':"B-ID_USDT@orderbook@50-futures"}]
Event: depth-snapshot
```

## Get Current Prices

```python
@sio.on('currentPrices@futures#update')
def on_message(response):
  print(response["data"])
Response:

{
   "vs":29358821,
   "ts":1707384027242,
   "pr":"futures",
   "pST":1707384027230,
   "prices":{
      "B-UNI_USDT":{
         "bmST":1707384027000,
         "cmRT":1707384027149
      },
      "B-LDO_USDT":{
         "mp":2.87559482,
         "bmST":1707384027000,
         "cmRT":1707384027149
      }
   }
}
```

## Definitions

```
Channel: currentPrices@futures@rt
Example to join channel : ['join', {'channelName':"currentPrices@futures@rt"}]
Event: currentPrices@futures#update
```

## Get New Trade

```python
@sio.on('new-trade')
def on_message(response):
  print(response["data"])
Response:

{
  "T":1705516361108,
  "RT":1705516416271.6133,
  "p":"0.3473",
  "q":"40",
  "m":1,
  "s":"B-ID_USDT",
  "pr":"f"
}
```

## Definitions

```
Channel: "[instrument_name]@trades-futures. Here [instrument_name] can be derived from Get active instruments
Example to join channel : ['join', {'channelName':"B-ID_USDT@trades-futures"}]
Event: new-trade
```

## Get LTP Data

```python
@sio.on('price-change')
def on_message(response):
  print(response["data"])
Response:

{
  "T":1705516361108,
  "p":"0.3473",
  "pr":"f"
}
```

## Definitions

```python
Channel: "[instrument_name]@trades-futures. Here [instrument_name] can be derived from Get active instruments
Example to join channel : ['join', {'channelName':"B-ID_USDT@prices-futures"}]
Event: new-trade
Sample code for Socket Connection
import socketio
import hmac
import hashlib
import json
import time
import asyncio
from datetime import datetime
from socketio.exceptions import TimeoutError
socketEndpoint = 'wss://stream.coindcx.com'
sio = socketio.AsyncClient()

key = "xxx"
secret = "xxx"

# python3

secret_bytes = bytes(secret, encoding='utf-8')
channelName = "coindcx"
body = {"channel": channelName}
json_body = json.dumps(body, separators=(',', ':'))
signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

async def ping_task():
    while True:
        await asyncio.sleep(25)
        try:
            await sio.emit('ping', {'data': 'Ping message'})
        except Exception as e:
            print(f"Error sending ping: {e}")

@sio.event
async def connect():
    print("I'm connected!")
    current_time = datetime.now()
    print("Connected Time:", current_time.strftime("%Y-%m-%d %H:%M:%S"))

    await sio.emit('join', {'channelName': "coindcx", 'authSignature': signature, 'apiKey': key})
    await sio.emit('join', {'channelName': "B-ID_USDT@prices-futures"})

@sio.on('price-change')
async def on_message(response):
    current_time = datetime.now()
    print("Price Change Time:", current_time.strftime("%Y-%m-%d %H:%M:%S"))
    print("Price Change Response !!!")
    print(response)

async def main():
    try:
        await sio.connect(socketEndpoint, transports='websocket')
        # Wait for the connection to be established
        asyncio.create_task(ping_task())

        await sio.wait()
        while True:
            time.sleep(1)
            sio.event('price-change', {'channelName': "B-ID_USDT@prices-futures"})
    except Exception as e:
        print(f"Error connecting to the server: {e}")
```

- `raise` — # re-raise the exception to see the full traceback

```
# Run the main function

if __name__ == '__main__':
    asyncio.run(main())
Response:
```

## Definitions

```
Websocket connection implementation with ping check
```

## FAQ

```
From where to start
Authentication
General API
Markets
Orders
Sockets
User Data
Handling Errors
```

## Errors

```
The CoinDCX API uses the following error codes:

Error Code Meaning
400 Bad Request -- Your request is invalid.
401 Unauthorized -- Your API key is wrong.
404 Not Found -- The specified link could not be found.
429 Too Many Requests -- You're making too many API calls
500 Internal Server Error -- We had a problem with our server. Try again later.
503 Service Unavailable -- We're temporarily offline for maintenance. Please try again later.
```
