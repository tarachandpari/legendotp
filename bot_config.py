from aiogram import Bot, Dispatcher
from aiogram.contrib.fsm_storage.memory import MemoryStorage
import os

# Load sensitive values from environment variables
TOKEN = os.getenv('TELEGRAM_TOKEN', '7353143614:AAF6bDLQXUh11V6YBHjF2qk-WEO2cINJgZo')
AZURE_SPEECH_KEY = os.getenv('AZURE_SPEECH_KEY', '16fe2d6d31804d55b4ba47d61308e065')
AZURE_SPEECH_REGION = os.getenv('AZURE_SPEECH_REGION', 'centralindia')

# Telegram bot setup
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
