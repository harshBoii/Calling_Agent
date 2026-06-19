from arq.connections import RedisSettings

from arq_jobs import run_call_job
from config import CAMPAIGN_QUEUE, REDIS_URL


class CampaignWorkerSettings:
    functions = [run_call_job]
    queue_name = CAMPAIGN_QUEUE
    max_jobs = 6
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
