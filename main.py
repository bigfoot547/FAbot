import asyncio
import bot
import json

bot_secrets: {}
config: {}


async def amain():
    the_bot = bot.FABot(config['bot'], bot_secrets)
    await the_bot.connect(config['uplink']['host'], config['uplink']['port'], config['uplink']['ssl'])


def main():
    with open('secrets.json', 'r') as fp:
        global bot_secrets
        bot_secrets = json.load(fp)

    with open('config.json', 'r') as fp:
        global config
        config = json.load(fp)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(amain())
    loop.close()


if __name__ == "__main__":
    main()
