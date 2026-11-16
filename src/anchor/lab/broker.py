"""Loopback-only development fallback when system Mosquitto is unavailable."""
import asyncio
import logging
import signal

from amqtt.broker import Broker
from .config import BROKER_PORT, RUNTIME, server_cert


async def main():
    logging.basicConfig(level=logging.ERROR)
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{BROKER_PORT}",
                    "ssl": True, "certfile": server_cert(), "keyfile": str(RUNTIME / "certs" / "localhost.key")}},
                    "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    done = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, done.set)
    await broker.start()
    await done.wait()
    await broker.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
