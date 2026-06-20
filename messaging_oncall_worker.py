from arq.connections import RedisSettings

from config import MESSAGES_ONCALL_QUEUE, REDIS_URL
from message_jobs import run_message_job


class MessagingOnDemandWorkerSettings:
    functions = [run_message_job]
    queue_name = MESSAGES_ONCALL_QUEUE
    max_jobs = 2
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
