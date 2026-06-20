from arq.connections import RedisSettings

from config import MESSAGES_CAMPAIGN_QUEUE, REDIS_URL
from message_jobs import run_message_job


class MessagingCampaignWorkerSettings:
    functions = [run_message_job]
    queue_name = MESSAGES_CAMPAIGN_QUEUE
    max_jobs = 6
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
