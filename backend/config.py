from dotenv import load_dotenv
import os

load_dotenv()  # reads .env file into os.environ

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
CELERY_BROKER_URL = os.environ["CELERY_BROKER_URL"]
CELERY_RESULT_BACKEND = os.environ["CELERY_RESULT_BACKEND"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
