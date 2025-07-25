import asyncio
import httpx
import json
import logging
import time
from datetime import datetime, timezone
import schedule
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- IMPORTANT: FILL IN YOUR ACTUAL VALUES HERE ---
# DO NOT COMMIT THESE VALUES TO GITHUB PUBLICLY!
# For production, use environment variables or GitHub Secrets.
TELEGRAM_BOT_TOKEN = "8421114487:AAH6PoGjlqrjUNmKVjfbBfYZEIElGDPG0Ug"
TELEGRAM_CHANNEL_ID = -1001234567890 # Replace with your channel ID (e.g., -1001234567890)
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com" # Or your preferred RPC provider (e.g., QuickNode, Helius)
BIRDEYE_API_KEY = "YOUR_BIRDEYE_API_KEY_HERE" # Get one from https://birdeye.so/api-access
# --- END IMPORTANT SECTION ---

# Deduplication cache expiration in seconds (1 hour)
DEDUPLICATION_TIME_SECONDS = 3600

# Cache for deduplication of token alerts (in-memory, lost on restart)
ALERTED_TOKENS_CACHE = {}

# Set to store mint addresses seen so far (in-memory, lost on restart)
# On first run, this will be populated with existing mints without alerting.
KNOWN_MINTS = set()

# Basic Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Telegram Bot Functions ---
async def send_token_alert(application, token_info):
    """Sends a formatted alert to the Telegram channel."""
    message = format_token_message(token_info)
    try:
        await application.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=message, parse_mode='HTML', disable_web_page_preview=True)
        logger.info(f"Alert sent for token: {token_info.get('name', 'N/A')} ({token_info.get('symbol', 'N/A')})")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

def format_token_message(token_info):
    """Formats the token information into an HTML message."""
    name = token_info.get('name', 'N/A')
    symbol = token_info.get('symbol', 'N/A')
    mint_address = token_info.get('mint_address', 'N/A')
    launch_timestamp = token_info.get('launch_timestamp', 'N/A')

    # Links
    solscan_link = f"https://solscan.io/token/{mint_address}"
    birdeye_link = f"https://birdeye.so/token/{mint_address}?chain=solana"

    message = (
        f"✨ <b>New Solana Token Listing!</b> ✨\n\n"
        f"<b>Name:</b> {name} ({symbol})\n"
        f"<b>Mint Address:</b> <code>{mint_address}</code>\n"
        f"<b>Launch Time:</b> {launch_timestamp}\n\n"
        f"🔗 <b>Links:</b>\n"
        f"  <a href='{solscan_link}'>Solscan</a> | <a href='{birdeye_link}'>Birdeye</a>\n"
    )

    socials = token_info.get('socials', {})
    if socials:
        message += "\n<b>🌐 Socials:</b>\n"
        for platform, url in socials.items():
            message += f"  <a href='{url}'>{platform.capitalize()}</a>\n"

    # Add description if available
    description = token_info.get('description')
    if description:
        # Truncate long descriptions
        if len(description) > 200:
            description = description[:197] + "..."
        message += f"\n<b>📝 Description:</b>\n{description}\n"

    return message

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! I'm a bot that tracks new Solana token listings. "
        f"I'll send alerts to the configured channel when a quality token is launched."
    )

# --- Solana Tracking and Filtering Logic ---
class SolanaTokenTracker:
    def __init__(self, rpc_url, birdeye_api_key, telegram_application):
        self.rpc_url = rpc_url
        self.birdeye_api_key = birdeye_api_key
        self.http_client = httpx.AsyncClient(timeout=30)
        self.telegram_application = telegram_application # Pass the already built application

    async def get_all_mint_accounts(self):
        """
        Fetches all accounts owned by the SPL Token Program that are the size of a mint account.
        This is a more direct way to find potential new tokens.
        """
        # SPL Token Program ID
        spl_token_program_id = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        # Standard size of a mint account (82 bytes)
        mint_account_data_size = 82

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getProgramAccounts",
            "params": [
                spl_token_program_id,
                {
                    "encoding": "base64", # We only need the pubkey, not the parsed data here
                    "filters": [
                        {"dataSize": mint_account_data_size}
                    ],
                    "commitment": "confirmed"
                }
            ]
        }
        try:
            response = await self.http_client.post(self.rpc_url, json=payload)
            response.raise_for_status()
            result = response.json().get("result", [])
            # Extract only the public keys (mint addresses)
            mint_addresses = {item["pubkey"] for item in result}
            return mint_addresses
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error fetching program accounts: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Request error fetching program accounts: {e}")
        except KeyError:
            logger.error(f"Invalid response when fetching program accounts: {response.json()}")
        return set()

    async def get_token_supply(self, mint_address):
        """Fetches the total supply of a given mint address."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenSupply",
            "params": [
                mint_address
            ]
        }
        try:
            response = await self.http_client.post(self.rpc_url, json=payload)
            response.raise_for_status()
            result = response.json().get("result")
            if result and 'value' in result:
                return int(result['value']['amount'])
            return 0
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error fetching token supply for {mint_address}: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            logger.warning(f"Request error fetching token supply for {mint_address}: {e}")
        return 0

    async def get_birdeye_token_info(self, mint_address):
        """Fetches token metadata from Birdeye API."""
        if not self.birdeye_api_key or self.birdeye_api_key == "YOUR_BIRDEYE_API_KEY_HERE":
            logger.warning("Birdeye API key not set or is placeholder. Skipping Birdeye info.")
            return {}

        headers = {"X-API-KEY": self.birdeye_api_key}
        url = f"https://public-api.birdeye.so/public/token/solana?address={mint_address}"
        try:
            response = await self.http_client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json().get("data", {})
            return data
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"Birdeye token info not found for {mint_address}")
            else:
                logger.error(f"Birdeye API error for {mint_address}: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Request error with Birdeye API for {mint_address}: {e}")
        return {}

    def _is_token_alerted_recently(self, mint_address):
        """Checks if a token has been alerted recently."""
        last_alert_time = ALERTED_TOKENS_CACHE.get(mint_address)
        if last_alert_time:
            time_since_alert = (datetime.now() - last_alert_time).total_seconds()
            if time_since_alert < DEDUPLICATION_TIME_SECONDS:
                logger.info(f"Token {mint_address} was alerted {time_since_alert:.0f} seconds ago. Skipping.")
                return True
        return False

    def _add_to_alert_cache(self, mint_address):
        """Adds a token to the alerted cache with current timestamp."""
        ALERTED_TOKENS_CACHE[mint_address] = datetime.now()
        logger.debug(f"Added {mint_address} to alert cache.")

    def _clean_alert_cache(self):
        """Removes old entries from the alert cache."""
        current_time = datetime.now()
        keys_to_remove = [
            mint_address for mint_address, timestamp in ALERTED_TOKENS_CACHE.items()
            if (current_time - timestamp).total_seconds() > DEDUPLICATION_TIME_SECONDS
        ]
        for key in keys_to_remove:
            del ALERTED_TOKENS_CACHE[key]
            logger.debug(f"Removed {key} from alert cache (expired).")
        if keys_to_remove:
            logger.info(f"Cleaned {len(keys_to_remove)} old entries from alert cache.")

    def is_quality_token(self, token_info):
        """Applies quality filtering criteria."""
        # 1. On-chain metadata (name, symbol, supply)
        # Birdeye API generally provides these, or we infer from RPC for supply.
        # We consider it "on-chain" if Birdeye has it, as they derive from on-chain.
        has_metadata = bool(token_info.get('name') and token_info.get('symbol') and token_info.get('supply') > 0)
        if not has_metadata:
            logger.debug(f"Failed metadata check: {token_info.get('mint_address')}")
            return False

        # 2. A profile image/icon and basic description
        has_image_and_description = bool(token_info.get('logo_uri') and token_info.get('description'))
        if not has_image_and_description:
            logger.debug(f"Failed image/description check: {token_info.get('mint_address')}")
            return False

        # 3. At least one active social link (Twitter/X, Discord, or website)
        has_social_link = bool(token_info.get('socials'))
        if not has_social_link:
            logger.debug(f"Failed social link check: {token_info.get('mint_address')}")
            return False

        return True

    async def track_new_tokens(self):
        """Main function to track new tokens."""
        logger.info("Starting new token tracking cycle...")
        current_mints = await self.get_all_mint_accounts()

        if not current_mints:
            logger.warning("Could not fetch any mint accounts. RPC might be down or rate-limited.")
            return

        # On the first run, populate KNOWN_MINTS without alerting
        if not KNOWN_MINTS:
            KNOWN_MINTS.update(current_mints)
            logger.info(f"Initial population of {len(KNOWN_MINTS)} known mints. No alerts on first run.")
            return

        new_mints = current_mints - KNOWN_MINTS
        if not new_mints:
            logger.info("No new mints found in this cycle.")
            return

        logger.info(f"Found {len(new_mints)} potential new mints. Processing...")

        for mint_address in new_mints:
            if self._is_token_alerted_recently(mint_address):
                continue # Already alerted recently, skip processing

            # Fetch Birdeye info and supply for the new mint
            birdeye_info = await self.get_birdeye_token_info(mint_address)
            supply = await self.get_token_supply(mint_address)

            # Birdeye doesn't always provide launch timestamp directly from get_token_info
            # For simplicity, we'll use current time or 'N/A' if not found easily.
            # A more robust solution would involve finding the initializeMint transaction.
            # For this optimized version, relying on Birdeye's existing data or a generic timestamp.
            launch_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC (approx)')
            if birdeye_info.get('createdAt'): # Birdeye might have a creation timestamp
                 try:
                     # Birdeye's createdAt is often a Unix timestamp in milliseconds
                     launch_timestamp = datetime.fromtimestamp(birdeye_info['createdAt'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                 except (TypeError, ValueError):
                     pass # Fallback to approximation

            token_info = {
                "mint_address": mint_address,
                "name": birdeye_info.get("name", "Unknown Token"),
                "symbol": birdeye_info.get("symbol", "UNKNOWN"),
                "supply": supply,
                "decimals": birdeye_info.get("decimals", 0),
                "launch_timestamp": launch_timestamp,
                "logo_uri": birdeye_info.get("logo", None),
                "description": birdeye_info.get("description", None),
                "socials": {}
            }

            if birdeye_info.get("twitter"):
                token_info["socials"]["twitter"] = birdeye_info["twitter"]
            if birdeye_info.get("discord"):
                token_info["socials"]["discord"] = birdeye_info["discord"]
            if birdeye_info.get("website"):
                token_info["socials"]["website"] = birdeye_info["website"]

            if self.is_quality_token(token_info):
                logger.info(f"Quality token found: {token_info['name']} ({token_info['symbol']}) - {token_info['mint_address']}")
                await send_token_alert(self.telegram_application, token_info)
                self._add_to_alert_cache(mint_address)
            else:
                logger.info(f"Token {token_info['name']} ({token_info['symbol']}) - {token_info['mint_address']} did not meet quality criteria.")

        # Update KNOWN_MINTS for the next cycle
        KNOWN_MINTS.update(new_mints)
        self._clean_alert_cache()

# --- Main Execution ---
async def main():
    # Check if placeholder values are still present
    if (TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE" or
        TELEGRAM_CHANNEL_ID == -1001234567890 or
        BIRDEYE_API_KEY == "YOUR_BIRDEYE_API_KEY_HERE"):
        logger.critical("ERROR: Placeholder API keys or IDs found! Please replace them in main.py before running.")
        return

    # Initialize Telegram Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))

    # Start Telegram bot polling in a separate task
    # This keeps the bot responsive to /start commands while tracking runs
    asyncio.create_task(application.run_polling(poll_interval=1, timeout=30, drop_pending_updates=True, close_on_stop=True))
    logger.info("Telegram bot polling started.")

    tracker = SolanaTokenTracker(SOLANA_RPC_URL, BIRDEYE_API_KEY, application)

    # Schedule the tracking job
    # The first run of track_new_tokens will just populate KNOWN_MINTS
    schedule.every(30).seconds.do(lambda: asyncio.create_task(tracker.track_new_tokens()))
    logger.info("Scheduler started. Checking for new tokens every 30 seconds.")

    while True:
        try:
            schedule.run_pending()
            await asyncio.sleep(1) # Keep the event loop running
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            await asyncio.sleep(5) # Wait before retrying

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
    except Exception as e:
        logger.critical(f"Unhandled exception in main application: {e}", exc_info=True)
