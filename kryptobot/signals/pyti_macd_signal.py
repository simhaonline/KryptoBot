from ..signals.base_signal_generator import BaseSignalGenerator
from ..ta.pyti_exponential_moving_average import PytiEma
from ..ta.pyti_macd import PytiMacd


class PytiMacdSignal(BaseSignalGenerator):

    def __init__(self, market, interval, params, strategy):
        super().__init__(market, interval, strategy)
        short = params.pop('short_window', 12)
        long = params.pop('long_window', 26)
        signal = params.pop('signal_window', 9)
        self.last_signal = None
        self.repeat_count = 0
        self.repeat_limit = 0
        # TODO: Don't actually need these two ema's for the signal
        self.ema_short = PytiEma(
            market,
            interval,
            short
        )
        self.ema_long = PytiEma(
            market,
            interval,
            long
        )
        self.macd = PytiMacd(
            market,
            interval,
            long,
            {
                'short_window': short,
                'signal_window': signal
            }
        )

    def check_condition(self, new_candle):
        macd = self.macd.value
        ema = {
            'ema_short': self.ema_short.value,
            'ema_long': self.ema_long.value
        }

        # print('macd', macd)

        self.strategy.add_message({
            'timestamp': new_candle[0],
            'open': new_candle[1],
            'high': new_candle[2],
            'low': new_candle[3],
            'close': new_candle[4],
            'volume': new_candle[5],
            'macd': macd,
            'ema': ema,
            'positions': self.strategy.get_open_position_count(),
            'quote_balance': self.market.get_wallet_balance(),
            'base_balance': self.market.base_balance,
            'exit_balance': self.market.get_wallet_balance()
                + (self.market.base_balance * ((new_candle[2] + new_candle[3])/ 2))
        }, 'db')

        if macd['crossover'] > 0:
            signal = 'buy'

        if macd['crossover'] < 0:
            signal = 'sell'

        # NOTE: This does seem to work better than buying all the way up
        # Also it seems better to up the limit and only buy on the first signal
        if signal == self.last_signal and self.repeat_count < self.repeat_limit:
            self.repeat_count = self.repeat_count + 1
            self.last_signal = signal
            return signal

        if signal != self.last_signal:
            self.repeat_count = 0
            self.last_signal = signal
            return signal

        self.last_signal = signal
        return 'hold'
