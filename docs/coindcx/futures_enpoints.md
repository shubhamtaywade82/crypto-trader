# Futures End Points

## Glossary

```plain
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

## Get active instruments

```python
import json

import requests  # Install requests module first.

# Use this url to get the USDT active instruments

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name[]=USDT>"

# Use this url to get the INR active instruments

# url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name[]=INR>"

response = requests.get(url)
data = response.json()
print(json.dumps(data, indent=2))
```

## Response

```json
[
    "B-VANRY_USDT",
    "B-BOME_USDT",
    "B-BTCDOM_USDT",
    "B-IOTX_USDT",
    "B-LPT_USDT",
    "B-ENA_USDT",
    "B-GMT_USDT",
    "B-APE_USDT",
    "B-WOO_USDT",
    "B-ASTR_USDT",
    "B-GMX_USDT",
    "B-TLM_USDT",
   ]
Use this endpoint to fetch the list of all active Futures instruments.
```

## HTTP Request

```
GET <https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name[]={futures_margin_mode}>
```

## Get instrument details

```python
import requests  # Install requests module first.

# Use this url to get the INR active instrument info

# url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/data/instrument?pair=B-BTC_USDT&margin_currency_short_name=INR>"

# Use this url to get the USDT active instrument info

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/data/instrument?pair=B-BTC_USDT&margin_currency_short_name=USDT>"
response = requests.get(url)
data = response.json()
print(data)
```

## Response

```json
{
   "instrument":{
      "settle_currency_short_name":"USDT",
      "quote_currency_short_name":"USDT",
      "position_currency_short_name":"AAVE",
      "underlying_currency_short_name":"AAVE",
      "status":"active",
      "pair":"B-AAVE_USDT",
      "kind":"perpetual",
      "settlement":"never",
      "max_leverage_long":10.0,
      "max_leverage_short":10.0,
      "unit_contract_value":1.0,
      "price_increment":0.01,
      "quantity_increment":0.1,
      "min_trade_size":0.1,
      "min_price":4.557,
      "max_price":2878.2,
      "min_quantity":0.1,
      "max_quantity":950000.0,
      "min_notional":6.0,
      "maker_fee":0.025,
      "taker_fee":0.075,
      "safety_percentage":2.0,
      "quanto_to_settle_multiplier":1.0,
      "is_inverse":false,
      "is_quanto":false,
      "allow_post_only":false,
      "allow_hidden":false,
      "max_market_order_quantity":1250.0,
      "funding_frequency":8,
      "max_notional":320000.0,
      "expiry_time":2548162800000,
      "exit_only":false,
      "multiplier_up":8.0,
      "multiplier_down":8.0,
      "liquidation_fee": 1.0,
      "margin_currency_short_name": "USDT",
      "time_in_force_options":[
         "good_till_cancel",
         "immediate_or_cancel",
         "fill_or_kill"
      ],
      "order_types":[
         "market_order",
         "limit_order",
         "stop_limit",
         "take_profit_limit",
         "stop_market",
         "take_profit_market"
      ],
      "dynamic_position_leverage_details":{
         "5":15000000.0,
         "8":5000000.0,
         "10":1000000.0,
         "15":500000.0,
         "20":100000.0,
         "25":50000.0
      },
      "dynamic_safety_margin_details":{
         "50000":1.5,
         "100000":2.0,
         "500000":3.0,
         "1000000":5.0,
         "5000000":6.0,
         "15000000":10.0
      }
   }
}

Use this endpoint to fetch the all the details of the instrument
```

## HTTP Request

```
GET <https://api.coindcx.com/exchange/v1/derivatives/futures/data/instrument?pair={instrument}&margin_currency_short_name={futures_margin_mode}>
```

## Response Defnitions

```json
Key Description
settle_currency_short_name Currency in which you buy/sell futures contracts
quote_currency_short_name Currency in which you see the price of the futures contract
position_currency_short_name Underlying crypto on which the futures contract is created
underlying_currency_short_name Underlying crypto on which the futures contract is created
status Status of the instrument. Possible values are “active“ and “inactive“.
pair Instrument Pair name. This is the format in which the input of the pairs will be given in any API request.
kind CoinDCX only supports perpetual contracts for now, so this value will always be “perpetual”
settlement This will be the settlement date of the contract. It will be “never” for perpetual contracts
max_leverage_long Ignore this
max_leverage_short Ignore this
unit_contract_value This will be equal to 1 for all the Perpetual futures
price_increment If price increment is 0.1 then price inputs for limit order can be x, x+0.1, x+0.1*2, x+0.1*3, etc
quantity_increment If qty increment is 0.1 then qty inputs for an order can be x, x+0.1, x+0.1*2, x+0.1*3, etc
min_trade_size This is the minimum quantity of a trade that can be settled on exchange.
min_price Minimum amount to enter the position
max_price Maximum amount to enter the position
min_quantity Minimum quantity to enter the position
max_quantity Maximum quantity to enter the position
min_notional Minimum value you can purchase for a symbol
maker_fee Maker fees mean when you add liquidity to the market by placing a new order that isn’t immediately matched.
taker_fee Taker fees mean when you remove liquidity by filling an existing order.
safety_percentage Ignore this
quanto_to_settle_multiplier Ignore this. This will be equal to 1
is_inverse Ignore this. This will be false
is_quanto Ignore this. This will be false
allow_post_only Ignore this
allow_hidden Ignore this
max_market_order_quantity This gives the maximum allowed quantity in a market order
funding_frequency Number of hours after which funding happens. If the value is 8 that means the funding happens every 8 hours.
max_notional Ignore this
exit_only If this is true then you can’t place fresh orders to take a new position or add more to an existing position. Although you can reduce your existing positions and cancel your open orders. If you already have open orders to add positions, they will not be impacted.
multiplier_up This denotes how aggressive your limit buy orders can be placed compared to LTP. For example if the LTP is 100 and you want to place a buy order. The limit price has to be between minPrice and LTP*(1+multiplierUp/100)
multiplier_down This denotes how aggressive your limit sell orders can be placed compared to LTP. For example if the LTP is 100 and you want to place a sell order. The limit price has to be between maxPrice and LTP*(1-multiplierDown/100)
liquidation_fee This denotes Applicable fee if the trade received for the order is a trade for the liquidation order
dynamic_position_leverage_details Sample response: { "5":15000000.0, "8":5000000.0, "10":1000000.0, "15":500000.0, "20":100000.0, "25":50000.0 } This gives you the max allowed leverage for a given position size. So for example if your positions size is 120K which is higher than 100K and less than 500K USDT, then max allowed leverage is 15x
dynamic_safety_margin_details Sample response: { "50000":1.5, "100000":2.0, "500000":3.0, "1000000":5.0, "5000000":6.0, "15000000":10.0 } This gives you the calculation for maintenance margin of the position. In the above example, if you have a position size of 60K USDT, then your maintenance margin will be 50K*1.5% + 10K*2% = 950 USDT.Your position will be liquidated when the margin available in your position goes below 950 USDT. Liquidation price is calculated using this number and will be updated in the get position endpoint
expiry_time Ignore this
time_in_force_options Time in force indicates how long your order will remain active before it is executed or expired. Possible values are good_till_cancel, immediate_or_cancel, fill_or_kill
margin_currency_short_name Futures margin mode
```

## Get instrument Real-time trade history

```python
import requests  # Install requests module first.
url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/data/trades?pair={instrument_name}>"
# sample_url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/data/trades?pair=B-MKR_USDT>"
response = requests.get(url)
data = response.json()
print(data)
```

## Response

```json
[
    {
        "price": 1.1702,
        "quantity": 22000,
        "timestamp": 1675037938736,
        "is_maker": true
    },
    {
        "price": 1.1702,
        "quantity": 38000,
        "timestamp": 1675037950130,
        "is_maker": true
    }
]

Use this endpoint to fetch the real time trade history details of the instrument.While rest APIs exist for this, we recommend using Futures Websockets
```

## HTTP Request

```
GET <https://api.coindcx.com/exchange/v1/derivatives/futures/data/trades?pair={instrument_name}>
```

## Response Defnitions

```
KEY DESCRIPTION
price Price of the trade update
quantity Quantity of the trade update
timestamp EPOCH timestamp of the event
is_maker If the trade is maker then this value will be “true”
```

## Get instrument orderbook

```python
import requests  # Install requests module first.

url = "<https://public.coindcx.com/market_data/v3/orderbook/{instrument}-futures/50>"
# sample_url = "public.coindcx.com/market_data/v3/orderbook/B-MKR_USDT-futures/50"
# Here 50 denotes, the depth of the order book the other possible values are 10 and 20
response = requests.get(url)
data = response.json()
print(data)
```

## Response

```json
{
    "ts": 1705483019891,
    "vs": 27570132,
    "asks": {
        "2001": "2.145",
        "2002": "4.453",
        "2003": "2.997"
    },
    "bids": {
        "1995": "2.618",
        "1996": "1.55"
    }
}

Use this endpoint to fetch the depth of the order book details of the instrument.While rest APIs exist for this, we recommend using Futures Websockets Here 50 denotes, the depth of the order book the other possible values are 10 and 20
```

## HTTP Request

```
GET <https://public.coindcx.com/market_data/v3/orderbook/{instrument}-futures/50>
```

## Response Defnitions

```
KEY DESCRIPTION
ts Epoch timestamp
vs Version
asks List of ask price and quantity
bids List of bid price and quantity
```

## Get instrument candlesticks

```python
import requests
url = "<https://public.coindcx.com/market_data/candlesticks>"
query_params = {
    "pair": "B-MKR_USDT",
    "from": 1704100940,
    "to": 1705483340,
    "resolution": "1D",  # '1' OR '5' OR '60' OR '1D'
    "pcode": "f"
}
response = requests.get(url, params=query_params)
if response.status_code == 200:
    data = response.json()
    # Process the data as needed
    print(data)
else:
    print(f"Error: {response.status_code}, {response.text}")
```

## Response

```json
{
   "s":"ok",
   "data":[
      {
         "open":1654.2,
         "high":1933.5,
         "low":1616.5,
         "volume":114433.544,
         "close":1831.9,
         "time":1704153600000
      },
      {
         "open":1832.2,
         "high":1961,
         "low":1438,
         "volume":158441.387,
         "close":1807.6,
         "time":1704240000000
      }
   ]
}

Use this endpoint to fetch the candlestick bars for a symbol. Klines are uniquely identified by their open time of the instrument.While rest APIs exist for this, we recommend using Futures Websockets
```

## HTTP Request

```
GET <https://public.coindcx.com/market_data/candlesticks?pair={pair}&from={from}&to={to}&resolution={resolution}&pcode=f>
```

## Request Defnitions

```python
Name Type Mandatory Description
pair String YES Name of the pair
from Integer YES EPOCH start timestamp of the required candlestick in seconds
to Integer YES EPOCH end timestamp of the required candlestick in seconds
resolution String YES '1' OR '5' OR '60' OR '1D' for 1min, 5min, 1hour, 1day respectively
pcode String YES Static value “f” to be used here. It denotes product = futures
```

## Response Defnitions

```
KEY DESCRIPTION
s status
open The first recorded trading price of the pair within that particular timeframe.
high The highest recorded trading price of the pair within that particular timeframe.
low The lowest recorded trading price of the pair within that particular timeframe.
volume Total volume in terms of the quantity of the pair.
close The last recorded trading price of the pair within that particular timeframe.
time EPOCH timestamp of the open time.
```

## List Orders

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
        "timestamp": timeStamp , # EPOCH timestamp in seconds
        "status": "open", # Comma separated statuses as open,filled,cancelled
        "side": "buy", # buy OR sell
        "page": "1", #// no.of pages needed
        "size": "10", #// no.of records needed
    "margin_currency_short_name": ["INR", "USDT"]
        }

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/orders>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
[
   {
      "id":"714d2080-1fe3-4c6e-ba81-9d2ac9a46003",
      "pair":"B-ETH_USDT",
      "side":"buy",
      "status":"open",
      "order_type":"limit_order",
      "stop_trigger_instruction":"last_price",
      "notification":"no_notification",
      "leverage":20.0,
      "maker_fee":0.025,
      "taker_fee":0.075,
      "fee_amount":0.0,
      "price":2037.69,
      "stop_price":0.0,
      "avg_price":0.0,
      "total_quantity":0.019,
      "remaining_quantity":0.019,
      "cancelled_quantity":0.0,
      "ideal_margin":1.93870920825,
      "order_category":"None",
      "stage":"default",
      "group_id":"None",
      "liquidation_fee": null,
      "position_margin_type": "crossed",
      "display_message":"ETH limit buy order placed!",
      "group_status":"None",
      "metatags": null,
      "created_at":1705565256365,
      "updated_at":1705565256940,
      "margin_currency_short_name": "INR",
      "settlement_currency_conversion_price": 89.0,
      "take_profit_price": 64000.0,
      "stop_loss_price": 61000.0,
   },
   {
      "id":"ffb261ae-8010-4cec-b6e9-c111e0cc0c10",
      "pair":"B-ID_USDT",
      "side":"buy",
      "status":"filled",
      "order_type":"market_order",
      "stop_trigger_instruction":"last_price",
      "notification":"no_notification",
      "leverage":10.0,
      "maker_fee":0.025,
      "taker_fee":0.075,
      "fee_amount":0.011181375,
      "price":0.3312,
      "stop_price":0.0,
      "avg_price":0.3313,
      "total_quantity":45.0,
      "remaining_quantity":0.0,
      "cancelled_quantity":0.0,
      "ideal_margin":1.4926356,
      "order_category":"None",
      "stage":"default",
      "group_id":"None",
      "liquidation_fee": null,
      "position_margin_type": "crossed",
      "display_message":"ID market buy order filled!",
      "group_status":"None",
      "metatags": null,
      "created_at":1705565061504,
      "updated_at":1705565062462,
      "margin_currency_short_name": "INR",
      "settlement_currency_conversion_price": 89.0,
      "take_profit_price": null,
      "stop_loss_price": null,
   }
]
Use this endpoint to fetch the list of orders based on the status ( open,filled,cancelled ) and side ( buy OR sell )
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/orders>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
status string YES Comma separated statuses as open, filled, partially_filled, partially_cancelled, cancelled, rejected, untriggered
side string YES buy OR sell
page string YES Required page number
size string YES Number of records needed per page
margin_currency_short_name Array OPTIONAL Futures margin mode.
Default value - ["USDT"]. Possible values INR & USDT.
```

## Response Defnitions

```
Note : fee_amount and ideal_margin values are in USDT for INR Futures.

KEY DESCRIPTION
id Order id
pair Instrument Pair (Format: B-ETH_USDT)
side Order side. Possible values are buy or sell
status Order status. Possible values are:
OPEN - The order has been accepted and is in open status
PARTIALLY_FILLED - Order which is partially filled and the remaining quantity is open
FILLED - The order has been completely filled
CANCELED - The order has been canceled
PARTIALLY_CANCELED - Order which is partially filled and the remaining quantity has been cancelled
REJECTED - The order was not accepted by the system
UNTRIGGERED - TP or SL orders which are not triggered yet
order_type Order type. Possible values are:
limit - A type of order where the execution price will be no worse than the order's set price. The execution price is limited to be the set price or better.
market - A type of order where the user buys or sells an asset at the best available prices and liquidity until the order is fully filled or the order book's liquidity is exhausted.
stop_market - Once the market price hits the stopPrice, a market order is placed on the order book.
stop_limit - Once the market price hits the stopPrice, a limit order is placed on the order book at the limit price.
take_profit_market - Once the market price hits the stopPrice, a market order is placed on the order book.
take_profit_limit - Once the market price hits the stopPrice, a limit order is placed on the order book at the limit price.
stop_trigger_instruction Ignore this
notification Possible options: no_notification, email_notification. Email notification will send email notification once the order is filled.
leverage This is the leverage at which the order was placed
maker_fee Fee charged when the order is executed as maker
taker_fee Fee charged when the order is executed as taker
fee_amount Amount of fee charged on an order. This shows the fee charged only for the executed part of the order
price Limit price at which the limit order was placed. For market order, this will be the market price at the time when the market order was placed
stop_price Trigger price of take profit or stop loss order
avg_price Average execution price of the order on the exchange. It can be different compared to “price” due to liquidity in the order books
total_quantity Total quantity of the order placed
remaining_quantity Remaining quantity of the order which is still open and can be executed in the future
cancelled_quantity Quantity of the order which is cancelled and will not be executed
ideal_margin Ignore this
order_category Ignore this
stage
default - Standard limit, market, stop limit, stop market, take profit limit, or take profit market order
exit - Quick exit which closes the entire position
liquidate - Order which was created by the system to liquidate a futures position
tpsl_exit - Take profit or stop loss order which was placed to close the entire futures position
group_id Group id used when a large order is split into smaller parts. All split parts will have the same group id
liquidation_fee Applicable fee if the trade received for the order is a trade for the liquidation order
position_margin_type “crossed” if the order was placed for cross margin position. “Isolated” if the order is placed for isolated margin position. Please consider NULL also as isolated.
display_message Ignore this
group_status Ignore this
created_at Timestamp at which the order was created
margin_currency_short_name Futures margin mode
settlement_currency_conversion_price USDT <> INR conversion price for the order. This is relevant only for INR margined Orders.
updated_at Last updated timestamp of the order
take_profit_price Take Profit Trigger: Once your order begins to fill, this take profit trigger will update any existing open TP/SL order and will apply to your entire position. Note: Take profit triggers attached to reduce-only orders will be ignored.
stop_loss_price Stop Loss Trigger: Once your order begins to fill, this stop loss trigger will update any existing open TP/SL order and will apply to your entire position. Note: Stop loss triggers attached to reduce-only orders will be ignored.
```

## Create Order

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
        "timestamp":timeStamp , # EPOCH timestamp in seconds
        "order": {
        "side": "sell", # buy OR sell
        "pair": "B-ID_USDT", # instrument.string
        "order_type": "market_order", # market_order OR limit_order
        "price": "0.2962", #numeric value
    "stop_price": "0.2962", #numeric value
        "total_quantity": 33, #numerice value
        "leverage": 10, #numerice value
        "notification": "email_notification", # no_notification OR email_notification OR push_notification
        "time_in_force": "good_till_cancel", # good_till_cancel OR fill_or_kill OR immediate_or_cancel
        "hidden": False, # True or False
        "post_only": False, # True or False
    "take_profit_price": 64000.0,
    "stop_loss_price": 61000.0
        }
        }

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/orders/create>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
[
   {
      "id":"c87ca633-6218-44ea-900b-e86981358cbd",
      "pair":"B-ID_USDT",
      "side":"sell",
      "status":"initial",
      "order_type":"market_order",
      "notification":"email_notification",
      "leverage":10.0,
      "maker_fee":0.025,
      "taker_fee":0.075,
      "fee_amount":0.0,
      "price":0.2966,
      "avg_price":0.0,
      "total_quantity":33.0,
      "remaining_quantity":33.0,
      "cancelled_quantity":0.0,
      "ideal_margin":0.98024817,
      "order_category":"None",
      "stage":"default",
      "group_id":"None",
      "take_profit_price": 64000.0,
      "stop_loss_price": 61000.0,
      "liquidation_fee": null,
      "position_margin_type": "crossed",
      "display_message":"None",
      "group_status":"None",
      "margin_currency_short_name" : "INR",
      "settlement_currency_conversion_price": 89.0,
      "created_at":1705647376759,
      "updated_at":1705647376759,
      "take_profit_price": 64000.0,
      "stop_loss_price": 61000.0
   }
]

Use this endpoint to create an order by passing the necessary parameters.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/orders/create>
```

## NOTE

```
*** "Do not include 'time_in_force' parameter for market orders."

Buy Orders:
Stop Limit:
Stop price must be greater than LTP.
Limit price must be greater than stop price.
Take Profit Limit:
Stop price must be less than LTP.
Limit price must be greater than stop price and less than LTP.
Sell Orders:
Stop Limit:
Stop price must be less than LTP.
Limit price must be less than stop price.
Take Profit Limit:
Stop price must be greater than LTP.
Limit price must be less than stop price and greater than LTP.
```

## Request Defnitions

```
Note : Cross margin mode is only supported on USDT margined Futures at the moment.

Name Type Mandatory Description
timestamp Integer YES Latest epoch timestamp when the order is placed. Orders with a delay of more than 10 seconds will be rejected.
side String YES buy OR sell
pair String YES Pair name (format: B-ETH_USDT)
order_type String YES market, limit, stop_limit, stop_market, take_profit_limit, take_profit_market
price Integer YES Order Price (limit price for limit, stop limit, and take profit limit orders). Keep this NULL for market orders.
stop_price Integer YES Stop Price (stop_limit, stop_market, take_profit_limit, take_profit_market orders). stop_price is the trigger price of the order.
total_quantity Integer YES Order total quantity
leverage Integer OPTIONAL This is the leverage at which you want to take a position. Should match the leverage of the position. Preferably set before placing the order to avoid rejection.
notification String YES no_notification OR email_notification. Set as email_notification to receive an email once the order is filled.
time_in_force String OPTIONAL Possible values: good_till_cancel, fill_or_kill, immediate_or_cancel. Default is good_till_cancel if not provided. Should be null for market orders.
hidden Boolean NO Ignore this (Not supported at the moment)
post_only Boolean NO Ignore this (Not supported at the moment)
margin_currency_short_name String OPTIONAL Futures margin mode.
Default value - "USDT". Possible values INR & USDT.
position_margin_type String OPTIONAL isolated, crossed.
If position margin type is not passed, it considers the margin type of the position as default.
take_profit_price Decimal OPTIONAL Take profit price. This value should only be sent for market_order, limit_order. These values will not be accepted for orders that reduce the position size (Note that no error will be raised in such cases)
stop_loss_price Decimal OPTIONAL Stop loss price. This value should only be sent for market_order, limit_order. These values will not be accepted for orders that reduce the position size (Note that no error will be raised in such cases)
```

## Possible Error Codes

```
Status Code Message Reason
422 Order leverage must be equal to position leverage When the leverage specified for an order does not match the leverage of the current position.
422 Quantity for limit variant orders should be less than 9500.0 Total quantity for a limit order exceeds the maximum allowed limit.
422 Quantity for market variant orders should be less than 9500.0 Total quantity for a market order exceeds the maximum allowed market order quantity.
400 Price is out of permissible range If limit price or stop price mentioned is out of range i.e. price > max_price || price < min_price for the instrument
400 Please enter a value lower than x Price is greater than max limit price (i.e. ltp + ltp *multiplier_up)
400 Please enter a value higher than x Price is lower than min limit price (i.e. ltp - ltp* multiplier_down)
400 Price should be divisible by 0.01 Price isn't divisible by the tick size
422 Quantity should be greater than y Quantity isn't greater than min quantity
400 Insufficient funds Wallet doesn't have sufficient funds for placing the order
400 Minimum order value should be x USDT Order value must be greater than min notional
400 Instrument is in exit-only mode. You can’t add more position.
400 You've exceeded the max allowed position of x USDT. Current position size is greater than position size threshold
400 Order is exceeding the max allowed position of x USDT. Position size + order value > position size threshold
422 Price can't be empty for limit_order Order
400 Trigger price should be greater than the current price buy order, trigger_price < current price
400 Limit price should be greater than the trigger price buy limit order, limit price < trigger price
400 Trigger price should be less than the current price sell order, trigger price > current price
400 Limit price should be less than the trigger price sell order, limit price < trigger price
500  Invalid input
```

## Response Defnitions

```
KEY DESCRIPTION
id Order id
pair Name of the futures pair
side Side: buy / sell
status Ignore this (It will be initial for all the newly placed orders)
order_type Order type. Possible values are :
limit - a type of order where the execution price will be no worse than the order's set price. The execution price is limited to be the set price or better.
market - A type of order where the user buys or sells an asset at the best available prices and liquidity until the order is fully filled or the order book's liquidity is exhausted.
stop_market - once the market price hits the stopPrice, a market order is placed on the order book.
stop_limit - once the market price hits the stopPrice, a limit order is placed on the order book at the limit price.
take_profit_market - once the market price hits the stopPrice, a market order is placed on the order book.
take_profit_limit - once the market price hits the stopPrice, a limit order is placed on the order book at the limit price.
notification no_notification OR email_notification
If property is set as email_notification then you will get an email once the order is filled

leverage This is the leverage at which you want to take a position.
This has to be the same as the leverage of the position. Else the order will be rejected.

You should preferably set the leverage before placing the order to avoid order rejection. Leverage needs to be set only once post which it will be saved in the system for that particular pair.

maker_fee Applicable fee if the trade received for the order is a maker trade
taker_fee Applicable fee if the trade received for the order is a taker trade
fee_amount This will be the fee that has been charged for the user till now. As soon as the order is placed, this value will be zero until you start receiving trades for the order
price Order Price (limit price for limit, stop limit and take profit limit orders) Keep this NULL for market orders. Else the order will be rejected.
avg_price It will be zero for the newly placed orders. You can check the latest fill price from the list orders endpoint.
total_quantity Total quantity of the order
remaining_quantity Remaining quantity of the order that is still open on the exchange and can get filled
cancelled_quantity Quantity of the order that is canceled and won’t be filled
ideal_margin Ignore this
order_category Ignore this
stage default - Standard limit, market, stop limit, stop market, take profit limit or take profit market order exit - Quick exit which closes the entire position liquidate - Order which was created by the system to liquidate a futures position tpsl_exit - Take profit or stop loss order which was placed to close the entire futures position
group_id Group id is an id which is used whenever a large order is split into smaller parts.System auto-splits the market variant orders like quick exit order, liquidate order and tpsl_exit order into smaller parts if the order size is huge. All the split parts will have the same group id
liquidation_fee Applicable fee if the trade received for the order is a trade for the liquidation order
position_margin_type “crossed” if the order was placed for cross margin position. “Isolated” if the order is placed for isolated margin position. Please consider NULL also as isolated.
display_message Ignore this
group_status Ignore this
created_at Timestamp at which the order was created
margin_currency_short_name Futures margin mode
settlement_currency_conversion_price USDT <> INR conversion price when the order is placed. This is relevant only for INR margined Orders.
updated_at Last updated timestamp of the order
take_profit_price Take Profit Trigger: Once your order begins to fill, this take profit trigger will update any existing open TP/SL order and will apply to your entire position. Note: Take profit triggers attached to reduce-only orders will be ignored.
stop_loss_price Stop Loss Trigger: Once your order begins to fill, this stop loss trigger will update any existing open TP/SL order and will apply to your entire position. Note: Stop loss triggers attached to reduce-only orders will be ignored.
```

## Cancel Order

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
        "timestamp":timeStamp , # EPOCH timestamp in seconds
        "id": "c87ca633-6218-44ea-900b-e86981358cbd" # order.id
        }

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/orders/cancel>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
{
   "message":"success",
   "status":200,
   "code":200
}

Use this endpoint to cancel an order by passing the order id.
```

## HTTP Request

```
POST <https://api.coindcx.com//exchange/v1/derivatives/futures/orders/cancel>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
id String YES Order id
```

## List Positions

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
        "timestamp":timeStamp , # EPOCH timestamp in seconds
        "page": "1", #no. of pages needed
        "size": "10",
        "margin_currency_short_name": ["USDT"]
        }

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
[
  {
    "id": "571eae12-236a-11ef-b36f-83670ba609ec",
    "pair": "B-BNB_USDT",
    "active_pos": 0.0,
    "inactive_pos_buy": 0.0,
    "inactive_pos_sell": 0.0,
    "avg_price": 0.0,
    "liquidation_price": 0.0,
    "locked_margin": 0.0,
    "locked_user_margin": 0.0,
    "locked_order_margin": 0.0,
    "take_profit_trigger": null,
    "stop_loss_trigger": null,
    "leverage": 10.0,
    "maintenance_margin": 0.0,
    "mark_price": 0.0,
    "margin_type": "crossed",
    "settlement_currency_avg_price": 0.0,
    "margin_currency_short_name": "USDT",
    "updated_at": 1717754279737
  }
]

Use this endpoint to fetch positions by passing timestamp.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
page String YES Required page number
size String YES Number of records needed per page
margin_currency_short_name Array OPTIONAL Futures margin mode.
Default value - ["USDT"]. Possible values INR & USDT.
```

## Response Defnitions

```
Note : All the margin values are in USDT for INR Futures.

KEY DESCRIPTION
id Position id. This remains fixed for a particular pair. For example, the position id of your B-ETH_USDT position will remain the same over time.
pair Name of the futures pair
active_pos Quantity of the position in terms of underlying. For example, if active_pos = 1 for B-ETH_USDT then you hold 1 quantity ETH Futures contract. For short positions, active_pos will be negative.
inactive_pos_buy Sum of the open quantities of the pending buy orders.
inactive_pos_sell Sum of the open quantities of the pending sell orders.
avg_price Average entry price of the position.
liquidation_price Price at which the position will get liquidated. This is applicable only for positions with isolated margin. Ignore this for cross margined positions.
locked_margin Margin (in USDT) locked in the position after debiting fees and adjusting funding from the initial investment.
locked_user_margin Margin (in USDT) that was initially invested in the futures position excluding fees and funding.
locked_order_margin Total margin in USDT that is locked in the open orders.
take_profit_trigger Trigger price set for Full Position take profit order.
stop_loss_trigger Trigger price set for Full position stop loss order.
leverage Leverage of the position.
maintenance_margin The amount of margin required to be maintained in the account to avoid liquidation. For cross margined positions, the maintenance margin required is equal to the sum of the maintenance margins of all the positions.
mark_price Mark price at the time when the position was last updated. Note that this value is not real-time and is only for reference purpose.
margin_type “crossed” if the order was placed for cross margin position. “Isolated” if the order is placed for isolated margin position. Please consider NULL also as isolated.
settlement_currency_avg_price Average USDT <> INR conversion price for the position. This is relevant only for INR margined Positions.
margin_currency_short_name Futures margin mode
updated_at Timestamp when the position was last updated. It could be due to trade update, funding, add/remove margin, or changes in full position take profit/stop loss orders.
```

## Get Positions By pairs or positionid

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "xxx"
secret = "xxx"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
    "timestamp": timeStamp,  # EPOCH timestamp in seconds
    "page": "1",
    "size": "10",
    "pairs": "B-BTC_USDT,B-ETH_USDT",
    #"position_ids": "7830d2d6-0c3d-11ef-9b57-0fb0912383a7"
    "margin_currency_short_name": ["USDT"]

}

json_body = json.dumps(body, separators=(',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data=json_body, headers=headers)
data = response.json()
print(data)
```

## Response

```json
[
  {
    "id": "c7ae392e-5d70-4aaf-97dc-8e6b0076e391",
    "pair": "B-BTC_USDT",
    "active_pos": 0.0,
    "inactive_pos_buy": 0.0,
    "inactive_pos_sell": 0.0,
    "avg_price": 0.0,
    "liquidation_price": 0.0,
    "locked_margin": 0.0,
    "locked_user_margin": 0.0,
    "locked_order_margin": 0.0,
    "take_profit_trigger": 0.0,
    "stop_loss_trigger": 0.0,
    "leverage": null,
    "maintenance_margin": null,
    "mark_price": null,
    "margin_type": "crossed",
    "settlement_currency_avg_price": 0.0,
    "margin_currency_short_name": "USDT",
    "updated_at": 1709548678689
  }
]

Use this endpoint to fetch positions by passing either pairs or position id’s.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
page String YES Required page number
size String YES Number of records needed per page
pairs String OPTIONAL Instrument pair (can pass multiple values with comma-separated)
position_ids String OPTIONAL Position id’s (can pass multiple values with comma-separated)
margin_currency_short_name Array OPTIONAL Futures margin mode.
Default value - ["USDT"]. Possible values INR & USDT.
NOTE : Based on the requirement use the “ pairs “ or “ position_ids “ parameter. You need to use either one of the 2 parameters
```

## Response Defnitions

```
Note : All the margin values are in USDT for INR Futures.

KEY DESCRIPTION
id Position id. This remains fixed for a particular pair. For example, the position id of your B-ETH_USDT position will remain the same over time.
pair Name of the futures pair
active_pos Quantity of the position in terms of underlying. For example, if active_pos = 1 for B-ETH_USDT then you hold 1 quantity ETH Futures contract. For short positions, active_pos will be negative.
inactive_pos_buy Sum of the open quantities of the pending buy orders.
inactive_pos_sell Sum of the open quantities of the pending sell orders.
avg_price Average entry price of the position.
liquidation_price Price at which the position will get liquidated. This is applicable only for positions with isolated margin. Ignore this for cross margined positions.
locked_margin Margin (in USDT) locked in the position after debiting fees and adjusting funding from the initial investment.
locked_user_margin Margin (in USDT) that was initially invested in the futures position excluding fees and funding.
locked_order_margin Total margin in USDT that is locked in the open orders.
take_profit_trigger Trigger price set for Full Position take profit order.
stop_loss_trigger Trigger price set for Full position stop loss order.
leverage Leverage of the position.
maintenance_margin The amount of margin required to be maintained in the account to avoid liquidation. For cross margined positions, the maintenance margin required is equal to the sum of the maintenance margins of all the positions.
mark_price Mark price at the time when the position was last updated. Note that this value is not real-time and is only for reference purpose.
margin_type “crossed” if the order was placed for cross margin position. “Isolated” if the order is placed for isolated margin position. Please consider NULL also as isolated.
settlement_currency_avg_price Average USDT <> INR conversion price for the position. This is relevant only for INR margined Positions.
margin_currency_short_name Futures margin mode
updated_at Timestamp when the position was last updated. It could be due to trade update, funding, add/remove margin, or changes in full position take profit/stop loss orders.
```

## Update position leverage

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "xxx"
secret = "xxx"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
    "timestamp": timeStamp,  # EPOCH timestamp in seconds
    "leverage": "5", # leverage value
    "pair": "B-LTC_USDT" # instrument.pair
    #"id": "7830d2d6-0c3d-11ef-9b57-0fb0912383a7",
    #"margin_currency_short_name": "INR"
}

json_body = json.dumps(body, separators=(',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/update_leverage>
"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data=json_body, headers=headers)
data = response.json()
print(data)
```

## Response

```json
{
  "message": "success",
  "status": 200,
  "code": 200
}

Use this endpoint to update the leverage by passing either pair or position id.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/update_leverage>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
leverage String YES leverage value
pair String OPTIONAL Instrument pair (can pass multiple values with comma-separated)
id String OPTIONAL Position id’s (can pass multiple values with comma-separated)
margin_currency_short_name String YES Futures margin mode.
Default value - ["USDT"]. Possible values INR & USDT.
NOTE : Based on the requirement use the “ pairs “ or “ position_ids “ parameter. You need to use either one of the 2 parameters
```

## Possible Error Codes

```
Status Code Message Reason
400 Leverage cannot be less than 1x When leverage specified is less than the minimum allowed leverage of 1x.
400 Max allowed leverage for current position size = 5x User leverage exceeds the maximum allowed leverage based on tiered limits.
400 Insufficient funds Wallet doesn't have sufficient funds for updating position or placing the order.
422 Liquidation will be triggered instantly Condition where liquidation of the position will occur immediately.
```

## Add Margin

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
        "timestamp": timeStamp , // EPOCH timestamp in seconds
        "id": "434dc174-6503-4509-8b2b-71b325fe417a", // position.id
        "amount":1
        }

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/add_margin>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}
response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
{
   "message":"success",
   "status":200,
   "code":200
}

Use this endpoint to add the margin to the position.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/add_margin>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
id String YES Position id
amount Integer YES Amount of margin to be added to the position.
Input will be in INR for INR margined futures and in USDT for USDT margined futures. Adding margin to the position makes your position safer by updating its liquidation price.
```

## Remove Margin

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
        "timestamp": timeStamp , # EPOCH timestamp in seconds
        "id": "434dc174-6503-4509-8b2b-71b325fe417a", # position.id
        "amount": 10
        }

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/remove_margin>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
{
   "message":"success",
   "status":200,
   "code":200
}

Use this endpoint to remove the margin for the position.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/remove_margin>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
id String YES Position id
amount Integer YES Amount of margin to be removed from the position.
Input will be in INR for INR margined futures and in USDT for USDT margined futures. Removing margin increases the risk of your position (liquidation price will get updated).
```

## Possible Error Codes

```
Status Code Message Reason
422 Cannot remove margin as exit or liquidation is already in process Attempting to modify margin while an exit or liquidation process is ongoing.
422 Cannot change margin for an inactive position Trying to adjust margin for a position that is currently inactive.
422 Cannot remove margin more than available in position Attempting to reduce margin by an amount greater than what is available in the position.
422 Liquidation will be triggered instantly Liquidation of the position will occur immediately due to specific conditions.
422 Max Y USDT can be removed Maximum amount of Y USDT can be withdrawn from the position.
400 Insufficient funds Wallet doesn't have sufficient funds for the requested action.
```

## Cancel All Open Orders

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
"timestamp": timeStamp,  # EPOCH timestamp in seconds
    "margin_currency_short_name": ["USDT"],
}

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/cancel_all_open_orders>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
{
   "message":"success",
   "status":200,
   "code":200
}

Use this endpoint to cancel all the open orders till time.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/cancel_all_open_orders>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
margin_currency_short_name Array OPTIONAL Futures margin mode.
Default value - ["USDT"]. Possible values INR & USDT.
```

## Cancel All Open Orders for Position

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
    "timestamp":timeStamp , # EPOCH timestamp in seconds
    "id": "434dc174-6503-4509-8b2b-71b325fe417a" # position.id
    }

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/cancel_all_open_orders_for_position>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
{
   "message":"success",
   "status":200,
   "code":200
}

Use this endpoint to cancel all the open orders by passing the position id.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/cancel_all_open_orders_for_position>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
id String YES Position id
```

## Exit Position

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
    "timestamp": timeStamp , # EPOCH timestamp in seconds
    "id": "434dc174-6503-4509-8b2b-71b325fe417a" # position.id
    }

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/exit>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
{
   "message":"success",
   "status":200,
   "code":200,
   "data":{
      "group_id":"baf926e6B-ID_USDT1705647709"
   }
}

Use this endpoint to exit position by passing position id.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/exit>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
id String YES Position id
```

## Response Defnitions

```python
KEY DESCRIPTION
message
status
code
group_id Group id is an id which is used whenever a large order is split into smaller parts. System auto-splits the exit order into smaller parts if the order size is huge. All the split parts will have the same group id.
Create Take Profit and Stop Loss Orders
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
  "timestamp": timeStamp, # EPOCH timestamp in seconds
  "id": "e65e8b77-fe7c-40c3-ada1-b1d4ea40465f", # position.id
  "take_profit": {
      "stop_price": "1",
      "limit_price": "0.9", # required for take_profit_limit orders
      "order_type": "take_profit_limit" # take_profit_limit OR take_profit_market
  },
  "stop_loss": {
      "stop_price": "0.271",
      "limit_price": "0.270", # required for stop_limit orders
      "order_type": "stop_limit" # stop_limit OR stop_market
  }
}

json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/create_tpsl>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
{
   "stop_loss":{
      "id":"8f8ee959-36cb-4932-bf3c-5c4294f21fec",
      "pair":"B-ID_USDT",
      "side":"sell",
      "status":"untriggered",
      "order_type":"stop_limit",
      "stop_trigger_instruction":"last_price",
      "notification":"email_notification",
      "leverage":1.0,
      "maker_fee":0.025,
      "taker_fee":0.075,
      "fee_amount":0.0,
      "price":0.27,
      "stop_price":0.271,
      "avg_price":0.0,
      "total_quantity":0.0,
      "remaining_quantity":0.0,
      "cancelled_quantity":0.0,
      "ideal_margin":0.0,
      "order_category":"complete_tpsl",
      "stage":"tpsl_exit",
      "group_id":"None",
      "display_message":"None",
      "group_status":"None",
      "margin_currency_short_name" : "INR",
      "settlement_currency_conversion_price": 89.0,
      "created_at":1705915027938,
      "updated_at":1705915028003
   },
   "take_profit":{
      "success":false,
      "error":"TP already exists"
   }
}
Use this endpoint to create the profit and stop loss order by passing necessary parameters.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/create_tpsl>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
id String YES Position id
take_profit - stop_price String YES Stop price is the trigger price of the take profit order
take_profit - limit_price String NO Limit price - Ignore this for now. This is not supported.
take_profit - order_type String YES Order type - Only “take_profit_market” is supported for now
stop_loss - stop_price String YES Stop price is the trigger price of the stop loss order
stop_loss - limit_price String NO Limit price - Ignore this for now. This is not supported.
stop_loss - order_type String YES Order type - Only “stop_market” is supported for now
```

## Response Defnitions

```
KEY DESCRIPTION
id
pair
side
status You’ll get this as "untriggered" for all the newly placed orders. Use the list order endpoint to track the status of the order. Large orders may be split into smaller orders; in that case, the group id can be used to track the statuses of all the child orders at once.
order_type
stop_trigger_instruction Ignore this
notification
leverage
maker_fee
taker_fee
fee_amount
price
stop_price
avg_price
total_quantity
remaining_quantity
cancelled_quantity
ideal_margin
order_category
stage
group_id
display_message Ignore this
group_status Ignore this
margin_currency_short_name Position margin mode
settlement_currency_conversion_price USDT <> INR conversion price when the order is placed
created_at
updated_at
success This will be false if a take profit (TP) or stop loss (SL) creation fails
error Reason for failure
```

## Get Transactions

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
  "timestamp": timeStamp, # EPOCH timestamp in seconds
  "stage": "all", # all OR default OR funding
  "page": "1", #no. of pages needed
  "size": "10" #no. of records needed
}
json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/transactions>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
[
   {
    "pair": "B-BTC_USDT",
    "stage": "default",
    "amount": 0.0,
    "fee_amount": 8.899963104,
    "price_in_inr": 1.0,
    "price_in_btc": 1.85407055628e-07,
    "price_in_usdt": 0.011572734637194769,
    "source": "user",
    "parent_type": "Derivatives::Futures::Order",
    "parent_id": "061a7f36-daaf-4349-97c0-47bad7d08f5e",
    "settlement_amount": 0.0,
    "margin_currency_short_name": "INR",
    "position_id": "beecde3c-7fe6-11ef-bd3a-5b8a901688d3",
    "created_at": 1728459094499,
    "updated_at": 1728459094499
  }
]

Use this endpoint to get the list of transactions by passing the position ids and stage ( all OR default )
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/transactions>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
stage String YES Funding: Transactions created due to funding
Default: Transactions created for any order placed other than quick exit and full position tpsl
Exit: Transactions created for quick exit orders
Tpsl_exit: Transactions created for full position tpsl_exit orders
Liquidation: Transactions created for liquidation orders
page String YES Required page number
size String YES Number of records needed per page
margin_currency_short_name Array OPTIONAL Futures margin mode.
Default value - ["USDT"]. Possible values INR & USDT.
```

## Response Defnitions

```
Note : "amount", "fee_amount", "settlement_amount" will show in INR for INR margined Futures and in USDT for USDT margined Futures.

KEY DESCRIPTION
pair
stage
amount This represents the PnL (Profit and Loss) from this particular transaction.
fee_amount This represents the fee charged per transaction. A transaction is created for every trade of the order.
price_in_inr Trade price in terms of INR.
price_in_btc Trade price in terms of BTC.
price_in_usdt Trade price in terms of USDT.
source Source will be “user” for the orders placed by the users and will be “system” for the orders placed by the system. Liquidation orders are placed by the system.
parent_type
parent_id
position_id
settlement_amount Ignore this
margin_currency_short_name Futures margin mode
created_at
updated_at
```

## Get Trades

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
  "timestamp":timeStamp, # EPOCH timestamp in seconds
  "pair": "B-ID_USDT", # instrument.pair
  "order_id": "9b37c924-d8cf-4a0b-8475-cc8a2b14b962", # order.id
  "from_date": "2024-01-01", # format YYYY-MM-DD
  "to_date": "2024-01-22", # format YYYY-MM-DD
  "page": "1", #no. of pages needed
  "size": "10" #no. of records needed
}
json_body = json.dumps(body, separators = (',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/trades>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data = json_body, headers = headers)
data = response.json()
print(data)
```

## Response

```json
[
  {
     "price":0.2962,
     "quantity":33.0,
     "is_maker":false,
     "fee_amount":0.00733095,
     "pair":"B-ID_USDT",
     "side":"buy",
     "timestamp":1705645534425.8374,
     "order_id":"9b37c924-d8cf-4a0b-8475-cc8a2b14b962",
     "settlement_currency_conversion_price": 0.0,
     "margin_currency_short_name": "USDT"

  }
]

Use this endpoint to all the trades information by passing the pair, order id and from and to date.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/trades>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
pair String YES Name of the pair
order_id String OPTIONAL Order ID
from_date String YES Start date in format YYYY-MM-DD
to_date String YES End date in format YYYY-MM-DD
page String YES Required page number
size String YES Number of records needed per page
margin_currency_short_name Array YES Futures margin mode.
Default value - ["USDT"]. Possible values INR & USDT.
```

## Response Defnitions

```
Note : fee_amount value is in USDT for INR Futures.

KEY DESCRIPTION
price
quantity
is_maker
fee_amount
pair
side
timestamp
order_id
settlement_currency_conversion_price USDT <> INR conversion price when the order is placed
margin_currency_short_name Futures margin mode
```

## Get Current Prices RT

```python
import requests  # Install requests module first.
url = "<https://public.coindcx.com/market_data/v3/current_prices/futures/rt>"
response = requests.get(url)
data = response.json()
print(data)
print(json.dumps(data, indent=2))
```

## Response

```json
{
  "ts": 1720429586580,
  "vs": 54009972,
  "prices": {
    "B-NTRN_USDT": {
      "fr": 5e-05,
      "h": 0.4027,
      "l": 0.3525,
      "v": 18568384.9349,
      "ls": 0.4012,
      "pc": 4.834,
      "mkt": "NTRNUSDT",
      "btST": 1720429583629,
      "ctRT": 1720429584517,
      "skw": -207,
      "mp": 0.40114525,
      "efr": 5e-05,
      "bmST": 1720429586000,
      "cmRT": 1720429586117
    },
    "B-1000SHIB_USDT": {
      "fr": -0.00011894,
      "h": 0.017099,
      "l": 0.014712,
      "v": 358042914.374195,
      "ls": 0.016909,
      "pc": 2.578,
      "mkt": "1000SHIBUSDT",
      "btST": 1720429586359,
      "ctRT": 1720429586517,
      "skw": -207,
      "mp": 0.01691261,
      "efr": -9.115e-05,
      "bmST": 1720429586000,
      "cmRT": 1720429586117
    }
  }
}

Use this endpoint to get the current prices.
```

## HTTP Request

```
GET <https://public.coindcx.com/market_data/v3/current_prices/futures/rt>
```

## Response Defnitions

```
KEY DESCRIPTION
fr
h high
l low
v volume
ls
pc price change percent
mkt
btST TPE Tick send time
ctRT
skw
mp
efr
bmST TPE mark price send time (The timestamp at which Third-Party exchange sent this event)
cmRT
```

## Get Pair Stats

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "xxx"
secret = "yyy"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))
body = {
    "timestamp": timeStamp,  # EPOCH timestamp in seconds
}
json_body = json.dumps(body, separators=(',', ':'))
signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()
url = "<https://api.coindcx.com/api/v1/derivatives/futures/data/stats?pair=B-ETH_USDT>"
headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}
response = requests.get(url, data=json_body, headers=headers)
data = response.json()
print(json.dumps(data, indent=2))
```

## Response

```json
{
  "price_change_percent": {
    "1H": -0.15,
    "1D": 1.41,
    "1W": -11.95,
    "1M": -17.34
  },
  "high_and_low": {
    "1D": {
      "h": 3098.0,
      "l": 2821.26
    },
    "1W": {
      "h": 3498.91,
      "l": 2800.0
    }
  },
  "position": {
    "count_percent": {
      "long": 93.2,
      "short": 6.8
    },
    "value_percent": {
      "long": 91.48,
      "short": 8.52
    }
  }
}

Use this endpoint to all the trades information by passing the pair.
```

## HTTP Request

```
POST <https://api.coindcx.com/api/v1/derivatives/futures/data/stats?pair=B-ETH_USDT>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
pair String YES Name of the pair
```

## Response Defnitions

```
KEY DESCRIPTION
price_change_percent
high_and_low
1H Hour
1D Day
1W Week
1M Month
l
position
count_percent
long
short
value_percent
```

## Get Cross Margin Details

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "xxx"
secret = "yyy"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

# secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))
body = {
    "timestamp": timeStamp
}
json_body = json.dumps(body, separators=(',', ':'))
signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()
url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/cross_margin_details>"
headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}
response = requests.get(url, data=json_body, headers=headers)
data = response.json()
print(json.dumps(data, indent=2))
```

## Response

```json
{
  "pnl": -0.0635144,
  "maintenance_margin": 0.10170128,
  "available_wallet_balance": 7.16966176,
  "total_wallet_balance": 7.16966176,
  "total_initial_margin": 0.68534648,
  "total_initial_margin_isolated": 0.0,
  "total_initial_margin_crossed": 0.68534648,
  "total_open_order_initial_margin_crossed": 0.0,
  "available_balance_cross": 6.42080088,
  "available_balance_isolated": 6.42080088,
  "margin_ratio_cross": 0.01431173,
  "withdrawable_balance": 6.42080088,
  "total_account_equity": 7.10614736,
  "updated_at": 1720526407542
}

Use this endpoint to get the cross margin details
Note : Cross margin mode is not supported on INR margined Futures.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/cross_margin_details>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
```

## Response Defnitions

```
KEY DESCRIPTION
pnl This is gives your unrealised PnL in cross margin positions
maintenance_margin Cumulative maintenance margin of all the cross margined positions
available_wallet_balance Ignore this
total_wallet_balance Total wallet balance excluding the PnL, funding and fees of active positions
total_initial_margin Cumulative maintenance margin for cross and isolated margined positions and orders
total_initial_margin_isolated Cumulative maintenance margin for isolated margined positions and orders
total_initial_margin_crossed Cumulative maintenance margin for Cross margined positions (Excluding orders)
total_open_order_initial_margin_crossed Cumulative initial margin locked for open orders
available_balance_cross Balance available for trading in Cross Margin mode
available_balance_isolated Balance available for trading in Isolated Margin mode
margin_ratio_cross Margin ratio of the positions in cross margin mode. Your Cross positions will get liquidated if the ratio becomes greater than equal to 1
withdrawable_balance Balance that can be withdrawn to spot wallet from futures wallet
total_account_equity total_wallet_balance plus pnl
updated_at Ignore this
```

## Wallet Transfer

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "xxx"
secret = "yyy"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

# secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))
body = {
    "timestamp": timeStamp,
    "transfer_type": "withdraw", # "deposit" OR "withdraw" (to/from DF wallet)
    "amount": 1,
    "currency_short_name": "USDT"

}
json_body = json.dumps(body, separators=(',', ':'))
signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()
url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/wallets/transfer>"
headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}
response = requests.post(url, data=json_body, headers=headers)
data = response.json()
print(json.dumps(data, indent=2))
```

## Response

```json
[
  {
    "id": "c5f039dd-4e11-4304-8f91-e9c1f62d754d",
    "currency_short_name": "USDT",
    "balance": "6.1693226",
    "locked_balance": "0.0",
    "cross_order_margin": "0.0",
    "cross_user_margin": "0.68534648"
  }
]
Use this endpoint to transfer money from spot to futures wallet and vice-versa
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/wallets/transfer>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
transfer_type String YES "deposit" for depositing funds to futures wallet "withdraw" for withdrawing funds from future wallet
amount Integer YES Amount in terms of input currency
currency_short_name String YES “USDT” for transferring USDT, "INR" for transferring INR
```

## Response Defnitions

```
KEY DESCRIPTION
id Transaction id
currency_short_name Currency that was transferred
balance Ignore this
locked_balance Total initial margin locked in isolated margined orders and positions
cross_order_margin Total initial margin locked in cross margined orders
cross_user_margin Total initial margin locked in cross margined positions
NOTE :
To calculate total wallet balance, use this formulae:
Total wallet balance = balance + locked_balance
```

## Wallet Details

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "xxx"
secret = "yyy"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

# secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))
body = {
    "timestamp": timeStamp

}
json_body = json.dumps(body, separators=(',', ':'))
signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()
url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/wallets>"
headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}
response = requests.get(url, data=json_body, headers=headers)
data = response.json()
print(json.dumps(data, indent=2))
```

## Response

```json
[
  {
    "id": "c5f039dd-4e11-4304-8f91-e9c1f62d754d",
    "currency_short_name": "USDT",
    "balance": "6.1693226",
    "locked_balance": "0.0",
    "cross_order_margin": "0.0",
    "cross_user_margin": "0.68534648"
  }
]

Use this endpoint to fetch the wallet details for both INR & USDT Futures Wallet.
```

## HTTP Request

```
GET <https://api.coindcx.com/exchange/v1/derivatives/futures/wallets>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
```

## Response Defnitions

```
KEY DESCRIPTION
id Futures wallet id
currency_short_name Currency of wallet
balance Ignore this
locked_balance Total initial margin locked in isolated margined orders and positions
cross_order_margin Total initial margin locked in cross margined orders
cross_user_margin Total initial margin locked in cross margined positions
```

## Wallet Transactions

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "xxx"
secret = "yyy"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

# secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))
body = {
    "timestamp": timeStamp

}
json_body = json.dumps(body, separators=(',', ':'))
signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()
url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/wallets/transactions?page=1&size=1000>"
headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}
response = requests.get(url, data=json_body, headers=headers)
data = response.json()
print(json.dumps(data, indent=2))
```

## Response

```json
[
  {
    "derivatives_futures_wallet_id": "c5f039dd-4e11-4304-8f91-e9c1f62d754d",
    "transaction_type": "debit",
    "amount": 1.0,
    "currency_short_name": "USDT",
    "currency_full_name": "Tether",
    "reason": "by_universal_wallet",
    "created_at": 1720547024000
  }
]
Use this endpoint to fetch the list of wallet transactions for both INR & USDT Futures Wallet.
```

## HTTP Request

```
GET <https://api.coindcx.com/exchange/v1/derivatives/futures/wallets/transactions?page=1&size=1000>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
```

## Response Defnitions

```
KEY DESCRIPTION
derivatives_futures_wallet_id Futures wallet id
transaction_type Credit (into futures wallet) or debit (from futures wallet)
amount Transaction amount
currency_short_name Currency of wallet
currency_full_name Currency full name of wallet
reason Reason will be
by_universal_wallet: For transfers between spot and futures wallets.
by_futures_order: For all the transactions created due to a futures order
by_futures_funding: For all the transaction created due to funding (only applicable for fundings that occur in cross margined positions)
<!--
by_adjust_position_settlement: Created while removing margin from INR margined positions to account for the difference in the Avg. USDT<>INR conversion of the position and the current USDT<>INR conversion price.
-->
created_at Timestamp at which the transaction got created
```

## Edit Order

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "xxx"
secret = "xxx"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

# secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
    "timestamp": timeStamp,
    "id": "dd456ab4-4a7d-11ef-a287-bf3cd92be693",
    "total_quantity": 12,
    "price": 0.999501,
    "take_profit_price": 64000.0,
    "stop_loss_price": 61000.0

}

json_body = json.dumps(body, separators=(',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/orders/edit>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data=json_body, headers=headers)
data = response.json()
print(json.dumps(data, indent=2))
```

## Response

```json
[
  {
    "id": "dd456ab4-4a7d-11ef-a287-bf3cd92be693",
    "pair": "B-USDC_USDT",
    "side": "buy",
    "status": "open",
    "order_type": "limit_order",
    "stop_trigger_instruction": "last_price",
    "notification": "email_notification",
    "leverage": 5.0,
    "maker_fee": 0.025,
    "taker_fee": 0.074,
    "liquidation_fee": null,
    "fee_amount": 0.0,
    "price": 0.999501,
    "stop_price": 0.0,
    "avg_price": 0.0,
    "total_quantity": 12.0,
    "remaining_quantity": 12.0,
    "cancelled_quantity": 0.0,
    "ideal_margin": 2.402352627552,
    "locked_margin": 2.402352627552,
    "order_category": null,
    "position_margin_type": "isolated",
    "stage": "default",
    "created_at": 1721908991520,
    "updated_at": 1721909127960,
    "trades": [],
    "display_message": "Order edited successfully",
    "group_status": null,
    "group_id": null,
    "metatags": null,
    "take_profit_price": 64000.0,
    "stop_loss_price": 61000.0
  }
]
Use this endpoint to edit the order which is in open status.
Note : Edit order is only supported on USDT margined Futures at the moment.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/orders/edit>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
id String YES Order id
total_quantity Integer YES New total quantity of the order
price Integer YES New price of the order
take_profit_price Decimal OPTIONAL Take profit price. This value should only be sent for market_order, limit_order. These values will not be accepted for orders that reduce the position size (Note that no error will be raised in such cases)
stop_loss_price Decimal OPTIONAL Stop loss price. This value should only be sent for market_order, limit_order. These values will not be accepted for orders that reduce the position size (Note that no error will be raised in such cases)
```

## Response Defnitions

```
KEY DESCRIPTION
id Order id
pair Name of the futures pair
side Side buy / sell
status Ignore this (It will be initial for all the newly placed orders)
order_type Order type. Possible values are:
limit - a type of order where the execution price will be no worse than the order's set price. The execution price is limited to be the set price or better.
market - A type of order where the user buys or sells an asset at the best available prices and liquidity until the order is fully filled or the order book's liquidity is exhausted.
stop_market - once the market price hits the stopPrice, a market order is placed on the order book.
stop_limit - once the market price hits the stopPrice, a limit order is placed on the order book at the limit price.
take_profit_market - once the market price hits the stopPrice, a market order is placed on the order book.
take_profit_limit - once the market price hits the stopPrice, a limit order is placed on the order book at the limit price.
stop_trigger_instruction
notification no_notification OR email_notification. If property is set as email_notification then you will get an email once the order is filled
leverage This is the leverage at which you want to take a position. This has to be the same as the leverage of the position. Else the order will be rejected. You should preferably set the leverage before placing the order to avoid order rejection. Leverage needs to be set only once post which it will be saved in the system for that particular pair.
maker_fee Applicable fee if the trade received for the order is a maker trade
taker_fee Applicable fee if the trade received for the order is a taker trade
liquidation_fee Applicable fee if the trade received for the order is a trade for the liquidation order
fee_amount This will be the fee that has been charged for the user till now. As soon as the order is placed, this value will be zero until you start receiving trades for the order
price Order Price (limit price for limit, stop limit and take profit limit orders). Keep this NULL for market orders. Else the order will be rejected.
stop_price
avg_price It will be zero for the newly placed orders. You can check the latest fill price from the list orders endpoint
total_quantity Total quantity of the order
remaining_quantity Remaining quantity of the order that is still open on the exchange and can get filled
cancelled_quantity Quantity of the order that is canceled and won’t be filled
ideal_margin This is the margin that is required for placing this order. You will see the ideal margin as non-zero even for reduce orders but the actual margin locked for reduce orders will be 0. This number is only for reference purpose.
locked_margin
order_category Ignore this
position_margin_type “crossed” if the order was placed for cross margin position. “Isolated” if the order is placed for isolated margin position. Please consider NULL also as isolated.
stage default - Standard limit, market, stop limit, stop market, take profit limit or take profit market order
exit - Quick exit which closes the entire position
liquidate - Order which was created by the system to liquidate a futures position
tpsl_exit - Take profit or stop loss order which was placed to close the entire futures position
trades
group_id Group id is an id which is used whenever a large order is split into smaller parts. System auto-splits the market variant orders like quick exit order, liquidate order and tpsl_exit order into smaller parts if the order size is huge. All the split parts will have the same group id
metatags
display_message Ignore this
group_status Ignore this
created_at Timestamp at which the order was created
updated_at Last updated timestamp of the order
take_profit_price Take Profit Trigger: Once your order begins to fill, this take profit trigger will update any existing open TP/SL order and will apply to your entire position. Note: Take profit triggers attached to reduce-only orders will be ignored.
stop_loss_price Stop Loss Trigger: Once your order begins to fill, this stop loss trigger will update any existing open TP/SL order and will apply to your entire position. Note: Stop loss triggers attached to reduce-only orders will be ignored.
```

## Change Position Margin Type

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "xxx"
secret = "yyy"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

# secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
    "timestamp": timeStamp,
    "pair": "B-JTO_USDT",
    "margin_type": "isolated",  # "isolated" or "crossed"
}

json_body = json.dumps(body, separators=(',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/exchange/v1/derivatives/futures/positions/margin_type>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.post(url, data=json_body, headers=headers)
data = response.json()
print(json.dumps(data, indent=2))
```

## Response

```json
[
  {
    "id": "6bcb26f8-4a7d-11ef-b553-6bef92793bf4",
    "pair": "B-JTO_USDT",
    "active_pos": 0.0,
    "inactive_pos_buy": 0.0,
    "inactive_pos_sell": 0.0,
    "avg_price": 0.0,
    "liquidation_price": 0.0,
    "locked_margin": 0.0,
    "locked_user_margin": 0.0,
    "locked_order_margin": 0.0,
    "take_profit_trigger": null,
    "stop_loss_trigger": null,
    "margin_type": "isolated",
    "leverage": 5.0,
    "mark_price": 0.0,
    "maintenance_margin": 0.0,
    "updated_at": 1721978237197
  }
]
Use this endpoint to change the margin type from "isolated" to "crossed" and vice-versa. You can only update the margin type when you don't have any active position or open orders in the instrument.

Note : Cross margin mode is only supported on USDT margined Futures at the moment.
```

## HTTP Request

```
POST <https://api.coindcx.com/exchange/v1/derivatives/futures/positions/margin_type>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
pair String YES Instrument Pair name, Format: B-BTC_USDT, B-ETH_USDT, etc
margin_type Integer YES “Isolated” or “crossed”
```

## Response Defnitions

```
KEY DESCRIPTION
id Position id
pair Name of the futures pair
active_pos Quantity of the position in terms of underlying. For example, if active_pos = 1 for B-ETH_USDT then you hold 1 quantity ETH Futures contract. For short positions, active_pos will be negative.
inactive_pos_buy Sum of the open quantities of the pending buy orders.
inactive_pos_sell Sum of the open quantities of the pending sell orders.
avg_price Average entry price of the position.
liquidation_price Price at which the position will get liquidated. This is applicable only for positions with isolated margin. Ignore this for cross margined positions.
locked_margin Margin (in USDT) locked in the position after debiting fees and adjusting funding from the initial investment.
locked_user_margin Margin (in USDT) that was initially invested in the futures position excluding fees and funding.
locked_order_margin Total margin in USDT that is locked in the open orders.
take_profit_trigger Trigger price set for Full Position take profit order.
stop_loss_trigger Trigger price set for Full position stop loss order.
margin_type “crossed” if the order was placed for cross margin position.“Isolated” if the order is placed for isolated margin position.Please consider NULL also as isolated.
leverage Leverage of the position
maintenance_margin The amount of margin required to be maintained in the account to avoid liquidation. For cross margined positions, the maintenance margin required is equal to the sum of the maintenance margins of all the positions
mark_price Mark price at the time when the position was last updated. Note that this value is not real-time and is only for reference purpose.
updated_at Ignore this
```

## Get Currency Conversion

```python
import hmac
import hashlib
import base64
import json
import time
import requests

# Enter your API Key and Secret here. If you don't have one, you can generate it from the website

key = "XXXX"
secret = "YYYY"

# python3

secret_bytes = bytes(secret, encoding='utf-8')

# python2

# secret_bytes = bytes(secret)

# Generating a timestamp

timeStamp = int(round(time.time() * 1000))

body = {
    "timestamp": timeStamp,
}

json_body = json.dumps(body, separators=(',', ':'))

signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()

url = "<https://api.coindcx.com/api/v1/derivatives/futures/data/conversions>"

headers = {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': key,
    'X-AUTH-SIGNATURE': signature
}

response = requests.get(url, data=json_body, headers=headers)
data = response.json()
print(json.dumps(data, indent=2))
```

## Response

```json
[
  {
    "symbol": "USDTINR",
    "margin_currency_short_name": "INR",
    "target_currency_short_name": "USDT",
    "conversion_price": 89.0,
    "last_updated_at": 1728460492399
  }
]
Use this endpoint to get the USDT currency conversion price in INR .
```

## HTTP Request

```
POST <https://api.coindcx.com/api/v1/derivatives/futures/data/conversions>
```

## Request Defnitions

```
Name Type Mandatory Description
timestamp Integer YES EPOCH timestamp in seconds
```

## Response Defnitions

```
KEY DESCRIPTION
symbol Symbol Name
margin_currency_short_name INR
target_currency_short_name USDT
conversion_price When using INR margin, CoinDCX notionally converts INR to USDT & vice-versa at this conversion rate. This conversion rate may change periodically due to extreme market movements.
last_updated_at Timestamp at which the fixed conversion price was last changed.
```
