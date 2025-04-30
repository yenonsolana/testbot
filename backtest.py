import json, argparse, strategy, asyncio
parser=argparse.ArgumentParser(); parser.add_argument("file")
args=parser.parse_args()
strategy.init_balance(1_000)
async def feed():
    with open(args.file) as f:
        for line in f:
            msg=json.loads(line); await strategy.on_pump(msg, None)
asyncio.run(feed())
print("Balance finale €", strategy.balance)
