from threading import Thread
from pubsub import pub
import time
import logging

logger = logging.getLogger(__name__)


class Ticker:

    def __init__(self):
        self.tickers = {}
        self.running = False

    def subscribe(self, tick_callable, interval):
        self.start_ticker(interval)
        pub.subscribe(tick_callable, "tick" + interval)

    def start_ticker(self, interval):
        """Start a ticker/timer that notifies market watchers when to pull a new candle"""
        if interval not in self.tickers:
            self.tickers[interval] = Thread(
                target=self.__start_ticker, args=(
                    interval,)).start()

    def __start_ticker(self, interval):
        """Start a ticker own its own thread, will use pypubsub to send a message each time interval"""
        logger.info(interval + " ticker running...")
        live_tick_count = 0
        self.running = True
        while self.running:
            """Running this 'ticker' from the main loop to trigger listeners to pull candles every 5 minutes"""
            logger.info("Live Tick: {}".format(str(live_tick_count)))
            print(interval + " tick")
            pub.sendMessage("tick" + interval)
            live_tick_count += 1
            time.sleep(self.__convert_interval_to_int(interval))

    def __convert_interval_to_int(self, interval):
        if interval == "15s":
            return 15
        if interval == "1m":
            return 60
        if interval == "5m":
            return 300
        if interval == "15m":
            return 900
        if interval == "1h":
            return 3600

    # TODO: Killing the ticker kills all the interval tickers?
    def stop_ticker(self, interval):
        self.running = False
