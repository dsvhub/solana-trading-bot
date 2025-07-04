import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import requests
import logging
from datetime import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import telegram
import os
import sys
import pygame
import pkg_resources
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.hash import Hash
from solders.message import Message
from solders.instruction import Instruction
from solana.rpc.types import TxOpts
import solana.exceptions
import base64
import traceback

# --- Configuration ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '7053035774:AAGMkr9_BFNJJtfBLzPdnTmxXu3xnzdr53o')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '6265564865')
WALLET_PRIVATE_KEY = os.getenv('WALLET_PRIVATE_KEY', '3HoiUVMBP3NfAgkZo1VAxsNmDwL5FEJUTj1wWrnYfx2t7orndtBfA7srzieEFAmJqkSVhGyfN8EdbF7eh2McMjDa')

RPC_ENDPOINT = os.getenv('RPC_ENDPOINT', 'https://hardworking-red-firefly.solana-mainnet.quiknode.pro/26a4ef1171209e5c637a5cc70ab7f79dff974beb/')

# --- Logging Setup ---
log_file = os.path.join(os.path.dirname(__file__), "jupbot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# --- Dependency and Sound File Check ---
required_packages = ['solana', 'requests', 'telegram', 'matplotlib', 'pygame']
missing_packages = []
for pkg in required_packages:
    try:
        __import__(pkg)
    except ImportError:
        missing_packages.append(pkg)
if missing_packages:
    logger.error(f"Missing required packages: {', '.join(missing_packages)}. Install with: pip install {' '.join(missing_packages)}")
    sys.exit(1)

# Check solana-py version
try:
    solana_version = pkg_resources.get_distribution("solana").version
    required_version = "0.31.0"
    if pkg_resources.parse_version(solana_version) < pkg_resources.parse_version(required_version):
        logger.warning(f"Detected old solana-py version ({solana_version}). Upgrade to >={required_version} for reliable operation: pip install --upgrade solana")
    else:
        logger.info(f"solana-py version {solana_version} is up to date.")
except pkg_resources.DistributionNotFound:
    logger.error("solana-py not installed. Install with: pip install solana")
    sys.exit(1)

sound_files = ['start_bot.mp3', 'stop_bot.mp3', 'reset_bot.mp3', 'buy_alert.wav', 'stop_loss_alert.wav', 'take_profit_alert.wav']
missing_sounds = [f for f in sound_files if not os.path.exists(os.path.join(os.path.dirname(__file__), f))]
if missing_sounds:
    logger.warning(f"Missing sound files: {', '.join(missing_sounds)}. Sound alerts may fail.")

# --- Configuration Validation ---
if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WALLET_PRIVATE_KEY, RPC_ENDPOINT]):
    logger.error("Missing configuration: Ensure TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WALLET_PRIVATE_KEY, and RPC_ENDPOINT are set.")
    sys.exit(1)
if "YOUR_API_KEY" in RPC_ENDPOINT:
    logger.error("Invalid RPC_ENDPOINT: Replace 'YOUR_API_KEY' with a valid Helio key.")
    sys.exit(1)

# --- Globals ---
is_running = False
position_open = False
entry_price = 0
buy_price = 0
stop_loss_price = 0
take_profit_price = 0
swap_in_progress = False

# Initialize pygame mixer
try:
    pygame.mixer.init()
except Exception as e:
    logger.warning(f"Pygame mixer initialization failed: {e}. Sound alerts disabled.")

# Telegram Bot
try:
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
except Exception as e:
    logger.error(f"Telegram Bot initialization failed: {e}")
    bot = None

# Wallet setup
solana_client = Client(RPC_ENDPOINT)
try:
    wallet = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
except Exception as e:
    logger.error(f"Wallet initialization failed: {e}")
    sys.exit(1)

def validate_rpc_endpoint():
    try:
        test_client = Client(RPC_ENDPOINT)
        response = test_client.get_epoch_info()
        if hasattr(response, 'value') and hasattr(response.value, 'epoch'):
            logger.info(f"RPC endpoint validated successfully: {RPC_ENDPOINT}")
            return True
        logger.error(f"Invalid RPC response during validation: {response}")
        return False
    except Exception as e:
        logger.error(f"RPC endpoint validation failed: {str(e)}\nTraceback: {traceback.format_exc()}")
        return False

def send_telegram(message):
    if bot is None:
        return
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"[{timestamp}] {message}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def fetch_wallet_balance(max_attempts=3, backoff_factor=2):
    for attempt in range(max_attempts):
        try:
            response = solana_client.get_balance(wallet.pubkey())
            if not hasattr(response, 'value'):
                logger.error(f"Invalid balance response type: {type(response)}, content: {response}")
                return None
            balance = response.value / 1e9
            return balance
        except solana.exceptions.SolanaRpcException as rpc_err:
            try:
                inner_exc = rpc_err.__cause__
                error_details = f"{str(inner_exc)}\nResponse: {getattr(inner_exc, 'response', 'N/A')}"
            except AttributeError:
                error_details = str(rpc_err)
            logger.error(f"Balance fetch attempt {attempt + 1}/{max_attempts} failed: {error_details}\nTraceback: {traceback.format_exc()}")
            if attempt == max_attempts - 1:
                logger.error(f"Failed to fetch balance after {max_attempts} attempts: {error_details}")
                return None
            time.sleep(backoff_factor ** attempt)
        except Exception as e:
            logger.error(f"Balance fetch attempt {attempt + 1}/{max_attempts} failed: {str(e)}\nTraceback: {traceback.format_exc()}")
            if attempt == max_attempts - 1:
                logger.error(f"Failed to fetch balance after {max_attempts} attempts: {str(e)}")
                return None
            time.sleep(backoff_factor ** attempt)
    return None

def fetch_current_price(max_attempts=3, backoff_factor=2):
    for attempt in range(max_attempts):
        try:
            url = 'https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd'
            headers = {'accept': 'application/json'}
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            price = data['solana']['usd']
            logger.debug(f"Price fetch successful: ${price}")
            return price
        except requests.exceptions.HTTPError as http_err:
            if http_err.response.status_code == 429:
                logger.error(f"Price fetch attempt {attempt + 1}/{max_attempts} failed: 429 Too Many Requests")
                if attempt == max_attempts - 1:
                    logger.error("Max attempts reached for price fetch due to rate limit.")
                    return None
                sleep_time = backoff_factor ** attempt
                logger.info(f"Backing off for {sleep_time} seconds due to rate limit.")
                time.sleep(sleep_time)
            else:
                logger.error(f"Price fetch attempt {attempt + 1}/{max_attempts} failed: {http_err}\nTraceback: {traceback.format_exc()}")
                return None
        except Exception as e:
            logger.error(f"Price fetch attempt {attempt + 1}/{max_attempts} failed: {e}\nTraceback: {traceback.format_exc()}")
            if attempt == max_attempts - 1:
                logger.error(f"Failed to fetch price after {max_attempts} attempts: {e}")
                return None
            time.sleep(backoff_factor ** attempt)
    return None

def log(message):
    logger.info(message)
    def append_log():
        try:
            log_output.insert(tk.END, f"{datetime.now().strftime('%H:%M:%S')} - {message}\n")
            log_output.yview_moveto(1.0)
        except tk.TclError:
            pass
    root.after(0, append_log)

def play_sound(file_name):
    if file_name in missing_sounds:
        return
    try:
        sound_path = os.path.join(os.path.dirname(__file__), file_name)
        pygame.mixer.music.load(sound_path)
        pygame.mixer.music.play()
    except Exception as e:
        logger.error(f"Sound playback error: {e}")

def start_bot():
    global is_running
    if not is_running:
        try:
            entry_price = float(entry_price_input.get())
            sl_percent = float(stop_loss_input.get())
            tp_percent = float(take_profit_input.get())
            trade_amount = float(trade_amount_input.get())
            if entry_price <= 0 or sl_percent <= 0 or tp_percent <= 0 or trade_amount <= 0:
                log("All inputs must be positive numbers.")
                messagebox.showerror("Invalid Input", "All inputs must be positive numbers.")
                return
            if trade_amount < 0.01:
                log("Trade amount must be at least 0.01 SOL.")
                messagebox.showerror("Invalid Input", "Trade amount must be at least 0.01 SOL.")
                return
        except ValueError:
            log("Please enter valid numerical inputs for all fields.")
            messagebox.showerror("Invalid Input", "Please enter valid numerical inputs.")
            return
        if not validate_rpc_endpoint():
            log("Bot startup aborted due to invalid RPC endpoint.")
            send_telegram("[ERROR] Bot startup aborted: Invalid RPC endpoint.")
            return
        is_running = True
        sol_balance = fetch_wallet_balance()
        sol_address = wallet.pubkey()
        log("Bot started.")
        log(f"Wallet: {sol_address}")
        log(f"Balance: {sol_balance:.4f} SOL" if sol_balance is not None else "Balance: Error")
        send_telegram(f"🚀 Bot started\nWallet: {sol_address}\nBalance: {sol_balance:.4f} SOL" if sol_balance is not None else "Balance: Error")
        play_sound("start_bot.mp3")
        threading.Thread(target=bot_loop, daemon=True).start()

def stop_bot():
    global is_running
    if is_running:
        if messagebox.askyesno("Confirm Stop", "Are you sure you want to stop the bot? A trade may be in progress."):
            is_running = False
            log("Bot stopped by user.")
            send_telegram("🛑 Bot stopped by user.")
            play_sound("stop_bot.mp3")
        else:
            log("Stop bot canceled.")

def reset_trade():
    global position_open, buy_price, stop_loss_price, take_profit_price
    position_open = False
    buy_price = 0
    stop_loss_price = 0
    take_profit_price = 0
    log("Trade reset.")
    send_telegram("🔄 Trade reset.")
    play_sound("reset_bot.mp3")

def update_price_chart(current_price):
    prices.append(current_price)
    timestamps.append(datetime.now())
    if len(prices) > 100:
        prices.pop(0)
        timestamps.pop(0)
    def update():
        try:
            ax.clear()
            ax.plot(timestamps, prices, label='SOL Price', color='#4CAF50')
            ax.set_xlabel('Time')
            ax.set_ylabel('Price (USD)')
            ax.legend()
            ax.tick_params(axis='x', rotation=45)
            ax.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter('%H:%M:%S'))
            canvas.draw()
        except tk.TclError:
            pass
    root.after(0, update)

def update_wallet_display():
    sol_balance = fetch_wallet_balance()
    def set_balance():
        try:
            if sol_balance is not None:
                wallet_balance.set(f"SOL Balance: {sol_balance:.4f}")
            else:
                wallet_balance.set("SOL Balance: Error")
        except tk.TclError:
            pass
    root.after(0, set_balance)

def get_jupiter_quote(amount_lamports, max_attempts=3, backoff_factor=2):
    for attempt in range(max_attempts):
        try:
            url = 'https://quote-api.jup.ag/v6/quote'
            params = {
                'inputMint': 'So11111111111111111111111111111111111111112',
                'outputMint': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
                'amount': str(amount_lamports),
                'slippageBps': '50',
                'asLegacyRoute': 'true',
                'onlyDirectRoutes': 'false'
            }
            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            if 'outAmount' in data and 'routePlan' in data:
                log(f"Quote received: {data}")
                return data
            log(f"Invalid quote response: {data}")
            return None
        except requests.exceptions.HTTPError as http_err:
            log(f"Quote API HTTP error: {http_err}. Response: {response.text if 'response' in locals() else 'N/A'}")
            if attempt == max_attempts - 1:
                log(f"Failed to get quote after {max_attempts} attempts: {http_err}")
                return None
            time.sleep(backoff_factor ** attempt)
        except requests.exceptions.RequestException as e:
            log(f"Quote fetch attempt {attempt + 1}/{max_attempts} failed: {e}")
            if attempt == max_attempts - 1:
                log(f"Failed to get quote after {max_attempts} attempts: {e}")
                return None
            time.sleep(backoff_factor ** attempt)
    return None

def get_jupiter_swap_transaction(quote_response_obj, user_public_key):
    try:
        url = 'https://quote-api.jup.ag/v6/swap'
        payload = {
            'quoteResponse': quote_response_obj,
            'userPublicKey': str(user_public_key),
            'wrapAndUnwrapSol': True,
            'asLegacyTransaction': True
        }
        log(f"Sending payload to /v6/swap: {payload}")
        response = requests.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        if 'swapTransaction' in data:
            return data
        log(f"Invalid swap transaction response: {data}")
        return {}
    except requests.exceptions.HTTPError as http_err:
        log(f"Swap API HTTP error: {http_err}. Response: {response.text if 'response' in locals() else 'N/A'}")
        return {}
    except Exception as e:
        log(f"Unexpected error in get_jupiter_swap_transaction: {e}\nTraceback: {traceback.format_exc()}")
        return {}

def get_latest_blockhash_with_retry(max_attempts=3, backoff_factor=2):
    for attempt in range(max_attempts):
        try:
            blockhash_resp = solana_client.get_latest_blockhash()
            logger.debug(f"Blockhash response: {blockhash_resp}")
            if hasattr(blockhash_resp, 'value') and hasattr(blockhash_resp.value, 'blockhash'):
                blockhash = blockhash_resp.value.blockhash
                log(f"Successfully fetched blockhash: {blockhash}")
                return blockhash
            log(f"Invalid blockhash response: {blockhash_resp}")
            return None
        except requests.exceptions.HTTPError as http_err:
            status_code = http_err.response.status_code if http_err.response else 'Unknown'
            log(f"Blockhash fetch attempt {attempt + 1}/{max_attempts} failed: HTTPError: {status_code} - {str(http_err)}\nTraceback: {traceback.format_exc()}")
            if status_code == 429:
                log("Rate limit hit. Consider upgrading QuickNode plan or switching providers.")
                return None
            if attempt == max_attempts - 1:
                log(f"Failed to fetch blockhash after {max_attempts} attempts: {str(http_err)}")
                return None
            time.sleep(backoff_factor ** attempt)
        except Exception as e:
            log(f"Blockhash fetch attempt {attempt + 1}/{max_attempts} failed: {str(e)}\nTraceback: {traceback.format_exc()}")
            if attempt == max_attempts - 1:
                log(f"Failed to fetch blockhash after {max_attempts} attempts: {str(e)}")
                return None
            time.sleep(backoff_factor ** attempt)
    return None

def execute_swap():
    global position_open, swap_in_progress
    if swap_in_progress:
        log("Swap already in progress. Skipping new swap attempt.")
        return
    swap_in_progress = True
    try:
        sol_amount_str = trade_amount_input.get()
        if not sol_amount_str:
            log("Error: Trade amount is empty. Please enter a SOL amount.")
            return
        try:
            sol_amount = float(sol_amount_str)
            min_trade_amount = 0.01
            if sol_amount < min_trade_amount:
                log(f"Error: Trade amount too low: {sol_amount} SOL. Minimum is {min_trade_amount} SOL.")
                send_telegram(f"[ERROR] Trade amount too low: {sol_amount} SOL")
                return
            if sol_amount <= 0:
                log("Error: Trade amount must be positive.")
                return
            balance = fetch_wallet_balance()
            fee_buffer = 0.0005
            if balance is not None and balance < sol_amount + fee_buffer:
                log(f"Error: Insufficient balance: {balance:.4f} SOL available, {sol_amount + fee_buffer:.4f} SOL required.")
                send_telegram(f"[ERROR] Insufficient balance: {balance:.4f} SOL")
                return
            if balance is None:
                log(f"Warning: Balance fetch failed, proceeding with trade assuming sufficient funds for {sol_amount} SOL.")
            amount_lamports = int(sol_amount * 1e9)
        except ValueError:
            log(f"Error: Invalid trade amount: '{sol_amount_str}'. Please enter a valid number for SOL.")
            return

        log(f"Attempting to get quote for {sol_amount} SOL ({amount_lamports} lamports)...")
        legacy_route_object = get_jupiter_quote(amount_lamports)
        if not legacy_route_object:
            log("Error: Failed to get a valid quote. Swap aborted.")
            send_telegram("[ERROR] Failed to get quote.")
            return

        in_amount_lamport_str = legacy_route_object.get('inAmount', 'N/A')
        out_amount_tokens_str = legacy_route_object.get('outAmount', 'N/A')
        log(f"Quote: {in_amount_lamport_str} lamports SOL -> {out_amount_tokens_str} USDC")

        log("Attempting to get swap transaction...")
        swap_tx_payload = get_jupiter_swap_transaction(legacy_route_object, wallet.pubkey())
        if not swap_tx_payload or 'swapTransaction' not in swap_tx_payload:
            log(f"Error: Failed to get swap transaction: {swap_tx_payload}")
            send_telegram(f"[ERROR] Failed to get swap transaction: {swap_tx_payload}")
            return

        swap_transaction_encoded = swap_tx_payload['swapTransaction']
        tx_bytes = base64.b64decode(swap_transaction_encoded)
        transaction = Transaction.from_bytes(tx_bytes)
        log("Transaction deserialized successfully.")

        log("Attempting to fetch latest blockhash...")
        recent_blockhash = get_latest_blockhash_with_retry()
        if not recent_blockhash:
            log("Error: Failed to fetch a valid blockhash. Swap aborted.")
            send_telegram("[ERROR] Failed to fetch blockhash.")
            return
        log(f"Latest blockhash: {recent_blockhash}")

        # Convert CompiledInstruction objects to Instruction objects
        instructions = []
        account_keys = transaction.message.account_keys
        for compiled_inst in transaction.message.instructions:
            program_id = account_keys[compiled_inst.program_id_index]
            accounts = [account_keys[idx] for idx in compiled_inst.accounts]
            data = bytes(compiled_inst.data)
            instruction = Instruction(program_id, data, accounts)
            instructions.append(instruction)

        # Create a new message with the updated blockhash
        new_message = Message.new_with_blockhash(
            instructions=instructions,
            payer=wallet.pubkey(),
            blockhash=recent_blockhash
        )
        log("Transaction message updated successfully with new blockhash.")

        # Create a new transaction with the updated message
        new_transaction = Transaction.populate(new_message, transaction.signatures)
        log("Transaction populated successfully with new message.")

        if new_transaction.fee_payer is None:
            new_transaction.fee_payer = wallet.pubkey()
            log("Set fee_payer to wallet public key.")

        log("Attempting to sign transaction...")
        new_transaction.sign([wallet])
        log("Transaction signed successfully.")

        log("Attempting to send transaction...")
        opts = TxOpts(skip_preflight=False, preflight_commitment="confirmed")
        send_tx_response = solana_client.send_transaction(new_transaction, opts=opts)
        log(f"Send transaction response: {send_tx_response}")
        txid = send_tx_response.get('result') if isinstance(send_tx_response, dict) else None
        if txid:
            log(f"Swap transaction completed successfully! TXID: {txid}")
            log(f"View on Solscan: https://solscan.io/tx/{txid}")
            send_telegram(f"✅ Swap completed!\nInput: {sol_amount:.4f} SOL\nOutput: {int(out_amount_tokens_str)/1e6:.2f} USDC\nTXID: {txid}\nhttps://solscan.io/tx/{txid}")
            for attempt in range(30):
                try:
                    status = solana_client.get_transaction(txid, commitment="confirmed")
                    if status.get('result') and status['result']['meta']['err'] is None:
                        log("Transaction confirmed successfully!")
                        send_telegram("✅ Transaction confirmed!")
                        break
                    elif status.get('result') and status['result']['meta']['err']:
                        log(f"Error: Transaction failed: {status['result']['meta']['err']}")
                        send_telegram(f"[ERROR] Transaction failed: {status['result']['meta']['err']}")
                        position_open = False
                        break
                except Exception as e:
                    log(f"Error checking transaction status: {e}")
                time.sleep(2)
        else:
            log(f"Error: Swap failed: {send_tx_response}")
            send_telegram(f"[ERROR] Swap failed: {send_tx_response.get('error', 'No error details available')}")
            position_open = False

    except Exception as e:
        log(f"Error: Swap execution failed: {e}\nTraceback: {traceback.format_exc()}")
        send_telegram(f"[ERROR] Swap execution error: {e}")
        # Keep position_open True to prevent repeated buys
    finally:
        swap_in_progress = False

def bot_loop():
    global is_running, position_open, entry_price, buy_price, stop_loss_price, take_profit_price
    while is_running:
        try:
            current_price = fetch_current_price()
            if current_price is None:
                log("Warning: Skipping loop iteration due to price fetch failure.")
                time.sleep(10)
                continue

            update_price_chart(current_price)
            update_wallet_display()
            log(f"Current Price: ${current_price:.2f}")
            log(f"Position open: {position_open}")

            try:
                target_entry = float(entry_price_input.get())
                sl_percent = float(stop_loss_input.get())
                tp_percent = float(take_profit_input.get())
                trade_amount = float(trade_amount_input.get())
                if target_entry <= 0 or sl_percent <= 0 or tp_percent <= 0 or trade_amount <= 0:
                    log("Error: Invalid input: All values must be positive. Pausing trade checks.")
                    time.sleep(10)
                    continue
                if trade_amount < 0.01:
                    log("Error: Invalid input: Trade amount must be at least 0.01 SOL. Pausing trade checks.")
                    time.sleep(10)
                    continue
                lower_bound = target_entry - 0.20
                upper_bound = target_entry + 0.20
                log(f"Entry price: {target_entry:.2f}, Range: ${lower_bound:.2f}–${upper_bound:.2f}, Current: ${current_price:.2f}")
            except ValueError:
                log("Error: Invalid numerical input. Pausing trade checks.")
                time.sleep(10)
                continue

            if not position_open and lower_bound <= current_price <= upper_bound and not swap_in_progress:
                position_open = True
                buy_price = current_price
                stop_loss_price = buy_price * (1 - sl_percent / 100)
                take_profit_price = buy_price * (1 + tp_percent / 100)
                log(f"BUY at ${buy_price:.2f} | SL: ${stop_loss_price:.2f}, TP: ${take_profit_price:.2f}")
                send_telegram(f"\U0001F7E2 BUY at ${buy_price:.2f}")
                play_sound("buy_alert.wav")
                threading.Thread(target=execute_swap, daemon=True).start()
                continue

            if position_open:
                if current_price <= stop_loss_price:
                    position_open = False
                    log(f"STOP-LOSS triggered at ${current_price:.2f}")
                    send_telegram(f"\U0001F53B STOP-LOSS at ${current_price:.2f}")
                    play_sound("stop_loss_alert.wav")
                elif current_price >= take_profit_price:
                    position_open = False
                    log(f"TAKE-PROFIT triggered at ${current_price:.2f}")
                    send_telegram(f"\U0001F4B0 TAKE-PROFIT at ${current_price:.2f}")
                    play_sound("take_profit_alert.wav")

            time.sleep(10)
        except Exception as e:
            log(f"Error: Bot loop failed: {e}\nTraceback: {traceback.format_exc()}")
            send_telegram(f"[ERROR] Bot loop error: {e}")
            time.sleep(10)

def on_closing():
    global is_running
    is_running = False
    log("Bot stopped.")
    log("Exiting application...")
    send_telegram("🛑 Application closed.")
    try:
        root.quit()
        root.destroy()
    except Exception as e:
        logger.error(f"Error during window closing: {e}")
    sys.exit(0)

# GUI Setup
root = tk.Tk()
root.title("Solana Entry-Price Trading Bot")
root.protocol("WM_DELETE_WINDOW", on_closing)

main_frame = ttk.Frame(root, padding="10")
main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

entry_price_input = ttk.Entry(main_frame)
entry_price_input.grid(row=0, column=1)
entry_price_input.insert(0, "171.14")
ttk.Label(main_frame, text="Entry Price (USD)").grid(row=0, column=0)

stop_loss_input = ttk.Entry(main_frame)
stop_loss_input.grid(row=1, column=1)
stop_loss_input.insert(0, "2")
ttk.Label(main_frame, text="Stop Loss (%)").grid(row=1, column=0)

take_profit_input = ttk.Entry(main_frame)
take_profit_input.grid(row=2, column=1)
take_profit_input.insert(0, "11")
ttk.Label(main_frame, text="Take Profit (%)").grid(row=2, column=0)

trade_amount_input = ttk.Entry(main_frame)
trade_amount_input.grid(row=3, column=1)
trade_amount_input.insert(0, "0.01")
ttk.Label(main_frame, text="Trade Amount (SOL)").grid(row=3, column=0)

wallet_balance = tk.StringVar()
wallet_balance.set("SOL Balance: N/A")
wallet_balance_label = ttk.Label(main_frame, textvariable=wallet_balance)
wallet_balance_label.grid(row=3, column=2, padx=5)

ttk.Button(main_frame, text="Start Bot", command=start_bot).grid(row=4, column=0, pady=10)
ttk.Button(main_frame, text="Stop Bot", command=stop_bot).grid(row=4, column=1, pady=10)
ttk.Button(main_frame, text="Reset Trade", command=reset_trade).grid(row=5, column=0, columnspan=2, pady=10)

log_output = tk.Text(main_frame, height=12, width=60)
log_output.grid(row=6, column=0, columnspan=2, pady=10)

prices = []
timestamps = []
fig, ax = plt.subplots(figsize=(6, 3))
canvas = FigureCanvasTkAgg(fig, master=main_frame)
canvas.draw()
canvas.get_tk_widget().grid(row=7, column=0, columnspan=2)

root.mainloop()