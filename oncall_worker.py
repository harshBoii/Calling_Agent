from arq.connections import RedisSettings

from arq_jobs import run_call_job
from config import ONCALL_QUEUE, REDIS_URL


class OnDemandWorkerSettings:
    functions = [run_call_job]
    queue_name = ONCALL_QUEUE
    max_jobs = 2
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
