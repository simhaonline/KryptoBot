from celery.signals import worker_ready
from .celery import app
from ..config import core
from ...bots.bot import Bot
from ..base_task import BaseTask
from ...db.models import Strategy
import importlib


def title_case(snake_str):
    components = snake_str.split('_')
    return ''.join(x.title() for x in components)


def dynamic_import(abs_module_path, class_name):
    module_object = importlib.import_module(abs_module_path)
    target_class = getattr(module_object, class_name)
    return target_class


@worker_ready.connect
def load_open_strategies(sender, **kwargs):
    # This works, just not in use yet
    # session = core.session()
    # for job in session.query(Strategy).filter(Strategy.status == 'active'):
    #     job.params['config'] = core.config
    #     schedule_t2_strategy.apply_async(
    #         None,
    #         {'params': job.params},
    #         task_id=job.celery_id
    #     )
    # session.close()
    pass


@app.task(base=BaseTask)
def schedule_t2_strategy(params):
    Strat = dynamic_import(
        'kryptobot.strategies.t2.' + params['strategy'],
        title_case(params['strategy'])
    )
    bot = Bot(
        Strat(
            params['default'],
            params['limits'],
            params['custom'],
            params['portfolio'],
            strategy_id=params['strategy_id'],
            portfolio_id=params['portfolio_id']
        ),
        config=params['config']
    )
    return bot.start()


@app.task(base=BaseTask)
def stop_strategy(id):
    app.control.revoke(id)