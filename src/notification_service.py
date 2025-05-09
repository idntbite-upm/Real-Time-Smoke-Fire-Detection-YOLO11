# notification_service.py
from concurrent.futures import ThreadPoolExecutor
from cryptography.fernet import Fernet
import json
import os
import requests
import cv2
import time
import logging
import asyncio
import telegram
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import quote_plus
from filelock import FileLock
from io import BytesIO
from twilio.rest import Client  # For Twilio WhatsApp

# Add GCS imports
from google.cloud import storage
from google.oauth2 import service_account
import uuid

# Setup environment and logging
PROJECT_ROOT = Path(__file__).parent.parent
ENV = PROJECT_ROOT / '.env'
load_dotenv(ENV, override=True)
logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, config):
        """Initialize notification services"""
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.config = config
        # Create new event loop for this instance
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._init_services()
        self._init_gcs()

    def _init_gcs(self):
        """Initialize Google Cloud Storage client"""
        try:
            # Get GCS credentials from env
            gcs_key_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
            bucket_name = os.getenv('GCS_BUCKET_NAME')
            
            if not gcs_key_path or not bucket_name:
                logger.warning("GCS credentials or bucket name missing, GCS storage disabled")
                self.gcs_enabled = False
                return
                
            # Initialize GCS client
            if Path(gcs_key_path).exists():
                self.credentials = service_account.Credentials.from_service_account_file(gcs_key_path)
                self.storage_client = storage.Client(credentials=self.credentials)
                self.bucket = self.storage_client.bucket(bucket_name)
                self.gcs_enabled = True
                self.gcs_bucket_name = bucket_name
                logger.info(f"GCS initialized with bucket: {bucket_name}")
            else:
                logger.error(f"GCS credentials file not found: {gcs_key_path}")
                self.gcs_enabled = False
        except Exception as e:
            logger.error(f"GCS initialization failed: {str(e)}")
            self.gcs_enabled = False

    def _init_services(self):
        """Initialize and validate notification providers"""
        # Twilio WhatsApp initialization
        twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
        twilio_token = os.getenv("TWILIO_AUTH_TOKEN")
        twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
        receiver = os.getenv("RECEIVER_WHATSAPP_NUMBER")
        
        if all([twilio_sid, twilio_token, twilio_number, receiver]):
            try:
                self.twilio_client = Client(twilio_sid, twilio_token)
                # Format numbers for WhatsApp API (whatsapp: prefix)
                if not twilio_number.startswith('whatsapp:'):
                    self.twilio_whatsapp_number = f"whatsapp:{twilio_number}"
                else:
                    self.twilio_whatsapp_number = twilio_number
                    
                # Make sure the receiver number has correct format
                if not receiver.startswith('whatsapp:'):
                    if not receiver.startswith('+'):
                        self.receiver_whatsapp_number = f"whatsapp:+{receiver}"
                    else:
                        self.receiver_whatsapp_number = f"whatsapp:{receiver}"
                else:
                    self.receiver_whatsapp_number = receiver
                    
                self.whatsapp_enabled = True
                self.use_twilio = True
                logger.info("Twilio WhatsApp service initialized")
            except Exception as e:
                logger.error(f"Twilio WhatsApp initialization failed: {e}")
                self.whatsapp_enabled = False
                self.use_twilio = False
        else:
            self.use_twilio = False
            logger.info("Twilio WhatsApp not configured")
        
        # Fallback to CallMeBot if Twilio is not available
        if all([os.getenv("CALLMEBOT_API_KEY"), os.getenv("RECEIVER_WHATSAPP_NUMBER")]) and not self.use_twilio:
            self.whatsapp_enabled = True
            self.use_callmebot = True
            self.base_url = "https://api.callmebot.com/whatsapp.php"
            logger.info("CallMeBot WhatsApp service initialized (legacy fallback)")
        elif not self.use_twilio:
            self.whatsapp_enabled = False
            self.use_callmebot = False
            logger.warning("WhatsApp alerts disabled: No WhatsApp service available")

        # Telegram initialization
        if token := os.getenv("TELEGRAM_TOKEN"):
            try:
                self.telegram_bot = FlareGuardBot(
                    token, os.getenv("TELEGRAM_CHAT_ID"))
                # Run all async initialization together
                if not self.loop.is_running():
                    self.loop.run_until_complete(self._init_telegram())
            except Exception as e:
                logger.error(f"Telegram setup failed: {e}")
                self.telegram_bot = None
        else:
            logger.info("Telegram alerts disabled: Missing token")

    async def _init_telegram(self):
        """Async initialization for Telegram"""
        await self.telegram_bot.initialize()
        logger.info("Telegram service initialized")

    def save_frame(self, frame) -> Path:
        """Save detection frame with timestamp"""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        filename = self.config.DETECTED_FIRES_DIR / f'alert_{timestamp}.jpg'
        cv2.imwrite(str(filename), frame)
        return filename

    def upload_to_gcs(self, image_path: Path) -> str:
        """Upload image to Google Cloud Storage"""
        if not self.gcs_enabled:
            logger.warning("GCS upload skipped: GCS not enabled")
            return None
            
        try:
            # Create a unique blob name with timestamp
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            unique_id = str(uuid.uuid4())[:8]
            blob_name = f"fire_alerts/{timestamp}_{unique_id}.jpg"
            
            # Create a new blob and upload the file
            blob = self.bucket.blob(blob_name)
            blob.upload_from_filename(str(image_path))
            
            # Make the blob publicly accessible (optional, based on your security needs)
            blob.make_public()
            
            # Get the public URL
            image_url = blob.public_url
            logger.info(f"Image uploaded to GCS: {image_url}")
            return image_url
            
        except Exception as e:
            logger.error(f"GCS upload error: {str(e)}")
            return None

    def upload_image(self, image_path: Path) -> str:
        """Upload image, using GCS with Imgur fallback"""
        # First try GCS if enabled
        if hasattr(self, 'gcs_enabled') and self.gcs_enabled:
            image_url = self.upload_to_gcs(image_path)
            if image_url:
                return image_url
            logger.warning("GCS upload failed, falling back to Imgur")
        
        # Fallback to Imgur
        try:
            # Ensure the file exists
            if not image_path.exists():
                logger.error(f"Image path does not exist: {image_path}")
                return None
                
            # Open file properly for uploading
            with open(image_path, 'rb') as image_file:
                # Read the file content
                image_data = image_file.read()
                
                # Send the image data to Imgur
                response = requests.post(
                    'https://api.imgur.com/3/image',
                    headers={
                        'Authorization': f'Client-ID {self.config.IMGUR_CLIENT_ID}'
                    },
                    files={'image': ('image.jpg', image_data, 'image/jpeg')},
                    timeout=10
                )
                
            response.raise_for_status()
            data = response.json().get('data', {})
            if 'link' not in data:
                logger.error("Imgur response missing 'link'")
                return None
            return data['link']
        except requests.exceptions.HTTPError as e:
            try:
                error_msg = response.json().get('data', {}).get('error', 'Unknown error')
                logger.error(f"Imgur Error: {error_msg}")
            except:
                logger.error(f"HTTP Error: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Image upload failed: {str(e)}")
            return None

    def send_alert(self, frame, detection: str = "Fire") -> bool:
        """Non-blocking alert dispatch"""
        image_path = self.save_frame(frame)

        # Submit to background thread
        future = self.executor.submit(
            self._send_alerts_async,
            image_path,
            detection
        )

        # Error logging callback
        future.add_done_callback(
            lambda f: f.exception() and logger.error(
                f"Alert error: {f.exception()}")
        )

        return True  # Immediate success assumption

    def _send_alerts_async(self, image_path, detection):
        """Background alert processing"""
        if self.whatsapp_enabled:
            self._send_whatsapp_alert(image_path, detection)
        if hasattr(self, 'telegram_bot') and self.telegram_bot:
            self._send_telegram_alert(image_path, detection)

    def _send_whatsapp_alert(self, image_path, detection):
        """Handle WhatsApp notification with multiple fallback options"""
        try:
            # Upload the image
            image_url = self.upload_image(image_path)
            
            # Prepare the message (with or without image URL)
            if image_url:
                message = f"🚨 {detection} Detected! View at {image_url}"
                logger.info(f"Image uploaded successfully: {image_url}")
            else:
                message = f"🚨 {detection} Detected! (Image attachment failed)"
                logger.warning("Image upload failed - will try direct sending if available")
            
            # Choose the appropriate method based on available services
            if hasattr(self, 'use_twilio') and self.use_twilio:
                # Twilio implementation with media attachment options
                try:
                    # Prepare message parameters
                    message_params = {
                        'body': message,
                        'from_': self.twilio_whatsapp_number,
                        'to': self.receiver_whatsapp_number,
                    }
                    
                    # Try to add media if we have a direct image path and cloud upload failed
                    if not image_url and hasattr(self, 'gcs_enabled') and self.gcs_enabled:
                        try:
                            # Create a temporary signed URL if image_url is not available
                            blob = self.bucket.blob(f"temp_media/{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg")
                            blob.upload_from_filename(str(image_path))
                            
                            # Generate a signed URL that expires in 1 hour
                            signed_url = blob.generate_signed_url(
                                version="v4",
                                expiration=datetime.timedelta(hours=1),
                                method="GET"
                            )
                            
                            # Add media URL to message
                            message_params['media_url'] = [signed_url]
                            logger.info(f"Using temporary signed URL for media: {signed_url}")
                        except Exception as media_error:
                            logger.error(f"Media URL generation failed: {str(media_error)}")
                    
                    # Send the message
                    message_response = self.twilio_client.messages.create(**message_params)
                    logger.info(f"Twilio WhatsApp alert delivered: {message_response.sid}")
                    return True
                    
                except Exception as e:
                    logger.error(f"Twilio WhatsApp alert failed: {str(e)}")
                    # Try fallback to CallMeBot if configured
                    if hasattr(self, 'use_callmebot') and self.use_callmebot:
                        logger.info("Falling back to CallMeBot...")
                        return self._send_callmebot_message(message)
                    return False
                    
            elif hasattr(self, 'use_callmebot') and self.use_callmebot:
                # Use CallMeBot as primary or fallback
                return self._send_callmebot_message(message)
            else:
                logger.error("No WhatsApp service is available")
                return False
                
        except Exception as e:
            logger.error(f"WhatsApp alert failed: {str(e)}")
            return False

    def _send_telegram_alert(self, image_path, detection):
        """Handle Telegram notification with proper loop management"""
        try:
            if not self.loop.is_running():
                asyncio.set_event_loop(self.loop)
                return self.loop.run_until_complete(
                    self.telegram_bot.send_alert(
                        image_path=image_path,
                        caption=f"🚨 {detection} Detected!"
                    )
                )
        except Exception as e:
            logger.error(f"Telegram alert failed: {str(e)}")
            return False

    def send_test_message(self):
        """Verify system connectivity"""
        success = False
        if self.whatsapp_enabled:
            test_msg = "🔧 System Test: Fire Detection System Operational"
            
            if hasattr(self, 'use_twilio') and self.use_twilio:
                success = self._send_test_twilio_message(test_msg)
            elif hasattr(self, 'use_callmebot') and self.use_callmebot:
                success = self._send_callmebot_message(test_msg)
                
        if hasattr(self, 'telegram_bot') and self.telegram_bot:
            try:
                test_image = Path(PROJECT_ROOT, 'data', "test_image.png")
                success |= self.loop.run_until_complete(
                    self.telegram_bot.send_test_alert(test_image))
            except Exception as e:
                logger.error(f"Telegram test failed: {e}")
                
        return success

    def _send_test_twilio_message(self, message):
        """Send a test message through Twilio"""
        try:
            message_response = self.twilio_client.messages.create(
                body=message,
                from_=self.twilio_whatsapp_number,
                to=self.receiver_whatsapp_number
            )
            logger.info(f"Twilio WhatsApp test message sent: {message_response.sid}")
            return True
        except Exception as e:
            logger.error(f"Twilio WhatsApp test failed: {str(e)}")
            return False

    def _send_callmebot_message(self, message: str) -> bool:
        """Core WhatsApp message sender for CallMeBot (legacy)"""
        try:
            encoded_msg = quote_plus(message)
            url = f"{self.base_url}?" \
                f"phone={os.getenv('RECEIVER_WHATSAPP_NUMBER')}&" \
                f"text={encoded_msg}&" \
                f"apikey={os.getenv('CALLMEBOT_API_KEY')}"

            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                logger.info("WhatsApp alert delivered via CallMeBot")
                return True
            logger.warning(
                f"WhatsApp Alert Attempt failed: HTTP {response.status_code}")
            return False
        except Exception as e:
            logger.error(f"CallMeBot error: {str(e)}")
            return False

    def cleanup(self):
        """Proper cleanup of resources"""
        try:
            self.executor.shutdown(wait=True)
            if hasattr(self, 'loop') and not self.loop.is_closed():
                # Cancel all pending tasks
                for task in asyncio.all_tasks(self.loop):
                    task.cancel()
                # Run loop one final time to complete cancellation
                self.loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(self.loop), return_exceptions=True))
                self.loop.close()
        except Exception as e:
            logger.error(f"Cleanup error: {str(e)}")

    def __del__(self):
        """Ensure cleanup is called"""
        self.cleanup()


class FlareGuardBot:
    def __init__(self, token: str, default_chat_id: str = None):
        self.logger = logging.getLogger(__name__)
        self.token = token
        self.default_chat_id = default_chat_id
        self.bot = telegram.Bot(token=self.token)
        self._init_crypto()
        self.storage_file = Path(__file__).parent / "sysdata.bin"
        self.update_file = Path(__file__).parent / "last_update.bin" 
        self.chat_ids = self._load_chat_ids()

    async def initialize(self):
        """Async initialization sequence"""
        await self._update_chat_ids()

    def _init_crypto(self):
        """Initialize encryption system"""
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            raise ValueError("ENCRYPTION_KEY environment variable required")
        self.cipher_suite = Fernet(key.encode())

    def _load_chat_ids(self):
        """Load encrypted chat IDs from secure storage with file locking"""
        try:
            if self.storage_file.exists():
                with FileLock(str(self.storage_file) + ".lock"):
                    self.storage_file.chmod(0o600)
                    with open(self.storage_file, "rb") as f:
                        encrypted_data = f.read()
                        decrypted = self.cipher_suite.decrypt(encrypted_data)
                        ids = json.loads(decrypted)
                        if not all(isinstance(i, int) for i in ids):
                            raise ValueError("Invalid chat ID format")
                        return list(set(ids))  # Remove duplicates
            return []
        except Exception as e:
            self.logger.error(f"Failed to load chat IDs: {e}")
            return []

    def _save_chat_ids(self):
        """Securely store chat IDs with encryption and file locking"""
        try:
            with FileLock(str(self.storage_file) + ".lock"):
                encrypted = self.cipher_suite.encrypt(
                    json.dumps(list(set(self.chat_ids))).encode()
                )
                with open(self.storage_file, "wb") as f:
                    f.write(encrypted)
                self.storage_file.chmod(0o600)
        except Exception as e:
            self.logger.error(f"Failed to save chat IDs: {e}")

    def _get_last_update_id(self):
        """Get the encrypted ID of the last processed update"""
        try:
            if self.update_file.exists():
                with FileLock(str(self.update_file) + ".lock"):
                    self.update_file.chmod(0o600)
                    with open(self.update_file, "rb") as f:
                        encrypted_data = f.read()
                        decrypted = self.cipher_suite.decrypt(encrypted_data)
                        return int(decrypted.decode())
        except Exception as e:
            self.logger.error(f"Failed to read last update ID: {e}")
        return 0

    def _save_last_update_id(self, update_id: int):
        """Save the encrypted ID of the last processed update"""
        try:
            with FileLock(str(self.update_file) + ".lock"):
                encrypted = self.cipher_suite.encrypt(str(update_id).encode())
                with open(self.update_file, "wb") as f:
                    f.write(encrypted)
                self.update_file.chmod(0o600)
        except Exception as e:
            self.logger.error(f"Failed to save last update ID: {e}")

    async def _update_chat_ids(self):
        """Discover and store new chat IDs securely with offset handling"""
        try:
            offset = self._get_last_update_id()
            updates = await self.bot.get_updates(offset=offset + 1, timeout=30)

            new_ids = []
            for update in updates:
                if update.message and update.message.chat_id:
                    chat_id = update.message.chat_id
                    if chat_id not in self.chat_ids:
                        new_ids.append(chat_id)
                        self.chat_ids.append(chat_id)
                        self.logger.info(f"New chat ID registered: {chat_id}")

                # Update the offset to the latest processed update
                if update.update_id >= offset:
                    offset = update.update_id
                    self._save_last_update_id(offset)

            if new_ids:
                self._save_chat_ids()
                self.logger.info(f"Saved {len(new_ids)} new chat IDs")
        except Exception as e:
            self.logger.error(f"Chat ID update failed: {e}")

    async def _verify_chat_id(self, chat_id: int) -> bool:
        """Verify if a chat ID is still valid"""
        try:
            await self.bot.send_chat_action(chat_id=chat_id, action="typing")
            return True
        except telegram.error.Unauthorized:
            return False
        except Exception:
            # For other errors, assume the chat is still valid
            return True

    async def cleanup_invalid_chats(self):
        """Remove invalid chat IDs from storage"""
        invalid_ids = []
        for chat_id in self.chat_ids:
            if not await self._verify_chat_id(chat_id):
                invalid_ids.append(chat_id)
                self.logger.info(f"Removing invalid chat ID: {chat_id}")

        if invalid_ids:
            self.chat_ids = [
                id for id in self.chat_ids if id not in invalid_ids]
            self._save_chat_ids()

    async def send_alert(self, image_path: Path, caption: str) -> bool:
        """Send alert to all registered chats with retry logic and invalid chat cleanup"""
        if not image_path.exists():
            self.logger.error(f"Alert image missing: {image_path}")
            return False

        overall_success = False
        failed_chats = []

        # Read image data once
        with open(image_path, 'rb') as f:
            image_data = f.read()

        try:
            for chat_id in self.chat_ids:
                sent = False
                for attempt in range(3):
                    try:
                        # Create new BytesIO for each send attempt
                        photo = BytesIO(image_data)
                        photo.name = 'image.jpg'  # Telegram requires a name

                        async with self.bot:  # Create new session for each chat
                            await self.bot.send_photo(
                                chat_id=chat_id,
                                photo=photo,
                                caption=caption,
                                parse_mode='Markdown',
                                pool_timeout=20
                            )
                        self.logger.info(
                            f"Alert sent to Telegram chat {chat_id}")
                        sent = True
                        overall_success = True
                        break
                    except telegram.error.Unauthorized:
                        self.logger.warning(f"Unauthorized for chat {chat_id}")
                        failed_chats.append(chat_id)
                        break
                    except telegram.error.TimedOut:
                        await asyncio.sleep(2 ** attempt)
                        self.logger.warning(
                            f"Timeout sending to {chat_id}, retry {attempt+1}/3")
                    except telegram.error.NetworkError:
                        await asyncio.sleep(5)
                        self.logger.warning(
                            f"Network error with {chat_id}, retry {attempt+1}/3")
                    except Exception as e:
                        self.logger.error(
                            f"Failed to send to {chat_id}: {str(e)}")
                        if attempt == 2:  # Only add to failed chats after all retries
                            failed_chats.append(chat_id)
                        break

                if not sent:
                    failed_chats.append(chat_id)

            # Clean up invalid chats after sending alerts
            if failed_chats:
                self.chat_ids = [
                    id for id in self.chat_ids if id not in failed_chats]
                self._save_chat_ids()
                self.logger.info(
                    f"Removed {len(failed_chats)} invalid chat IDs")

        except Exception as e:
            self.logger.error(f"Telegram error: {str(e)}")

        return overall_success

    async def send_test_alert(self, test_image: Path):
        """Special method for test alerts"""
        return await self.send_alert(test_image, "🔧 System Test: Service Operational")
