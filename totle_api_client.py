import sys
import traceback
import http.client
import json
import requests
import argparse
import datetime

##############################################################################################
#
# Library functions for exchanges, tokens, and prices
#
# get exchanges
r = requests.get('https://services.totlesystem.com/exchanges').json()
exchanges = { e['name']: e['id'] for e in r['exchanges'] }
exchange_by_id = { e['id']: e['name'] for e in r['exchanges'] }


# get tokens
r = requests.get('https://services.totlesystem.com/tokens').json()
tokens = { t['symbol']: t['address'] for t in r['tokens'] if t['tradable']}
token_symbols = { t['address']: t['symbol'] for t in r['tokens'] if t['tradable']}
token_decimals = { t['symbol']: t['decimals'] for t in r['tokens'] if t['tradable']}
ETH_ADDRESS = "0x0000000000000000000000000000000000000000" 

def addr(token):
    """Returns the string address that identifies the token"""
    # convert 'ETH' to addr 0x000... in anticipation of fix to swap
    return ETH_ADDRESS if token == 'ETH' else tokens[token]

def int_amount(float_amount, token):
    """Returns the integer amount of token units for the given float_amount and token"""
    return int(float(float_amount) * (10**token_decimals[token]))

def real_amount(int_amount, token):
    """Returns the decimal number of tokens for the given integer amount and token"""
    return int(int_amount) / (10**token_decimals[token])

# get all token prices on all exchanges
all_prices = requests.get('https://services.totlesystem.com/tokens/prices').json()['response']

# We assume that the prices endpoints returns the lowest 'ask' and highest 'bid' price for
# a given token. If it does not, then that would explain why rebalance returns orders
# with lower prices than the ask price
def price(token, exchange):
    """Returns lowest ask price in ETH for the given token on the given exchange"""
    return float(all_prices[tokens[token]][str(exchanges[exchange])]['ask'])

def best_ask_price(token):
    """Returns lowest ask price in ETH for the given token across all exchanges"""
    ap = all_prices[tokens[token]]
    return min([ float(ap[v]['ask']) for v in ap ])

def best_bid_price(token):
    """Returns highest bid price in ETH for the given token across all exchanges"""
    ap = all_prices[tokens[token]]
    return max([ float(ap[v]['bid']) for v in ap ])

def best_prices(token, bidask='ask'):
    """Returns lowest ask or highest bid prices in ETH for all exchanges"""
    token_prices = all_prices[tokens[token]]
    return { exchange_by_id[int(i)]: float(token_prices[i][bidask]) for i in token_prices }

def show_prices(from_token, to_token):
    if from_token == 'ETH':
        print(f"Lowest ask prices for {to_token}: {pp(best_prices(to_token, 'ask'))}")
    elif to_token == 'ETH':
        print(f"Highest bid prices for {from_token}: {pp(best_prices(from_token, 'bid'))}")
    else:
        # once ERC20/ERC20 swaps, which have both buys and sells, are supported by the swap_data
        # extractor method, then we can get rid of the exception below and do something useful
        raise Exception("ERC20/ERC20 swaps haven't been implemented yet")

def wei_to_eth(wei_amount):
    """Returns a decimal value of the amount of ETH for the given wei_amount"""
    return int(wei_amount) / 10**18

def pp(data):
    return json.dumps(data, indent=3)

def all_liquid_tokens(min_exchanges=2):
    """returns all the tokens for which price data exists on at least min_exchanges DEXs"""
    if min_exchanges > len(exchanges):
        raise Exception(f"min_exchanges set to {min_exchanges}, but there are only {len(exchanges)} possible")

    taddrs = [ ta for ta in all_prices if ta in token_symbols ]

    return [ token_symbols[taddr] for taddr in taddrs if len(all_prices[taddr]) > min_exchanges ] 

##############################################################################################
#
# functions to call swap/rebalance and extract data
#
def swap_data(response):
    """Extracts relevant data from a swap/rebalance API endpoint response"""
    summary = response['summary']
    buys, sells = summary['buys'], summary['sells']

    if len(buys) + len(sells) == 0: # Suggester has no orders
        return {}


    # This method assumes the summary includes either a single buy or sell, i.e. that rebalance 
    # was called with a single buy or sell or (someday) that swap was called with an ETH pair
    # This method would need to be modified to handle results from a call to swap with an
    # ERC20/ERC20 pair, which would contain both buys and sells.
    if len(buys) + len(sells) > 1:
        err_msg = f"expected payload to have either 1 buy or 1 sell, but it has {len(buys)} buys and {len(sells)} sells.\nresponse={pp(response)}"
        raise Exception(err_msg)

    action, bsd = ("buy", buys[0]) if buys else ("sell", sells[0])

    token_sym = token_symbols[bsd['token']]

    x = real_amount(bsd['amount'], token_sym)
    
    return {
        "action": action,
        "weiAmount": int(response['ethValue']),
        "ethAmount": wei_to_eth(response['ethValue']),
        "token": bsd['token'],
        "tokenSymbol": token_sym,
        "exchange": bsd['exchange'],
        "price": float(bsd['price']),
        "intAmount": int(bsd['amount']),
        "realAmount": x,
        "fee": bsd['fee']
    }

# Default parameters for swap. These can be overridden by passing params
DEFAULT_WALLET_ADDRESS = "0xD18CEC4907b50f4eDa4a197a50b619741E921B4D"
DEFAULT_TRADE_SIZE = 1.0 # the amount of ETH to spend or acquire, used to calculate amount
DEFAULT_MIN_SLIPPAGE_PERCENT = 10
DEFAULT_MIN_FILL_PERCENT = 80

def call_swap(from_token, to_token, exchange=None, params=None, debug=None):
    """Calls the swap API endpoint with the given token pair and whitelisting exchange if given. Returns the result as a swap_data dict """
    # the swap_data dict is defined by the return statement in swap_data method above

    # trade_size is not an endpoint input so we extract it from params (after making a local copy)
    params = dict(params)
    trade_size = params.pop('tradeSize') if params and 'tradeSize' in params else DEFAULT_TRADE_SIZE

    base_inputs = {
        "address": DEFAULT_WALLET_ADDRESS,
        "minSlippagePercent": DEFAULT_MIN_SLIPPAGE_PERCENT,
        "minFillPercent": DEFAULT_MIN_FILL_PERCENT
    }
    if params:
        base_inputs = {**base_inputs, **params}

    if exchange: # whitelist the given exchange:
        base_inputs["exchanges"] = { "list": [ exchanges[exchange] ], "type": "white" } 

    from_token_addr = addr(from_token)
    to_token_addr = addr(to_token)

    if from_token_addr == ETH_ADDRESS and to_token_addr == ETH_ADDRESS:
        raise Exception('from_token and to_token cannot both be ETH')

    if from_token_addr != ETH_ADDRESS and to_token_addr != ETH_ADDRESS:
        swap_endpoint = 'https://services.totlesystem.com/swap'
        real_amount_to_sell = trade_size / best_bid_price(from_token)
        amount_to_sell = int_amount(real_amount_to_sell, from_token)
        if debug: print(f"selling {real_amount_to_sell} {from_token} tokens ({amount_to_sell} units)")
        swap_inputs = {
            "swap": {
                "from": from_token_addr,
                "to": to_token_addr,
                "amount": amount_to_sell
            },
        }
    
    else: # for now we have to call the rebalance endpoint because swap doesn't support ETH
        swap_endpoint = 'https://services.totlesystem.com/rebalance'

        if from_token_addr == ETH_ADDRESS and to_token_addr != ETH_ADDRESS:
            real_amount_to_buy = trade_size / best_ask_price(to_token)
            amount_to_buy = int_amount(real_amount_to_buy, to_token)
            if debug: print(f"buying {real_amount_to_buy} {to_token} tokens ({amount_to_buy} units)")
            swap_inputs = {
                "buys": [ {
                    "token": addr(to_token),
                    "amount": amount_to_buy
                } ],
            }

        else: # from_token_addr != ETH_ADDRESS and to_token_addr == ETH_ADDRESS
            real_amount_to_sell = trade_size / best_bid_price(from_token)
            amount_to_sell = int_amount(real_amount_to_sell, from_token)
            if debug: print(f"selling {real_amount_to_sell} {from_token} tokens ({amount_to_sell} units)")
            swap_inputs = {
                "sells": [ {
                    "token": addr(from_token),
                    "amount": amount_to_sell
                } ],
            }


    swap_inputs = pp({**swap_inputs, **base_inputs})
    if debug: print(f"REQUEST to {swap_endpoint}:\n{swap_inputs}\n\n")
    r = requests.post(swap_endpoint, data=swap_inputs)

    try:
        j = r.json()
    except Exception as e:
        print(f"cannot extract JSON REQUEST:\n{swap_inputs}\ncannot extract JSON RESPONSE={r.text} ")
        raise Exception(f"cannot extract JSON from response. Response={r.text}")

    if debug: print(f"RESPONSE from {swap_endpoint}:\n{pp(j)}\n\n")

    if j['success']:
        return swap_data(j['response'])
    else:
        print(f"FAILED REQUEST:\n{swap_inputs}\n")
        print(f"FAILED RESPONSE:\n{pp(j)}\n\n")
        raise Exception(j['response'])

def print_results(label, sd):
    """Prints a formatted results string based on given label and swap_data sd"""
    # This should ultimately be used to send output to a CSV or some file that calculations
    # can be run on
    try:
        print(f"{label}: {sd['action']} {sd['realAmount']} {sd['tokenSymbol']} for {sd['ethAmount']} ETH on {sd['exchange']} price={sd['price']} fee={sd['fee']}")
    except Exception as e:
        raise Exception(f"print_results raised {e}, received {label} {sd}")

def print_price_comparisons(swap_prices, token):
    if len(swap_prices) > 1: # there is data to compare
        for k in swap_prices:
            if k != 'Totle':
                totle_discount = 100 - (100.0 * (swap_prices['Totle'] / swap_prices[k]))
                if swap_prices['Totle'] < 0.0:
                    print(f"Totle savings could not be computed since Totle received an invalid price={swap_prices['Totle']} buying {token}")
                elif swap_prices[k] < 0.0:
                    print(f"Totle savings could not be computed since {k} received an invalid price={swap_prices[k]} buying {token}")
                else:
                    print(f"Totle saved {totle_discount:.2f} percent vs {k} buying {token}")
    else:
        print(f"No {token} prices for comparison were found on other DEXs")

def compare_prices(from_token, to_token, params=None, debug=False):
    """Returns a dict containing Totle and other DEX prices"""

    swap_prices = {}

    # Get the best price using Totle's aggregated order books
    try:
        totle_sd = call_swap(from_token, to_token, params=params, debug=debug)
        if debug: print(pp(totle_sd))

        if totle_sd:
            print_results('Totle', totle_sd)
            swap_prices['Totle'] = totle_sd['price']

            # Compare to best prices from other DEXs
            for dex in best_prices(to_token):
                if dex != totle_sd['exchange']:
                    try:
                        dex_sd = call_swap(from_token, to_token, dex, params=params, debug=debug)
                        if dex_sd:
                            print_results(dex, dex_sd)
                            swap_prices[dex] = dex_sd['price']
                        else:
                            print(f"{dex}: Suggester returned no orders for {from_token}->{to_token}")
                    except Exception as e:
                        print(f"{dex}: swap raised {e}")

            print_price_comparisons(swap_prices, to_token)
        else:
            print(f"Totle: Suggester returned no orders for {from_token}->{to_token}")

    except Exception as e:
        print(f"{'Totle'}: swap raised {e}")
        # traceback.print_tb(sys.exc_info()[2])


##############################################################################################
#
# Main program
#

# Accept tradeSize, minSlippagePercent, and minFillPercent as program arguments
parser = argparse.ArgumentParser(description='Run price comparisons')
parser.add_argument('tradeSize', type=float, nargs='?', default=DEFAULT_TRADE_SIZE, help='the size (in ETH) of the order')
parser.add_argument('minSlippagePercent', nargs='?', default=DEFAULT_MIN_SLIPPAGE_PERCENT, type=float, help='acceptable percent of slippage')
parser.add_argument('minFillPercent', nargs='?', default=DEFAULT_MIN_FILL_PERCENT, type=float, help='acceptable percent of amount to acquire')

params = vars(parser.parse_args())

# Redirect output to a .txt file in outputs directory
# Comment out the following 3 lines to see output on console
d = datetime.datetime.today()
output_filename = f"outputs/{d.year}-{d.month:02d}-{d.day:02d}_{d.hour:02d}-{d.minute:02d}-{d.second:02d}_{params['tradeSize']}-{params['minSlippagePercent']}-{params['minFillPercent']}.txt"
print(f"sending output to {output_filename} ...")
sys.stdout = open(output_filename, 'w')

print(d, params)

TOKENS_TO_BUY = all_liquid_tokens()

# For now, all price comparisons are done by buying the ERC20 token with ETH (i.e. from_token == 'ETH')
from_token = 'ETH'

for to_token in TOKENS_TO_BUY:
    print(f"\n----------------------------------------\n{to_token}")
    if to_token not in tokens:
        print(f"'{to_token}' is not a listed token or is not tradable")
        continue
    show_prices(from_token, to_token)
    compare_prices(from_token, to_token, params=params, debug=False)
