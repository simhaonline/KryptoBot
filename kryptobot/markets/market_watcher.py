from collections import defaultdict
from threading import Thread
from queue import Queue
import logging
import time
from datetime import datetime
from pubsub import pub
from threading import Lock
from ..db.models import Ohlcv, TradingPair
from ..db.timescale import get_candle_gaps
from .market import ccxt

lock = Lock()
logger = logging.getLogger(__name__)


class MarketWatcher:
    """Active object that subscribes to a ticker of a specific interval and keeps track of OHLCV data
     A market watcher is instantiated with a trading pair (base, quote) and an interval
     It then subscribes to the ticker of that interval and calls for candles each time period
     It is responsible for syncing data with the DB and adding new candles
     Strategies that subscribe to the ticker will be given the new candles"""
    def __init__(self, exchange, base_currency, quote_currency, interval, session, ticker):
        exchange = getattr(ccxt, exchange)
        self.ticker = ticker()
        self.ticker.subscribe(self.tick, interval)
        self.analysis_pair = '{}/{}'.format(base_currency, quote_currency)
        self.exchange = exchange()
        self.interval = interval
        self.base_currency = base_currency
        self.quote_currency = quote_currency
        self.topic = self.exchange.id + self.analysis_pair + self.interval
        self.__thread = Thread(target=self.__run)  # create thread for listener
        self._jobs = Queue()  # create job queue
        self.__running = False
        self.historical_synced = False
        self.latest_candle = None
        self.session = session()
        self.pair_id = self.write_trade_pairs_to_db(self.exchange.id, self.base_currency, self.quote_currency, self.interval)
        self.__thread.start()

    def __del__(self):
        self.session.close()

    def __run(self):
        """Start listener queue waiting for ticks"""
        self.__running = True
        self.sync_historical()
        while self.__running:
            if not self._jobs.empty():
                job = self._jobs.get()
                try:
                    job()
                except Exception as e:
                    print(e)
                    logger.error(job.__name__ + " threw error:\n" + str(e))

    def stop(self):
        """Stop listener queue"""
        self.__running = False
        self.ticker.stop_ticker(self.interval)

    def tick(self):
        """Queue a pull of the latest candle"""
        if self.historical_synced:
            self._jobs.put(lambda: self.__pull_latest_candle(self.interval))

    def write_trade_pairs_to_db(self, exchange_id, base, quote, interval):
        """Returns the ID of the trade pair"""
        logger.info("Writing market data pair to DB")
        pair = self.session.query(TradingPair).filter(
            TradingPair.exchange == exchange_id,
            TradingPair.base_currency == base,
            TradingPair.quote_currency == quote,
            TradingPair.interval == interval,
        ).first()
        if pair is None:
            pair = TradingPair(
                exchange=exchange_id,
                base_currency=base,
                quote_currency=quote,
                interval=interval
            )
            self.session.add(pair)
            self.session.commit()
        else:
            logger.info("Market data already available for pair, returning ID for lookups")
        return pair.id

    def normalize_candle_timestamps(self, candle_set):
        # TODO: For cryptopia and others who give to the minute timestamps
        pass

    def get_candle_date_range(self, start_date, end_date):
        gaps = self.check_candle_date_range(start_date, end_date)
        if gaps is True:
            return self.query_candle_date_range(start_date, end_date)
        else:
            self.fill_candle_gaps(gaps)
            return self.query_candle_date_range(start_date, end_date)

    # NOTE: This will only work with timescaledb and postgres
    def check_candle_date_range(self, start_date, end_date):
        gaps = get_candle_gaps(
            self.session,
            start_date,
            end_date,
            self.interval,
            self.exchange.id,
            self.analysis_pair
        )
        if len(gaps) == 0:
            return True
        else:
            return gaps

    # NOTE: This will only work with timescaledb and postgres
    def fill_candle_gaps(self, gaps):
        step = 1000
        retry_limit = 3
        retries = 0
        gaps = [convert_date_to_timestamp(g) for g in gaps]
        while len(gaps) > 0:
            earliest_date = convert_timestamp_to_date(gaps[0])
            latest_date = convert_timestamp_to_date(gaps[-1])
            print('filling gaps in candles', len(gaps))
            print('date range:', earliest_date, latest_date)
            # TODO: Clean this and other stupid parts up
            if earliest_date == latest_date:
                break
            candles = self.query_ccxt(earliest_date, step)
            good_count, gaps, next_start = self.merge_candles(candles, gaps)
            next_start = datetime.strptime(next_start, '%Y-%m-%d %H:%M:%S')
            latest_date = datetime.strptime(latest_date, '%Y-%m-%d %H:%M:%S')
            if next_start >= latest_date or next_start >= datetime.now():
                break
            if good_count == step or retries == retry_limit:
                gaps = [g for g in gaps if g >= convert_date_to_timestamp(next_start)]
                retries = 0
            else:
                retries = retries + 1
            time.sleep(self.exchange.rateLimit / 1000)

    def query_ccxt(self, start_date, limit=1000):
        return self.exchange.fetch_ohlcv(
            self.analysis_pair,
            timeframe=self.interval,
            since=convert_date_to_timestamp(start_date),
            limit=limit
        )

    # TODO: Get this to work for exchange data that is simply missing
    # Always dynamic, never stored, so must wrap final db call
    # possible make optional with a flag
    def interpolate_missing_candles(self, candles):
        for c in candles:
            print(c)
        return candles

    def merge_candles(self, candles, gaps):
        end_date = convert_timestamp_to_date(candles[-1][0])
        good_count = 0
        for entry in candles:
            if entry[0] > gaps[0] and entry[0] < gaps[-1]:
                good_count = good_count + 1
            if entry[0] in gaps:
                gaps.remove(entry[0])
                ohlcv = Ohlcv(
                    exchange=self.exchange.id,
                    pair=self.analysis_pair,
                    interval=self.interval,
                    pair_id=self.pair_id,
                    timestamp=convert_timestamp_to_date(entry[0]),
                    timestamp_raw=entry[0],
                    open=entry[1],
                    high=entry[2],
                    low=entry[3],
                    close=entry[4],
                    volume=entry[5]
                )
                self.session.add(ohlcv)
                print('Writing candle ' + str(entry[0]) + ' to database')
        self.session.commit()
        return good_count, gaps, end_date

    def query_candle_date_range(self, start_date, end_date):
        data = self.session.query(Ohlcv).filter(
            Ohlcv.exchange == self.exchange.id,
            Ohlcv.pair_id == self.pair_id,
            Ohlcv.interval == self.interval,
            Ohlcv.timestamp > start_date,
            Ohlcv.timestamp < end_date
        ).all()
        return [(d.timestamp_raw, d.open, d.high, d.low, d.close, d.volume) for d in data]

    def sync_historical(self):
        """Queue loading of historical candles"""
        self._jobs.put(lambda: self.__sync_historical())

    def get_historical_candles(self):
        data = self.session.query(Ohlcv).filter(
            Ohlcv.exchange == self.exchange.id,
            Ohlcv.pair_id == self.pair_id,
            Ohlcv.interval == self.interval
        ).all()
        return [(d.timestamp_raw, d.open, d.high, d.low, d.close, d.volume) for d in data]

    def __sync_historical(self):
        """Load all missing historical candles to database"""
        logger.info('Syncing market candles with DB...')
        latest_db_candle = self.session.query(Ohlcv).filter(
            Ohlcv.exchange == self.exchange.id,
            Ohlcv.pair_id == self.pair_id,
            Ohlcv.interval == self.interval
        ).order_by(Ohlcv.timestamp_raw.desc()).first()
        data = self.exchange.fetch_ohlcv(self.analysis_pair, self.interval)
        if latest_db_candle is None:
            logger.info("No historical data for market, adding all available OHLCV data")
            for entry in data:
                ohlcv = Ohlcv(
                    exchange=self.exchange.id,
                    pair=self.analysis_pair,
                    interval=self.interval,
                    pair_id=self.pair_id,
                    timestamp=convert_timestamp_to_date(entry[0]),
                    timestamp_raw=entry[0],
                    open=entry[1],
                    high=entry[2],
                    low=entry[3],
                    close=entry[4],
                    volume=entry[5]
                )
                self.session.add(ohlcv)
                print('Writing candle ' + str(entry[0]) + ' to database')
        else:
            for entry in data:
                if not latest_db_candle.timestamp_raw >= entry[0]:
                    ohlcv = Ohlcv(
                        exchange=self.exchange.id,
                        pair=self.analysis_pair,
                        interval=self.interval,
                        pair_id=self.pair_id,
                        timestamp=convert_timestamp_to_date(entry[0]),
                        timestamp_raw=entry[0],
                        open=entry[1],
                        high=entry[2],
                        low=entry[3],
                        close=entry[4],
                        volume=entry[5]
                    )
                    self.session.add(ohlcv)
                    print('Writing missing candle ' + str(entry[0]) + ' to database')
        self.session.commit()
        self.historical_synced = True
        # TODO: Seperate these
        # historical preps on launch
        # backtest currently launches, needlessly syncs historical
        # then the strategy resquests the date range and this watcher then
        # catches the data up
        pub.sendMessage(self.topic + "backtest")
        pub.sendMessage(self.topic + "historical")
        logger.info('Market data has been synced.')

    def __pull_latest_candle(self, interval):
        """Initiate a pull of the latest candle, making sure not to pull a duplicate candle"""
        logger.info("Getting latest candle for " + self.exchange.id + " " + self.analysis_pair + " " + interval)
        print("Getting latest candle")
        try:
            latest_data = self.exchange.fetch_ohlcv(self.analysis_pair, interval)[-1]
            while latest_data == self.latest_candle:
                print("retrying candle update")
                logger.info('Candle already contained in DB, retrying...')
                time.sleep(self.exchange.rateLimit * 2 / 1000)
                latest_data = self.exchange.fetch_ohlcv(self.analysis_pair, interval)[-1]
            ohlcv = Ohlcv(
                exchange=self.exchange.id,
                pair=self.analysis_pair,
                interval=interval,
                pair_id=self.pair_id,
                timestamp=convert_timestamp_to_date(latest_data[0]),
                timestamp_raw=latest_data[0],
                open=latest_data[1],
                high=latest_data[2],
                low=latest_data[3],
                close=latest_data[4],
                volume=latest_data[5]
            )
            self.session.add(ohlcv)
            self.session.commit()
        except Exception as e:
            print(e)
            logger.info("Timeout pulling latest candle, trying again")
            self.__pull_latest_candle(interval)
            return
        self.latest_candle = latest_data
        pub.sendMessage(self.topic, candle=self.latest_candle)
        print("Sent message to " + self.topic)


lookup_list = defaultdict(MarketWatcher)


def get_market_watcher(exchange_id, base, quote, interval, session=None, ticker=None):
    """Return or create market watcher for the given analysis market"""
    topic = str(exchange_id + base + "/" + quote + interval)
    if topic not in lookup_list:
        lookup_list[topic] = MarketWatcher(exchange_id, base, quote, interval, session, ticker)
    return lookup_list[topic]


def subscribe_historical(exchange_id, base, quote, interval, callable, session, ticker):
    """Subscribe to a notification that is sent when historical data is loaded for the market given"""
    topic = str(exchange_id + base + "/" + quote + interval + "historical")
    pub.subscribe(callable, topic)


def subscribe_backtest(exchange_id, base, quote, interval, callable, session, ticker):
    """Subscribe to a notification that is sent when backtest data is loaded for the market given"""
    topic = str(exchange_id + base + "/" + quote + interval + "backtest")
    pub.subscribe(callable, topic)


def subscribe(exchange_id, base, quote, interval, callable, session, ticker):
    """
    Enroll strategy to recieve new candles from a market
    :param exchange_id: string representing exchange i.e. 'bittrex'
    :param base: string represeting base i.e. ETH
    :param base: string represeting quote i.e. BTC
    :param interval: string representing interval i.e. '5m'
    :param callable: method to recieve new candle (must take candle as a param)
    :return: none
    """
    with lock:
        topic = str(exchange_id + base + "/" + quote + interval)
        get_market_watcher(exchange_id, base, quote, interval, session, ticker)
        print("Subscribing to " + topic)
        pub.subscribe(callable, topic)


def stop_watcher(exchange_id, base, quote, interval):
    with lock:
        watcher = get_market_watcher(exchange_id, base, quote, interval)
        if watcher is not None:
            watcher.stop()


def convert_timestamp_to_date(timestamp):
    value = datetime.fromtimestamp(float(str(timestamp)[:-3]))
    return value.strftime('%Y-%m-%d %H:%M:%S')


def convert_date_to_timestamp(date):
    if isinstance(date, str):
        date = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
    return int(str(int(date.timestamp())) + '000')
