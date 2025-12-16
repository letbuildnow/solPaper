import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.error import BadRequest
import aiohttp
import json
from datetime import datetime, timedelta
import asyncio
import random
import time

# Paper trading portfolio
portfolios = {}
# Price alerts
alerts = {}
# Watchlist
watchlists = {}
# User Settings
user_settings = {}
# User Stats (Analytics)
user_stats = {}

class PaperTradingBot:
    def __init__(self):
        self.starting_balance = 20.0  # 10 SOL starting balance
        self.price_cache = {}  # Cache for API responses
        self.data_lock = asyncio.Lock()  # Prevent data corruption
        self.load_data()

    def apply_slippage(self, price, is_buy, user_id):
        """Calculate execution price based on user's slippage settings"""
        settings = user_settings.get(user_id, {})
        slippage_pct = settings.get('slippage', 1.0)
        
        # Simulate random slippage between 0 and max tolerance
        # In real markets, you don't always hit max slippage, but this simulates the risk
        impact = random.uniform(0, slippage_pct / 100)
        
        # Buy = Price increases (pay more), Sell = Price decreases (get less)
        exec_price = price * (1 + impact) if is_buy else price * (1 - impact)
        return exec_price, impact * 100

    def load_data(self):
        """Load bot data from file"""
        global portfolios, watchlists, user_settings, user_stats
        filename = 'paper_trading_data.json'
        if os.path.exists(filename):
            try:
                with open(filename, 'r') as f:
                    data = json.load(f)
                    # Convert string keys back to integers for user_ids
                    if 'portfolios' in data:
                        portfolios.clear()
                        for k, v in data['portfolios'].items():
                            portfolios[int(k)] = v
                    if 'watchlists' in data:
                        watchlists.clear()
                        for k, v in data['watchlists'].items():
                            watchlists[int(k)] = v
                    if 'user_settings' in data:
                        user_settings.clear()
                        for k, v in data['user_settings'].items():
                            user_settings[int(k)] = v
                    if 'user_stats' in data:
                        user_stats.clear()
                        for k, v in data['user_stats'].items():
                            user_stats[int(k)] = v
                print("✅ Data loaded successfully")
            except Exception as e:
                print(f"❌ Error loading data: {e}")

    async def save_data(self):
        """Save bot data to file asynchronously to prevent blocking"""
        async with self.data_lock:
            filename = 'paper_trading_data.json'
            try:
                data = {
                    'portfolios': portfolios,
                    'watchlists': watchlists,
                    'user_settings': user_settings,
                    'user_stats': user_stats
                }
                # Run blocking I/O in a separate thread
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._write_file_sync, filename, data)
            except Exception as e:
                print(f"❌ Error saving data: {e}")
    
    def _write_file_sync(self, filename, data):
        """Helper to write file synchronously (runs in background thread)"""
        temp_file = f"{filename}.tmp"
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(temp_file, filename)
        
    async def log_activity(self, update: Update, command_name: str):
        """Track user activity for analytics"""
        user = update.effective_user
        if not user:
            return
            
        user_id = user.id
        now = datetime.now().isoformat()
        
        if user_id not in user_stats:
            user_stats[user_id] = {
                'username': user.username,
                'first_name': user.first_name,
                'joined_at': now,
                'last_active': now,
                'commands': {}
            }
        
        stats = user_stats[user_id]
        stats['last_active'] = now
        stats['username'] = user.username
        
        if command_name not in stats['commands']:
            stats['commands'][command_name] = 0
        stats['commands'][command_name] += 1
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.log_activity(update, "start")
        user_id = update.effective_user.id
        if user_id not in portfolios:
            portfolios[user_id] = {
                'balance': self.starting_balance,
                'positions': {},
                'history': []
            }
            await self.save_data()
        
        await update.message.reply_text(
            f"🤖 *Solana Memecoin Paper Trading Bot*\n\n"
            f"💰 Balance: {portfolios[user_id]['balance']:.4f} SOL\n\n"
            f"*Trading Commands:*\n"
            f"/buy `<token>` `<amount_sol>` - Buy tokens\n"
            f"/sell `<token>` `<amount>` - Sell tokens\n"
            f"/portfolio - View portfolio\n"
            f"/leaderboard - Top traders\n"
            f"/history - Trade history\n\n"
            f"*Token Info:*\n"
            f"/info `<token>` - Full token details\n"
            f"/price `<token>` - Quick price check\n"
            f"/chart `<token>` - Price chart link\n\n"
            f"*Tools:*\n"
            f"/watch `<token>` - Add to watchlist\n"
            f"/watchlist - View watchlist\n"
            f"/alert `<token>` `<price>` - Set price alert\n"
            f"/alerts - View all alerts\n"
            f"/settings - Configure bot\n\n"
            f"*Account:*\n"
            f"/fund `<amount>` - Add virtual SOL\n"
            f"/reset - Reset portfolio\n"
            f"/stats - Your trading stats\n\n"
            f"💡 *Tip:* Just paste a token address for instant info!",
            parse_mode='Markdown'
        )
    
    async def get_token_info(self, token_address):
        """Get comprehensive token information with multiple API fallbacks"""
        # 1. Check Cache (Optimization)
        current_time = time.time()
        if token_address in self.price_cache:
            cached = self.price_cache[token_address]
            if current_time - cached['timestamp'] < 10:  # 10 seconds cache
                return cached['data']

        info = {
            'price': None,
            'price_usd': None,
            'name': None,
            'symbol': None,
            'market_cap': None,
            'liquidity': None,
            'volume_24h': None,
            'price_change_24h': None,
            'holders': None,
            'created_at': None,
            'dex_name': None,
            'pair_address': None,
            'sol_price': 0,
            'price_timestamp': None
        }
        
        # Create session with timeout
        timeout = aiohttp.ClientTimeout(total=10)
        connector = aiohttp.TCPConnector(ssl=False)  # Disable SSL verification as fallback
        
        try:
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                # 0. Fetch global SOL price first (needed for conversions)
                sol_price = 0
                try:
                    async with session.get("https://price.jup.ag/v4/price?ids=SOL") as sol_resp:
                        if sol_resp.status == 200:
                            sol_json = await sol_resp.json()
                            sol_price = float(sol_json['data']['SOL']['price'])
                            info['sol_price'] = sol_price
                except Exception as e:
                    print(f"⚠️ Failed to fetch SOL price from Jupiter: {e}")
                    # Fallback: Try DexScreener for SOL price (Wrapped SOL)
                    try:
                        async with session.get("https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112") as sol_dex_resp:
                            if sol_dex_resp.status == 200:
                                sol_data = await sol_dex_resp.json()
                                if sol_data.get('pairs'):
                                    # Use the first pair's priceUsd
                                    sol_price = float(sol_data['pairs'][0].get('priceUsd', 0))
                                    info['sol_price'] = sol_price
                                    print(f"✅ Fetched SOL price from DexScreener: ${sol_price}")
                    except Exception as e2:
                        print(f"⚠️ Failed to fetch SOL price from DexScreener: {e2}")

                # 1. Try Pump.fun first (User Request for Bonding Curve tokens)
                try:
                    pump_url = f"https://frontend-api.pump.fun/coins/{token_address}"
                    headers = {"User-Agent": "Mozilla/5.0"} # User-Agent is often required
                    async with session.get(pump_url, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            # Only use Pump.fun if the curve is NOT complete (still on bonding curve)
                            # If complete, DexScreener (Raydium) is usually more accurate
                            if 'mint' in data and not data.get('complete', False):
                                    v_sol = int(data.get('virtual_sol_reserves', 0))
                                    v_token = int(data.get('virtual_token_reserves', 0))

                                    if v_token > 0:
                                        # Calculate price: (Virtual SOL / 1e9) / (Virtual Token / 1e6)
                                        price_sol = (v_sol / 1e9) / (v_token / 1e6)
                                        info['price'] = price_sol # Price in SOL
                                        info['price_usd'] = price_sol * sol_price if sol_price else 0
                                        info['name'] = data.get('name', 'Unknown')
                                        info['symbol'] = data.get('symbol', 'Unknown')
                                        info['market_cap'] = data.get('market_cap', 0) * sol_price # MC usually in USD
                                        info['dex_name'] = 'Pump.fun (Bonding Curve)'
                                        info['price_timestamp'] = datetime.now()
                                        if data.get('created_timestamp'):
                                            info['created_at'] = datetime.fromtimestamp(data.get('created_timestamp') / 1000)
                                        return info
                except Exception as e:
                    print(f"⚠️ Pump.fun failed: {e}")

                # Primary source: DexScreener (most reliable)
                try:
                    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
                    async with session.get(dex_url) as response:
                        if response.status == 200:
                            data = await response.json()
                            if 'pairs' in data and len(data['pairs']) > 0:
                                # Get the pair with highest liquidity (most accurate price)
                                pair = max(data['pairs'], key=lambda x: x.get('liquidity', {}).get('usd', 0) or 0)
                                
                                info['name'] = pair.get('baseToken', {}).get('name', 'Unknown')
                                info['symbol'] = pair.get('baseToken', {}).get('symbol', 'Unknown')
                                info['price_usd'] = float(pair.get('priceUsd', 0))
                                if sol_price > 0:
                                    info['price'] = info['price_usd'] / sol_price
                                info['market_cap'] = pair.get('fdv')
                                info['liquidity'] = pair.get('liquidity', {}).get('usd')
                                info['volume_24h'] = pair.get('volume', {}).get('h24')
                                info['price_change_24h'] = pair.get('priceChange', {}).get('h24')
                                info['dex_name'] = pair.get('dexId', 'Unknown DEX')
                                info['pair_address'] = pair.get('pairAddress', '')
                                info['price_timestamp'] = datetime.now()
                                
                                # Created timestamp
                                created = pair.get('pairCreatedAt')
                                if created:
                                    info['created_at'] = datetime.fromtimestamp(created / 1000)
                                
                                if info['price'] is not None:
                                    print(f"✅ DexScreener: Found {info['symbol']} at {info['price']:.9f} SOL")
                                else:
                                    print(f"⚠️ DexScreener: Found {info['symbol']} ($ {info['price_usd']}) but SOL price is missing")
                except Exception as e:
                    print(f"⚠️ DexScreener failed: {e}")
                
                # Fallback 1: Try Jupiter API if DexScreener didn't get price
                if not info['price']:
                    try:
                        price_url = f"https://price.jup.ag/v4/price?ids={token_address}"
                        async with session.get(price_url) as response:
                            if response.status == 200:
                                data = await response.json()
                                if 'data' in data and token_address in data['data']:
                                    price_data = data['data'][token_address]
                                    info['price_usd'] = float(price_data.get('price', 0))
                                    if sol_price > 0:
                                        info['price'] = info['price_usd'] / sol_price
                                    info['dex_name'] = 'Jupiter Aggregated'
                                    info['price_timestamp'] = datetime.now()
                                    if info['price'] is not None:
                                        print(f"✅ Jupiter: Found price {info['price']:.9f} SOL")
                                    else:
                                        print(f"⚠️ Jupiter: Found price ${info['price_usd']} but SOL price is missing")
                    except Exception as e:
                        print(f"⚠️ Jupiter failed: {e}")
                
                # Fallback 2: Try Birdeye API
                if not info['price']:
                    try:
                        birdeye_url = f"https://public-api.birdeye.so/public/price?address={token_address}"
                        headers = {"X-API-KEY": "public"}  # Public endpoint
                        async with session.get(birdeye_url, headers=headers) as response:
                            if response.status == 200:
                                data = await response.json()
                                if 'data' in data and 'value' in data['data']:
                                    info['price_usd'] = float(data['data']['value'])
                                    if sol_price > 0:
                                        info['price'] = info['price_usd'] / sol_price
                                    info['dex_name'] = 'Birdeye'
                                    info['price_timestamp'] = datetime.now()
                                    if info['price'] is not None:
                                        print(f"✅ Birdeye: Found price {info['price']:.9f} SOL")
                                    else:
                                        print(f"⚠️ Birdeye: Found price ${info['price_usd']} but SOL price is missing")
                    except Exception as e:
                        print(f"⚠️ Birdeye failed: {e}")
                
        except Exception as e:
            print(f"❌ All APIs failed: {e}")
        
        # Save to cache
        self.price_cache[token_address] = {
            'timestamp': time.time(),
            'data': info
        }
        return info
    
    async def info_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Detailed token information"""
        await self.log_activity(update, "info")
        if not context.args:
            await update.message.reply_text("Usage: /info <token_address>")
            return
        
        token = context.args[0]
        msg = await update.message.reply_text("🔍 Fetching token info...")
        
        info = await self.get_token_info(token)
        
        if not info['price']:
            await msg.edit_text("❌ Token not found or invalid address")
            return
        
        # Format the response
        response = f"🪙 *{info['name'] or 'Unknown'}* ({info['symbol'] or 'N/A'})\n\n"
        response += f"📍 Address: `{token[:8]}...{token[-8:]}`\n"
        
        # Show price source and timestamp
        if info['dex_name']:
            response += f"🔄 Source: {info['dex_name']}\n"
        if info['price_timestamp']:
            response += f"⏰ Updated: {info['price_timestamp'].strftime('%H:%M:%S')}\n"
        response += "\n"
        
        response += f"💵 *Price:* {info['price']:.9f} SOL (~${info['price_usd']:.4f})\n"
        
        if info['price_change_24h']:
            change_emoji = "📈" if float(info['price_change_24h']) > 0 else "📉"
            response += f"{change_emoji} *24h Change:* {float(info['price_change_24h']):.2f}%\n"
        
        if info['market_cap']:
            response += f"💎 *Market Cap:* ${float(info['market_cap']):,.0f}\n"
        
        if info['liquidity']:
            response += f"💧 *Liquidity:* ${float(info['liquidity']):,.0f}\n"
        
        if info['volume_24h']:
            response += f"📊 *24h Volume:* ${float(info['volume_24h']):,.0f}\n"
        
        if info['created_at']:
            age = datetime.now() - info['created_at']
            if age.days > 0:
                response += f"🎂 *Age:* {age.days} days\n"
            else:
                hours = age.seconds // 3600
                response += f"🎂 *Age:* {hours} hours (NEW!)\n"
        
        response += f"\n🔗 [DexScreener](https://dexscreener.com/solana/{token})"
        response += f" | [Birdeye](https://birdeye.so/token/{token})"
        
        # Add quick action buttons
        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh Price", callback_data=f"refresh_{token}"),
            ],
            [
                InlineKeyboardButton("📈 Buy 0.5 SOL", callback_data=f"qbuy_{token}_0.5"),
                InlineKeyboardButton("📈 Buy 1.0 SOL", callback_data=f"qbuy_{token}_1.0")
            ],
            [
                InlineKeyboardButton("⭐ Add to Watchlist", callback_data=f"watch_{token}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await msg.edit_text(response, parse_mode='Markdown', reply_markup=reply_markup, disable_web_page_preview=True)
    
    async def handle_address_paste(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Auto-detect when user pastes a Solana address"""
        text = update.message.text.strip()
        
        # Check if it looks like a Solana address (base58, 32-44 chars)
        if len(text) >= 32 and len(text) <= 44 and text.isalnum():
            # Likely a token address, show info automatically
            context.args = [text]
            await self.info_command(update, context)
    
    async def quick_buy_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle quick buy buttons and refresh"""
        query = update.callback_query
        await query.answer()
        
        data = query.data.split('_')
        action = data[0]
        token = data[1]
        
        user_id = query.from_user.id
        
        # Handle refresh button
        if action == "refresh":
            await query.answer("🔄 Refreshing price...")
            info = await self.get_token_info(token)
            
            if not info['price']:
                await query.edit_message_text("❌ Could not fetch price")
                return
            
            # Rebuild the message with updated price
            response = f"🪙 *{info['name'] or 'Unknown'}* ({info['symbol'] or 'N/A'})\n\n"
            response += f"📍 Address: `{token[:8]}...{token[-8:]}`\n"
            
            if info['dex_name']:
                response += f"🔄 Source: {info['dex_name']}\n"
            if info['price_timestamp']:
                response += f"⏰ Updated: {info['price_timestamp'].strftime('%H:%M:%S')}\n"
            response += "\n"
            
            response += f"💵 *Price:* {info['price']:.9f} SOL (~${info['price_usd']:.4f})\n"
            
            if info['price_change_24h']:
                change_emoji = "📈" if float(info['price_change_24h']) > 0 else "📉"
                response += f"{change_emoji} *24h Change:* {float(info['price_change_24h']):.2f}%\n"
            
            if info['market_cap']:
                response += f"💎 *Market Cap:* ${float(info['market_cap']):,.0f}\n"
            
            if info['liquidity']:
                response += f"💧 *Liquidity:* ${float(info['liquidity']):,.0f}\n"
            
            if info['volume_24h']:
                response += f"📊 *24h Volume:* ${float(info['volume_24h']):,.0f}\n"
            
            if info['created_at']:
                age = datetime.now() - info['created_at']
                if age.days > 0:
                    response += f"🎂 *Age:* {age.days} days\n"
                else:
                    hours = age.seconds // 3600
                    response += f"🎂 *Age:* {hours} hours (NEW!)\n"
            
            response += f"\n🔗 [DexScreener](https://dexscreener.com/solana/{token})"
            response += f" | [Birdeye](https://birdeye.so/token/{token})"
            
            keyboard = [
                [
                    InlineKeyboardButton("🔄 Refresh Price", callback_data=f"refresh_{token}"),
                ],
                [
                    InlineKeyboardButton("📈 Buy 0.5 SOL", callback_data=f"qbuy_{token}_0.5"),
                    InlineKeyboardButton("📈 Buy 1.0 SOL", callback_data=f"qbuy_{token}_1.0")
                ],
                [
                    InlineKeyboardButton("⭐ Add to Watchlist", callback_data=f"watch_{token}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(response, parse_mode='Markdown', reply_markup=reply_markup, disable_web_page_preview=True)
            return
        
        # Handle buy and watchlist
        if user_id not in portfolios:
            await self.log_activity(update, "quick_buy_attempt")
            await query.edit_message_text("Use /start first to create a portfolio")
            return
        
        if action == "qbuy":
            amount = float(data[2])
            portfolio = portfolios[user_id]
            
            if amount > portfolio['balance']:
                await query.edit_message_text(f"❌ Insufficient balance! You have {portfolio['balance']:.4f} SOL")
                return
            
            info = await self.get_token_info(token)
            price = info['price']
            
            if not price:
                await query.edit_message_text("❌ Could not fetch price")
                return

            # Apply slippage
            exec_price, slippage_hit = self.apply_slippage(price, True, user_id)
            tokens = amount / exec_price
            
            # Update portfolio
            portfolio['balance'] -= amount
            
            if token in portfolio['positions']:
                old_tokens = portfolio['positions'][token]['amount']
                old_avg = portfolio['positions'][token]['avg_price']
                new_total = old_tokens + tokens
                new_avg = ((old_tokens * old_avg) + (tokens * exec_price)) / new_total
                portfolio['positions'][token] = {
                    'amount': new_total,
                    'avg_price': new_avg,
                    'symbol': info['symbol']
                }
            else:
                portfolio['positions'][token] = {
                    'amount': tokens,
                    'avg_price': exec_price,
                    'symbol': info['symbol']
                }
            
            # Record trade
            portfolio['history'].append({
                'type': 'BUY',
                'token': token,
                'amount': tokens,
                'price': exec_price,
                'value_sol': amount,
                'dex': info.get('dex_name', 'Unknown'),
                'timestamp': datetime.now().isoformat()
            })
            
            await self.log_activity(update, "quick_buy")
            await self.save_data()
            
            response = f"✅ *Bought {tokens:.2f} {info['symbol'] or 'tokens'}*\n\n"
            response += f"💵 Price: {exec_price:.9f} SOL (Slip: {slippage_hit:.2f}%)\n"
            if info.get('dex_name'):
                response += f"🔄 Source: {info['dex_name']}\n"
            response += f"💰 Spent: {amount:.4f} SOL\n"
            response += f"💰 New Balance: {portfolio['balance']:.4f} SOL"
            
            await query.edit_message_text(response, parse_mode='Markdown')
        
        elif action == "watch":
            if user_id not in watchlists:
                watchlists[user_id] = []
            
            if token not in watchlists[user_id]:
                watchlists[user_id].append(token)
                await query.edit_message_text(f"⭐ Added to watchlist!\n\nUse /watchlist to see all watched tokens")
                await self.log_activity(update, "watch_click")
                await self.save_data()
            else:
                await query.edit_message_text("Already in your watchlist!")
    
    async def get_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Usage: /price <token_address>")
            return
        
        token = context.args[0]
        info = await self.get_token_info(token)
        
        if info['price']:
            response = f"💵 *Price:* {info['price']:.9f} SOL (~${info['price_usd']:.4f})\n"
            if info['symbol']:
                response = f"🪙 *{info['symbol']}*\n\n" + response
            if info.get('dex_name'):
                response += f"🔄 Source: {info['dex_name']}\n"
            if info.get('price_timestamp'):
                response += f"⏰ {info['price_timestamp'].strftime('%H:%M:%S')}\n"
            if info['price_change_24h']:
                change_emoji = "📈" if float(info['price_change_24h']) > 0 else "📉"
                response += f"{change_emoji} *24h:* {float(info['price_change_24h']):.2f}%"
            
            await update.message.reply_text(response, parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Could not fetch price")
    
    async def buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.log_activity(update, "buy")
        user_id = update.effective_user.id
        
        if user_id not in portfolios:
            portfolios[user_id] = {
                'balance': self.starting_balance,
                'positions': {},
                'history': []
            }
            await self.save_data()
        
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /buy <token_address> <sol_amount>")
            return
        
        token = context.args[0]
        try:
            sol_amount = float(context.args[1])
        except:
            await update.message.reply_text("❌ Invalid amount")
            return
        
        portfolio = portfolios[user_id]
        
        if sol_amount > portfolio['balance']:
            await update.message.reply_text(f"❌ Insufficient balance! You have {portfolio['balance']:.4f} SOL")
            return
        
        info = await self.get_token_info(token)
        price = info['price']
        
        if not price:
            await update.message.reply_text("❌ Could not fetch price")
            return
        
        # Apply slippage
        exec_price, slippage_hit = self.apply_slippage(price, True, user_id)
        tokens = sol_amount / exec_price
        
        # Update portfolio
        portfolio['balance'] -= sol_amount
        
        if token in portfolio['positions']:
            old_tokens = portfolio['positions'][token]['amount']
            old_avg = portfolio['positions'][token]['avg_price']
            new_total = old_tokens + tokens
            new_avg = ((old_tokens * old_avg) + (tokens * exec_price)) / new_total
            portfolio['positions'][token] = {
                'amount': new_total,
                'avg_price': new_avg,
                'symbol': info['symbol']
            }
        else:
            portfolio['positions'][token] = {
                'amount': tokens,
                'avg_price': exec_price,
                'symbol': info['symbol']
            }
        
        # Record trade
        portfolio['history'].append({
            'type': 'BUY',
            'token': token,
            'amount': tokens,
            'price': exec_price,
            'value_sol': sol_amount,
            'timestamp': datetime.now().isoformat()
        })
        
        await self.save_data()
        
        await update.message.reply_text(
            f"✅ *Bought {tokens:.2f} {info['symbol'] or 'tokens'}*\n\n"
            f"💵 Price: {exec_price:.9f} SOL (Slip: {slippage_hit:.2f}%)\n"
            f"💰 Spent: {sol_amount:.4f} SOL\n"
            f"💰 New Balance: {portfolio['balance']:.4f} SOL",
            parse_mode='Markdown'
        )
    
    async def sell(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.log_activity(update, "sell")
        user_id = update.effective_user.id
        
        if user_id not in portfolios:
            await update.message.reply_text("You don't have a portfolio yet. Use /start")
            return
        
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /sell <token_address> <token_amount> OR /sell <token_address> all")
            return
        
        token = context.args[0]
        portfolio = portfolios[user_id]
        
        if token not in portfolio['positions']:
            await update.message.reply_text("❌ You don't own this token")
            return
        
        # Check for "all" keyword
        if context.args[1].lower() == 'all':
            amount = portfolio['positions'][token]['amount']
        else:
            try:
                amount = float(context.args[1])
            except:
                await update.message.reply_text("❌ Invalid amount")
                return
        
        if amount > portfolio['positions'][token]['amount']:
            await update.message.reply_text(
                f"❌ You only have {portfolio['positions'][token]['amount']:.2f} tokens"
            )
            return
        
        info = await self.get_token_info(token)
        price = info['price']
        
        if not price:
            await update.message.reply_text("❌ Could not fetch price")
            return
        
        # Apply slippage
        exec_price, slippage_hit = self.apply_slippage(price, False, user_id)
        sol_amount = amount * exec_price
        avg_price = portfolio['positions'][token]['avg_price']
        profit = (exec_price - avg_price) * amount
        profit_pct = ((exec_price / avg_price) - 1) * 100
        
        # Update portfolio
        portfolio['balance'] += sol_amount
        portfolio['positions'][token]['amount'] -= amount
        
        if portfolio['positions'][token]['amount'] < 0.0001:
            del portfolio['positions'][token]
        
        # Record trade
        portfolio['history'].append({
            'type': 'SELL',
            'token': token,
            'amount': amount,
            'price': exec_price,
            'value_sol': sol_amount,
            'profit': profit,
            'timestamp': datetime.now().isoformat()
        })
        
        await self.save_data()
        
        profit_emoji = "📈" if profit > 0 else "📉"
        await update.message.reply_text(
            f"✅ *Sold {amount:.2f} {info['symbol'] or 'tokens'}*\n\n"
            f"💵 Price: {exec_price:.9f} SOL (Slip: {slippage_hit:.2f}%)\n"
            f"💰 Received: {sol_amount:.4f} SOL\n"
            f"{profit_emoji} *P/L:* {profit:.4f} SOL ({profit_pct:+.2f}%)\n"
            f"💰 New Balance: {portfolio['balance']:.4f} SOL",
            parse_mode='Markdown'
        )
    
    async def portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.log_activity(update, "portfolio")
        user_id = update.effective_user.id
        
        if user_id not in portfolios:
            await update.message.reply_text("You don't have a portfolio yet. Use /start")
            return
        
        portfolio = portfolios[user_id]
        
        msg = f"📊 *Your Portfolio*\n\n💰 Cash: {portfolio['balance']:.4f} SOL\n\n"
        
        if not portfolio['positions']:
            msg += "No positions"
            await update.message.reply_text(msg, parse_mode='Markdown')
        else:
            total_value = portfolio['balance']
            msg += "*Positions:*\n"
            
            # Create buttons for each position
            keyboard = []
            
            for token, pos in portfolio['positions'].items():
                info = await self.get_token_info(token)
                price = info['price']
                symbol = pos.get('symbol', 'N/A')
                
                if not price:
                    continue
                
                value = pos['amount'] * price
                profit = (price - pos['avg_price']) * pos['amount']
                profit_pct = ((price / pos['avg_price']) - 1) * 100
                total_value += value
                
                profit_emoji = "📈" if profit > 0 else "📉"
                msg += (
                    f"\n🪙 *{symbol}*\n"
                    f"   Amount: {pos['amount']:.2f}\n"
                    f"   Avg: {pos['avg_price']:.9f} SOL\n"
                    f"   Now: {price:.9f} SOL\n"
                    f"   Value: {value:.4f} SOL\n"
                    f"   {profit_emoji} P/L: {profit:.4f} SOL ({profit_pct:+.2f}%)\n"
                )
                
                # Add button for this position
                keyboard.append([
                    InlineKeyboardButton(f"🎯 {symbol} Actions", callback_data=f"pos_{token}")
                ])
            
            total_profit = total_value - self.starting_balance
            total_pct = ((total_value / self.starting_balance) - 1) * 100
            msg += f"\n💼 *Total Value:* {total_value:.4f} SOL\n"
            msg += f"📊 *Total P/L:* {total_profit:.4f} SOL ({total_pct:+.2f}%)"
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def position_actions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed actions for a specific position"""
        query = update.callback_query
        await query.answer()
        
        data = query.data.split('_')
        if data[0] != 'pos':
            return
        
        token = data[1]
        user_id = query.from_user.id
        
        if user_id not in portfolios or token not in portfolios[user_id]['positions']:
            await query.edit_message_text("Position not found")
            return
        
        portfolio = portfolios[user_id]
        pos = portfolio['positions'][token]
        
        # Get current price
        info = await self.get_token_info(token)
        price = info['price']
        
        if not price:
            await query.edit_message_text("❌ Could not fetch current price")
            return
        
        # Calculate P/L
        value = pos['amount'] * price
        profit = (price - pos['avg_price']) * pos['amount']
        profit_pct = ((price / pos['avg_price']) - 1) * 100
        
        profit_emoji = "📈" if profit > 0 else "📉"
        
        # Build message
        msg = f"🪙 *{pos.get('symbol', 'Token')} Position*\n\n"
        msg += f"📊 *Holdings:* {pos['amount']:.2f}\n"
        msg += f"💰 *Value:* {value:.4f} SOL (~${value * info.get('sol_price', 0):.2f})\n\n"
        msg += f"📍 *Entry Price:* {pos['avg_price']:.9f} SOL\n"
        msg += f"💵 *Current Price:* {price:.9f} SOL\n"
        
        if info.get('dex_name'):
            msg += f"🔄 *Source:* {info['dex_name']}\n"
        if info.get('price_timestamp'):
            msg += f"⏰ *Updated:* {info['price_timestamp'].strftime('%H:%M:%S')}\n"
        
        msg += f"\n{profit_emoji} *Unrealized P/L:*\n"
        msg += f"   {profit:.4f} SOL ({profit_pct:+.2f}%)\n"
        
        # Add price change if available
        if info.get('price_change_24h'):
            change_emoji = "📈" if float(info['price_change_24h']) > 0 else "📉"
            msg += f"\n{change_emoji} *24h Change:* {float(info['price_change_24h']):.2f}%"
        
        # Create action buttons
        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=f"posrefresh_{token}"),
                InlineKeyboardButton("📊 Chart", callback_data=f"poschart_{token}")
            ],
            [
                InlineKeyboardButton("📈 Buy 0.5 SOL", callback_data=f"posbuy_{token}_0.5"),
                InlineKeyboardButton("📈 Buy 1.0 SOL", callback_data=f"posbuy_{token}_1.0")
            ],
            [
                InlineKeyboardButton("📉 Sell 25%", callback_data=f"possell_{token}_25"),
                InlineKeyboardButton("📉 Sell 50%", callback_data=f"possell_{token}_50")
            ],
            [
                InlineKeyboardButton("📉 Sell 75%", callback_data=f"possell_{token}_75"),
                InlineKeyboardButton("🔴 Sell All", callback_data=f"possell_{token}_100")
            ],
            [
                InlineKeyboardButton("⬅️ Back to Portfolio", callback_data="back_portfolio")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup, disable_web_page_preview=True)
    
    async def handle_position_actions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle actions from position detail screen"""
        query = update.callback_query
        await query.answer()
        
        data = query.data.split('_')
        action = data[0]
        
        # Handle back to portfolio
        if query.data == "back_portfolio":
            user_id = query.from_user.id
            if user_id not in portfolios:
                await query.edit_message_text("Portfolio not found")
                return
            
            # Recreate portfolio view
            portfolio = portfolios[user_id]
            msg = f"📊 *Your Portfolio*\n\n💰 Cash: {portfolio['balance']:.4f} SOL\n\n"
            
            if not portfolio['positions']:
                msg += "No positions"
                await query.edit_message_text(msg, parse_mode='Markdown')
                return
            
            total_value = portfolio['balance']
            msg += "*Positions:*\n"
            keyboard = []
            
            for token, pos in portfolio['positions'].items():
                info = await self.get_token_info(token)
                price = info['price']
                symbol = pos.get('symbol', 'N/A')
                
                if not price:
                    continue
                
                value = pos['amount'] * price
                profit = (price - pos['avg_price']) * pos['amount']
                profit_pct = ((price / pos['avg_price']) - 1) * 100
                total_value += value
                
                profit_emoji = "📈" if profit > 0 else "📉"
                msg += (
                    f"\n🪙 *{symbol}*\n"
                    f"   Amount: {pos['amount']:.2f}\n"
                    f"   Avg: {pos['avg_price']:.9f} SOL\n"
                    f"   Now: {price:.9f} SOL\n"
                    f"   Value: {value:.4f} SOL\n"
                    f"   {profit_emoji} P/L: {profit:.4f} SOL ({profit_pct:+.2f}%)\n"
                )
                
                keyboard.append([
                    InlineKeyboardButton(f"🎯 {symbol} Actions", callback_data=f"pos_{token}")
                ])
            
            total_profit = total_value - self.starting_balance
            total_pct = ((total_value / self.starting_balance) - 1) * 100
            msg += f"\n💼 *Total Value:* {total_value:.4f} SOL\n"
            msg += f"📊 *Total P/L:* {total_profit:.4f} SOL ({total_pct:+.2f}%)"
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)
            return
        
        token = data[1]
        user_id = query.from_user.id
        
        if user_id not in portfolios:
            await query.edit_message_text("Portfolio not found")
            return
        
        # Handle refresh
        if action == "posrefresh":
            await self.position_actions(update, context)
            return
        
        # Handle chart
        if action == "poschart":
            await query.edit_message_text(
                f"📈 *Chart Links:*\n\n"
                f"🦅 [Birdeye](https://birdeye.so/token/{token}?chain=solana)\n"
                f"📊 [DexScreener](https://dexscreener.com/solana/{token})\n"
                f"🔥 [DexTools](https://www.dextools.io/app/en/solana/pair-explorer/{token})\n\n"
                f"Click refresh on your position to go back",
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
            return
        
        # Handle buy more
        if action == "posbuy":
            amount = float(data[2])
            portfolio = portfolios[user_id]
            
            if amount > portfolio['balance']:
                await query.edit_message_text(f"❌ Insufficient balance! You have {portfolio['balance']:.4f} SOL")
                return
            
            info = await self.get_token_info(token)
            price = info['price']
            
            if not price:
                await query.edit_message_text("❌ Could not fetch price")
                return
            
            # Apply slippage
            exec_price, slippage_hit = self.apply_slippage(price, True, user_id)
            tokens = amount / exec_price
            
            # Update position
            old_tokens = portfolio['positions'][token]['amount']
            old_avg = portfolio['positions'][token]['avg_price']
            new_total = old_tokens + tokens
            new_avg = ((old_tokens * old_avg) + (tokens * exec_price)) / new_total
            
            portfolio['balance'] -= amount
            portfolio['positions'][token]['amount'] = new_total
            portfolio['positions'][token]['avg_price'] = new_avg
            
            # Record trade
            portfolio['history'].append({
                'type': 'BUY',
                'token': token,
                'amount': tokens,
                'price': exec_price,
                'value_sol': amount,
                'timestamp': datetime.now().isoformat()
            })
            
            await self.save_data()
            
            await query.edit_message_text(
                f"✅ *Bought {tokens:.2f} more!*\n\n"
                f"💵 Price: {exec_price:.9f} SOL (Slip: {slippage_hit:.2f}%)\n"
                f"💰 Spent: {amount:.4f} SOL\n"
                f" New Position: {new_total:.2f}\n"
                f"📍 New Avg: {new_avg:.9f} SOL\n"
                f"💰 Balance: {portfolio['balance']:.4f} SOL\n\n"
                f"Use /portfolio to see updated positions",
                parse_mode='Markdown'
            )
            return
        
        # Handle sell percentage
        if action == "possell":
            percentage = float(data[2])
            portfolio = portfolios[user_id]
            
            if token not in portfolio['positions']:
                await query.edit_message_text("Position not found")
                return
            
            pos = portfolio['positions'][token]
            amount = pos['amount'] * (percentage / 100)
            
            info = await self.get_token_info(token)
            price = info['price']
            
            if not price:
                await query.edit_message_text("❌ Could not fetch price")
                return
            
            # Apply slippage
            exec_price, slippage_hit = self.apply_slippage(price, False, user_id)
            sol_amount = amount * exec_price
            profit = (exec_price - pos['avg_price']) * amount
            profit_pct = ((exec_price / pos['avg_price']) - 1) * 100
            
            # Update portfolio
            portfolio['balance'] += sol_amount
            portfolio['positions'][token]['amount'] -= amount
            
            # Remove position if sold all
            if portfolio['positions'][token]['amount'] < 0.0001:
                del portfolio['positions'][token]
            
            # Record trade
            portfolio['history'].append({
                'type': 'SELL',
                'token': token,
                'amount': amount,
                'price': exec_price,
                'value_sol': sol_amount,
                'profit': profit,
                'timestamp': datetime.now().isoformat()
            })
            
            await self.save_data()
            
            profit_emoji = "📈" if profit > 0 else "📉"
            await query.edit_message_text(
                f"✅ *Sold {percentage:.0f}% ({amount:.2f} tokens)*\n\n"
                f"💵 Price: {exec_price:.9f} SOL (Slip: {slippage_hit:.2f}%)\n"
                f"💰 Received: {sol_amount:.4f} SOL\n"
                f"{profit_emoji} *P/L:* {profit:.4f} SOL ({profit_pct:+.2f}%)\n"
                f"💰 New Balance: {portfolio['balance']:.4f} SOL\n\n"
                f"Use /portfolio to see updated positions",
                parse_mode='Markdown'
            )
            return
    
    async def watchlist_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id not in watchlists or not watchlists[user_id]:
            await update.message.reply_text("⭐ Your watchlist is empty!\n\nUse /watch <token> to add tokens")
            return
        
        msg = "⭐ *Your Watchlist*\n\n"
        
        for token in watchlists[user_id]:
            info = await self.get_token_info(token)
            if info['price']:
                symbol = info['symbol'] or 'Unknown'
                change = info['price_change_24h'] or 0
                change_emoji = "📈" if float(change) > 0 else "📉"
                
                msg += f"🪙 *{symbol}*\n"
                msg += f"   {info['price']:.9f} SOL {change_emoji} {float(change):.2f}%\n\n"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def watch_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Usage: /watch <token_address>")
            return
        
        user_id = update.effective_user.id
        token = context.args[0]
        
        if user_id not in watchlists:
            watchlists[user_id] = []
        
        if token in watchlists[user_id]:
            await update.message.reply_text("Already in your watchlist!")
            return
        
        watchlists[user_id].append(token)
        info = await self.get_token_info(token)
        await self.save_data()
        
        await update.message.reply_text(
            f"⭐ Added *{info['symbol'] or 'token'}* to watchlist!\n\n"
            f"Use /watchlist to see all watched tokens",
            parse_mode='Markdown'
        )
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id not in portfolios or not portfolios[user_id]['history']:
            await update.message.reply_text("No trading history yet!")
            return
        
        portfolio = portfolios[user_id]
        history = portfolio['history']
        
        total_trades = len(history)
        buys = [t for t in history if t['type'] == 'BUY']
        sells = [t for t in history if t['type'] == 'SELL']
        
        winning_trades = [t for t in sells if t.get('profit', 0) > 0]
        losing_trades = [t for t in sells if t.get('profit', 0) < 0]
        
        total_profit = sum(t.get('profit', 0) for t in sells)
        win_rate = (len(winning_trades) / len(sells) * 100) if sells else 0
        
        msg = f"📊 *Your Trading Stats*\n\n"
        msg += f"📈 Total Trades: {total_trades}\n"
        msg += f"🟢 Buys: {len(buys)}\n"
        msg += f"🔴 Sells: {len(sells)}\n\n"
        
        if sells:
            msg += f"✅ Winning Trades: {len(winning_trades)}\n"
            msg += f"❌ Losing Trades: {len(losing_trades)}\n"
            msg += f"🎯 Win Rate: {win_rate:.1f}%\n\n"
            msg += f"💰 Total Realized P/L: {total_profit:.4f} SOL\n"
        
        # Current portfolio value
        total_value = portfolio['balance']
        for token, pos in portfolio['positions'].items():
            info = await self.get_token_info(token)
            if info['price']:
                total_value += pos['amount'] * info['price']
        
        overall_pl = total_value - self.starting_balance
        overall_pct = ((total_value / self.starting_balance) - 1) * 100
        
        msg += f"\n📊 Overall P/L: {overall_pl:.4f} SOL ({overall_pct:+.2f}%)"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def leaderboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not portfolios:
            await update.message.reply_text("No active traders yet!")
            return
            
        msg = await update.message.reply_text("🏆 Calculating leaderboard... (fetching live prices)")
        
        # Get all unique tokens to fetch prices efficiently
        all_tokens = set()
        for data in portfolios.values():
            all_tokens.update(data['positions'].keys())
            
        # Fetch prices in parallel
        token_prices = {}
        tasks = [self.get_token_info(token) for token in all_tokens]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for token, result in zip(all_tokens, results):
            if isinstance(result, dict) and result.get('price'):
                token_prices[token] = result['price']
        
        leaderboard_data = []
        
        for user_id, data in portfolios.items():
            total_value = data['balance']
            
            for token, pos in data['positions'].items():
                price = token_prices.get(token, pos['avg_price'])
                total_value += pos['amount'] * price
            
            leaderboard_data.append({
                'user_id': user_id,
                'equity': total_value,
                'pnl_pct': ((total_value / self.starting_balance) - 1) * 100
            })
            
        leaderboard_data.sort(key=lambda x: x['equity'], reverse=True)
        
        text = "🏆 *Top Traders Leaderboard*\n\n"
        for i, trader in enumerate(leaderboard_data[:10], 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            text += f"{medal} *User {trader['user_id']}*\n"
            text += f"   💰 {trader['equity']:,.4f} SOL ({trader['pnl_pct']:+.2f}%)\n\n"
            
        await msg.edit_text(text, parse_mode='Markdown')

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id not in portfolios or not portfolios[user_id]['history']:
            await update.message.reply_text("No trade history yet")
            return
        
        history = portfolios[user_id]['history'][-10:]  # Last 10 trades
        
        msg = "📜 *Trade History* (Last 10)\n\n"
        
        for trade in reversed(history):
            emoji = "🟢" if trade['type'] == 'BUY' else "🔴"
            msg += f"{emoji} *{trade['type']}*\n"
            msg += f"   {trade['token'][:8]}...\n"
            msg += f"   {trade['amount']:.2f} @ {trade['price']:.9f} SOL\n"
            if trade['type'] == 'SELL' and 'profit' in trade:
                pl_emoji = "📈" if trade['profit'] > 0 else "📉"
                msg += f"   {pl_emoji} P/L: {trade['profit']:.4f} SOL\n"
            msg += "\n"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def chart_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Usage: /chart <token_address>")
            return
        
        token = context.args[0]
        
        await update.message.reply_text(
            f"📈 *Chart Links:*\n\n"
            f"🦅 [Birdeye](https://birdeye.so/token/{token}?chain=solana)\n"
            f"📊 [DexScreener](https://dexscreener.com/solana/{token})\n"
            f"🔥 [DexTools](https://www.dextools.io/app/en/solana/pair-explorer/{token})",
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
    
    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        portfolios[user_id] = {
            'balance': self.starting_balance,
            'positions': {},
            'history': []
        }
        await self.save_data()
        await update.message.reply_text("✅ Portfolio reset! Starting balance: 10 SOL")
    
    async def fund(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id not in portfolios:
            await update.message.reply_text("Use /start first to create a portfolio")
            return
        
        if not context.args:
            amount = 1.0  # Default 1 SOL
        else:
            try:
                amount = float(context.args[0])
            except:
                await update.message.reply_text("Usage: /fund <amount> (e.g., /fund 5)")
                return
        
        if amount > 20:
            await update.message.reply_text("❌ You can only fund up to 20 SOL at a time!")
            return
        
        portfolios[user_id]['balance'] += amount
        await self.save_data()
        
        await update.message.reply_text(
            f"✅ Added {amount:.4f} SOL to your account!\n"
            f"💰 New Balance: {portfolios[user_id]['balance']:.4f} SOL\n\n"
            f"💡 Remember: This is paper trading - no real money!"
        )
    
    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Open settings menu"""
        user_id = update.effective_user.id
        
        if user_id not in user_settings:
            user_settings[user_id] = {'slippage': 1.0}
            await self.save_data()
            
        settings = user_settings[user_id]
        slippage = settings.get('slippage', 1.0)
        
        msg = f"⚙️ *Settings*\n\n"
        msg += f"Adjust your trading parameters here.\n\n"
        msg += f"📉 *Slippage Tolerance:* {slippage}%\n"
        msg += "Controls the maximum price movement allowed during trade execution.\n"
        
        keyboard = [
            [
                InlineKeyboardButton("0.5%", callback_data="set_slip_0.5"),
                InlineKeyboardButton("1.0%", callback_data="set_slip_1.0"),
                InlineKeyboardButton("3.0%", callback_data="set_slip_3.0")
            ],
            [
                InlineKeyboardButton("5.0%", callback_data="set_slip_5.0"),
                InlineKeyboardButton("10.0%", callback_data="set_slip_10.0")
            ],
            [
                InlineKeyboardButton("🔄 Reset to Default", callback_data="set_reset")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)
            except BadRequest:
                pass  # Ignore "Message is not modified" error
        else:
            await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

    async def handle_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle settings callbacks"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        user_id = query.from_user.id
        
        if user_id not in user_settings:
            user_settings[user_id] = {'slippage': 1.0}
        
        if data == "set_reset":
            user_settings[user_id]['slippage'] = 1.0
            await query.answer("✅ Settings reset to default", show_alert=True)
        elif data.startswith("set_slip_"):
            try:
                value = float(data.split('_')[2])
                user_settings[user_id]['slippage'] = value
                await query.answer(f"✅ Slippage set to {value}%")
            except:
                pass
        
        await self.save_data()
        await self.settings_command(update, context)
        
    async def admin_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show internal analytics"""
        user_id = update.effective_user.id
        admin_id = os.getenv("ADMIN_USER_ID")
        
        # Security Check: Only allow the admin defined in .env
        if not admin_id or str(user_id) != str(admin_id):
            return  # Silent ignore (don't even reply to unauthorized users)
        
        total_users = len(user_stats)
        now = datetime.now()
        dau = 0
        mau = 0
        new_today = 0
        active_7d = 0
        
        top_commands = {}
        
        for u in user_stats.values():
            try:
                last = datetime.fromisoformat(u['last_active'])
                joined = datetime.fromisoformat(u['joined_at'])
                
                delta = now - last
                if delta.days < 1:
                    dau += 1
                if delta.days < 7:
                    active_7d += 1
                if delta.days < 30:
                    mau += 1
                
                if (now - joined).days < 1 and joined.date() == now.date():
                    new_today += 1
                
                for cmd, count in u.get('commands', {}).items():
                    top_commands[cmd] = top_commands.get(cmd, 0) + count
            except:
                continue
                
        # Sort commands
        sorted_cmds = sorted(top_commands.items(), key=lambda x: x[1], reverse=True)[:5]
        
        msg = "📊 *Bot Analytics (Admin)*\n\n"
        msg += f"👥 Total Users: {total_users}\n"
        msg += f"🆕 New Today: {new_today}\n\n"
        msg += f"🔥 *Activity:*\n"
        msg += f"• DAU (24h): {dau}\n"
        msg += f"• Weekly (7d): {active_7d}\n"
        msg += f"• MAU (30d): {mau}\n\n"
        msg += f"⌨️ *Top Commands:*\n"
        for cmd, count in sorted_cmds:
            msg += f"• /{cmd}: {count}\n"
            
        await update.message.reply_text(msg, parse_mode='Markdown')

def main():
    # Load environment variables
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env file")
        return
    
    bot = PaperTradingBot()
    app = Application.builder().token(token).build()
    
    # Add command handlers
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("info", bot.info_command))
    app.add_handler(CommandHandler("price", bot.get_price))
    app.add_handler(CommandHandler("buy", bot.buy))
    app.add_handler(CommandHandler("sell", bot.sell))
    app.add_handler(CommandHandler("portfolio", bot.portfolio))
    app.add_handler(CommandHandler("history", bot.history))
    app.add_handler(CommandHandler("fund", bot.fund))
    app.add_handler(CommandHandler("reset", bot.reset))
    app.add_handler(CommandHandler("watch", bot.watch_command))
    app.add_handler(CommandHandler("watchlist", bot.watchlist_command))
    app.add_handler(CommandHandler("stats", bot.stats_command))
    app.add_handler(CommandHandler("chart", bot.chart_command))
    app.add_handler(CommandHandler("leaderboard", bot.leaderboard))
    app.add_handler(CommandHandler("settings", bot.settings_command))
    app.add_handler(CommandHandler("admin", bot.admin_stats))
    
    # Handle callback buttons
    app.add_handler(CallbackQueryHandler(bot.quick_buy_callback, pattern="^(qbuy|watch|refresh)_"))
    app.add_handler(CallbackQueryHandler(bot.position_actions, pattern="^pos_"))
    app.add_handler(CallbackQueryHandler(bot.handle_position_actions, pattern="^(posrefresh|poschart|posbuy|possell)_"))
    app.add_handler(CallbackQueryHandler(bot.handle_position_actions, pattern="^back_portfolio$"))
    app.add_handler(CallbackQueryHandler(bot.handle_settings, pattern="^set_"))
    
    # Auto-detect pasted addresses
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_address_paste))
    
    print("🤖 Bot started successfully!")
    print("Go to Telegram and send /start to your bot")
    app.run_polling()

if __name__ == '__main__':
    main()
