from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict
import uuid
from datetime import datetime, timedelta
import json
import asyncio
import aiohttp
import time
import traceback
from collections import defaultdict
import random

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
api_router = APIRouter()

def custom_json_encoder(obj):
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

app.json_encoder = custom_json_encoder

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

mongo_url = os.environ.get('MONGO_URL')
if not mongo_url:
    raise RuntimeError("MONGO_URL is not set in environment")
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME', 'tokenwise_db')]

SOLANA_RPC_URL = os.environ.get('SOLANA_RPC_URL')
SOLANA_WS_URL = os.environ.get('SOLANA_WS_URL')
TOKEN_CONTRACT = os.environ.get('TOKEN_CONTRACT')

if not SOLANA_RPC_URL or not SOLANA_WS_URL:
    raise RuntimeError("SOLANA_RPC_URL and SOLANA_WS_URL must be set in environment (e.g., from Alchemy/QuickNode).")
if not TOKEN_CONTRACT:
    logger.warning("TOKEN_CONTRACT is not set in environment. Defaulting to a placeholder.")
    TOKEN_CONTRACT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"

PROTOCOL_PROGRAM_IDS = {
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium",
    "5quBtoiQqxF9Jv6KYKctB59NT3gtJD2Y65kdnB1Uev3h": "Raydium",
    "27haf8L6oxUeXrHrgEgsexjSY5hbVUWEmvv9Nyxg8vQv": "Raydium",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca",
    "DjVE6JNiYqPL2QXyCUUh8rNjHrbz9hXHNYt99MQ59qw1": "Orca",
    "SwaPpA9LAaLfeLi3a68M4DjnLqgtticKg6CnyNwgAC8": "Saber",
    "22Y43yTVxuUkoRKdm9thyRhQ3SdgQS7c7kB6UNCiaczD": "Serum",
    "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin": "Serum",
}

class TokenHolder(BaseModel):
    owner: str
    address: str
    balance: float
    ui_amount: float
    percentage: Optional[float] = None
    decimals: int = 0

class RealtimeTransaction(BaseModel):
    id: Optional[str] = Field(alias="_id", default_factory=lambda: str(ObjectId()))
    signature: str
    timestamp: datetime
    wallet: str
    token_address: str
    amount: float
    action_type: str
    protocol: str
    block_time: int
    slot: int
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    pre_balance: Optional[float] = None
    post_balance: Optional[float] = None

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str, datetime: lambda dt: dt.isoformat()}

class WalletTracker(BaseModel):
    id: Optional[str] = Field(alias="_id", default_factory=lambda: str(ObjectId()))
    address: str
    tracked_since: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True
    balance: Optional[float] = None
    token_amount: Optional[float] = None
    last_transaction: Optional[datetime] = None
    total_buys: int = 0
    total_sells: int = 0
    profit_loss: Optional[float] = None

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str, datetime: lambda dt: dt.isoformat()}

class TokenHolderSnapshot(BaseModel):
    id: Optional[str] = Field(alias="_id", default_factory=lambda: str(ObjectId()))
    token_address: str
    holders: List[TokenHolder]
    total_supply: float
    holder_count: int
    last_updated: datetime

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str, datetime: lambda dt: dt.isoformat()}

class WalletCreate(BaseModel):
    address: str

async def call_solana_rpc(method: str, params: list, timeout: int = 30, retries: int = 3, initial_delay: float = 5.0):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params
    }
    headers = {"Content-Type": "application/json"}
    
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(SOLANA_RPC_URL, json=payload, headers=headers) as response:
                    if response.status == 429:
                        delay = initial_delay * (2 ** attempt)
                        logger.warning(f"RPC {method} hit rate limit (429). Retrying in {delay:.2f} seconds (attempt {attempt + 1}/{retries})...")
                        await asyncio.sleep(delay)
                        continue
                    
                    response.raise_for_status()
                    result = await response.json()
                    if 'error' in result:
                        logger.error(f"RPC {method} failed: {result['error']}")
                        raise HTTPException(status_code=500, detail=f"RPC Error ({result['error'].get('code', 'N/A')}): {result['error'].get('message', 'Unknown RPC error')}")
                    return result['result']
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error during RPC call {method} (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                delay = initial_delay * (2 ** attempt)
                logger.info(f"Retrying in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
            else:
                raise HTTPException(status_code=503, detail=f"Failed to connect to Solana RPC after multiple retries: {e}")
        except asyncio.TimeoutError:
            logger.error(f"Timeout during RPC call {method} (attempt {attempt + 1}/{retries})")
            if attempt < retries - 1:
                delay = initial_delay * (2 ** attempt)
                logger.info(f"Retrying in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
            else:
                raise HTTPException(status_code=504, detail="Solana RPC call timed out after multiple retries.")
        except Exception as e:
            logger.error(f"An unexpected error occurred during RPC call {method} (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                delay = initial_delay * (2 ** attempt)
                logger.info(f"Retrying in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
            else:
                raise HTTPException(status_code=500, detail=f"An unexpected error occurred after multiple retries: {e}")
    
    raise HTTPException(status_code=500, detail=f"RPC call {method} failed after {retries} attempts.")


async def get_token_supply(token_address: str):
    try:
        mint_info = await call_solana_rpc("getAccountInfo", [
            token_address,
            {"encoding": "jsonParsed", "commitment": "confirmed"}
        ])
        
        if mint_info and mint_info.get("value") and mint_info["value"].get("data"):
            supply = mint_info["value"]["data"]["parsed"]["info"]["supply"]
            decimals = mint_info["value"]["data"]["parsed"]["info"]["decimals"]
            ui_supply = float(supply) / (10**decimals)
            return {"value": {"uiAmount": ui_supply, "decimals": decimals}}
        return None
    except Exception as e:
        logger.error(f"Error getting token supply: {e}", exc_info=True)
        return None

async def get_signatures_for_address(address: str, limit: int = 50):
    try:
        params = [address, {"limit": limit, "commitment": "confirmed"}]
        result = await call_solana_rpc("getSignaturesForAddress", params)
        return result or []
    except Exception as e:
        logger.error(f"Error getting signatures for {address}: {e}", exc_info=True)
        return None

async def get_transaction(signature: str):
    try:
        params = [signature, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        result = await call_solana_rpc("getTransaction", params, timeout=20)
        return result
    except Exception as e:
        logger.error(f"Error getting transaction {signature}: {e}", exc_info=True)
        return None

async def get_account_balance(address: str):
    try:
        result = await call_solana_rpc("getBalance", [address])
        return result
    except Exception as e:
        logger.error(f"Error getting SOL balance: {e}", exc_info=True)
        return 0

class WalletManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.tracked_wallets: Dict[str, Dict[str, Any]] = {}
        self.is_monitoring = False
        self.monitor_task = None
        self.last_discovery_run = None
        self.discovery_interval_seconds = 21600
        self.last_processed_slot: int = 0

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        client_id = str(uuid.uuid4())
        self.active_connections[client_id] = websocket
        logger.info(f"New WebSocket connection. Total: {len(self.active_connections)}")
        return client_id

    async def disconnect(self, websocket: WebSocket):
        client_id_to_remove = None
        for cid, ws in self.active_connections.items():
            if ws == websocket:
                client_id_to_remove = cid
                break
        if client_id_to_remove:
            del self.active_connections[client_id_to_remove]
        logger.info(f"WebSocket client disconnected. Total: {len(self.active_connections)}")
        if not self.active_connections and self.is_monitoring:
            await self.stop_monitoring()

    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except RuntimeError as e:
            logger.warning(f"Could not send to WebSocket (likely closed): {e}")
            await self.disconnect(websocket)

    async def broadcast(self, message: str):
        disconnected = []
        for websocket in list(self.active_connections.values()):
            try:
                await websocket.send_text(message)
            except RuntimeError as e:
                logger.warning(f"Could not send to WebSocket (likely closed): {e}")
                disconnected.append(websocket)
        for ws in disconnected:
            await self.disconnect(ws)
        if disconnected:
            logger.info(f"Disconnected {len(disconnected)} stale WebSocket clients.")

    async def start_monitoring(self):
        if not self.is_monitoring:
            self.is_monitoring = True
            logger.info("Starting wallet monitoring.")
            self.monitor_task = asyncio.create_task(self._monitor_wallets_periodically())

    async def stop_monitoring(self):
        if self.is_monitoring:
            self.is_monitoring = False
            if self.monitor_task:
                self.monitor_task.cancel()
                try:
                    await self.monitor_task
                except asyncio.CancelledError:
                    logger.info("Wallet monitoring stopped.")
            self.monitor_task = None

    async def _monitor_wallets_periodically(self):
        while self.is_monitoring:
            try:
                current_time = datetime.utcnow()
                if self.last_discovery_run is None or \
                   (current_time - self.last_discovery_run).total_seconds() >= self.discovery_interval_seconds:
                    logger.info("Initiating scheduled top wallet discovery.")
                    await self.discover_top_wallets(TOKEN_CONTRACT)
                    self.last_discovery_run = current_time

                if self.tracked_wallets:
                    await self._generate_and_broadcast_mock_transaction()
                
                await self.broadcast_dashboard_data()

            except Exception as e:
                logger.error(f"Error in periodic wallet monitoring: {e}\n{traceback.format_exc()}")
            finally:
                await asyncio.sleep(5)

    async def _generate_and_broadcast_mock_transaction(self):
        if not self.tracked_wallets:
            logger.warning("No tracked wallets available to generate mock transactions.")
            return

        wallet_address = random.choice(list(self.tracked_wallets.keys()))
        
        action_type = random.choice(["buy", "sell"])
        amount = round(random.uniform(10, 1000), 4)
        protocol = random.choice(list(PROTOCOL_PROGRAM_IDS.values()))
        
        signature = str(uuid.uuid4()).replace('-', '') + str(int(time.time()))
        
        mock_tx = RealtimeTransaction(
            signature=signature,
            timestamp=datetime.utcnow(),
            wallet=wallet_address,
            token_address=TOKEN_CONTRACT,
            amount=amount,
            action_type=action_type,
            protocol=protocol,
            block_time=int(time.time()),
            slot=random.randint(100000000, 200000000)
        )

        try:
            await db.realtime_transactions.insert_one(mock_tx.model_dump(by_alias=True))
            logger.info(f"Generated and saved mock transaction: {mock_tx.action_type} {mock_tx.amount} for {mock_tx.wallet[:8]}...")
            
            await self.broadcast(json.dumps({
                "type": "new_transaction",
                "data": mock_tx.model_dump(by_alias=True),
                "timestamp": datetime.utcnow().isoformat()
            }, default=custom_json_encoder))
        except Exception as e:
            logger.error(f"Error generating or saving mock transaction: {e}", exc_info=True)


    async def discover_top_wallets(self, mint_address: str, top_n: int = 100):
        logger.info(f"Discovering top {top_n} wallets for mint: {mint_address}")
        SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5mW"

        params = [
            SPL_TOKEN_PROGRAM_ID,
            {
                "encoding": "jsonParsed",
                "filters": [
                    {"memcmp": {"offset": 0, "bytes": mint_address}}
                ],
                "commitment": "confirmed"
            }
        ]

        try:
            accounts_data = await call_solana_rpc("getProgramAccounts", params)
            
            if not accounts_data:
                logger.warning(f"No token accounts found for mint {mint_address} from RPC. Relying on seeded data if available.")
                return

            supply_info = await get_token_supply(mint_address)
            token_decimals = 0
            if supply_info and supply_info.get("value"):
                token_decimals = int(supply_info["value"].get("decimals", 0))

            owner_balances = defaultdict(float)
            owner_token_accounts = {}

            for account in accounts_data:
                pubkey = account['pubkey']
                account_info = account['account']['data']['parsed']['info']
                owner = account_info['owner']
                token_amount_raw = int(account_info['tokenAmount']['amount'])
                
                ui_amount = float(token_amount_raw) / (10**token_decimals)

                if owner and ui_amount > 0:
                    owner_balances[owner] += ui_amount
                    if owner not in owner_token_accounts:
                        owner_token_accounts[owner] = pubkey

            aggregated_holders = []
            for owner, balance in owner_balances.items():
                if balance > 0:
                    aggregated_holders.append(TokenHolder(
                        owner=owner,
                        address=owner_token_accounts.get(owner, owner),
                        balance=balance,
                        ui_amount=balance,
                        decimals=token_decimals
                    ))
            
            sorted_holders = sorted(aggregated_holders, key=lambda x: x.balance, reverse=True)
            top_n_holders = sorted_holders[:top_n]

            holders_to_db = [h.model_dump(by_alias=True) for h in top_n_holders]

            current_total_supply = 0.0
            if supply_info and supply_info.get("value"):
                current_total_supply = supply_info["value"]["uiAmount"]

            snapshot = TokenHolderSnapshot(
                token_address=mint_address,
                holders=holders_to_db,
                total_supply=current_total_supply,
                holder_count=len(top_n_holders),
                last_updated=datetime.utcnow()
            )
            await db.token_holders.update_one(
                {"token_address": mint_address},
                {"$set": snapshot.model_dump(by_alias=True)},
                upsert=True
            )

            for holder_model in top_n_holders:
                wallet_address = holder_model.owner
                existing_wallet = await db.wallets.find_one({"address": wallet_address})
                if not existing_wallet:
                    wallet_tracker = WalletTracker(
                        address=wallet_address,
                        balance=holder_model.balance,
                        token_amount=holder_model.balance
                    )
                    await db.wallets.insert_one(wallet_tracker.model_dump(by_alias=True))
                    logger.info(f"📋 Auto-tracked wallet: {wallet_address[:8]}... with {holder_model.balance} tokens")
                else:
                    await db.wallets.update_one(
                        {"address": wallet_address},
                        {"$set": {"balance": holder_model.balance, "token_amount": holder_model.balance, "last_updated": datetime.utcnow()}}
                    )

            await self.load_tracked_wallets()
            logger.info(f"✅ Discovered and tracking {len(top_n_holders)} wallets using getProgramAccounts.")

        except HTTPException as e:
            logger.error(f"Failed to discover top wallets (HTTPException): {e.detail}. Relying on seeded data if available.")
        except Exception as e:
            logger.error(f"An unexpected error occurred during top wallet discovery: {e}\n{traceback.format_exc()}. Relying on seeded data if available.")

    async def load_tracked_wallets(self):
        try:
            wallets_data = await db.wallets.find({"active": True}).to_list(1000)
            self.tracked_wallets = {}
            for doc in wallets_data:
                if '_id' in doc:
                    doc['_id'] = str(doc['_id'])
                wallet_model = WalletTracker(**doc)
                self.tracked_wallets[wallet_model.address] = wallet_model.model_dump(by_alias=True)
            logger.info(f"📋 Loaded {len(self.tracked_wallets)} tracked wallets from DB.")
        except Exception as e:
            logger.error(f"Error loading tracked wallets: {e}", exc_info=True)

    async def broadcast_dashboard_data(self):
        try:
            top_holders_data = await db.token_holders.find_one({"token_address": TOKEN_CONTRACT})
            top_holders_list = []
            holder_count = 0
            if top_holders_data:
                if '_id' in top_holders_data:
                    top_holders_data['_id'] = str(top_holders_data['_id'])
                snapshot_model = TokenHolderSnapshot(**top_holders_data)
                top_holders_list = [h.model_dump(by_alias=True) for h in snapshot_model.holders[:10]]
                holder_count = snapshot_model.holder_count # Get actual seeded count

            recent_txns_data = await db.realtime_transactions.find().sort("timestamp", -1).limit(20).to_list(20)
            recent_txns_list = []
            for doc in recent_txns_data:
                if '_id' in doc:
                    doc['_id'] = str(doc['_id'])
                recent_txns_list.append(RealtimeTransaction(**doc).model_dump(by_alias=True))

            protocol_stats = await db.realtime_transactions.aggregate([
                {"$group": {"_id": "$protocol", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}}
            ]).to_list(10)
            
            active_wallets_raw = await db.realtime_transactions.aggregate([
                {"$group": {"_id": "$wallet", "tx_count": {"$sum": 1}}},
                {"$sort": {"tx_count": -1}},
                {"$limit": 10}
            ]).to_list(10)
            active_wallets_list = [{"wallet_address": w["_id"], "tx_count": w["tx_count"]} for w in active_wallets_raw]

            dashboard_data = {
                "type": "dashboard_update",
                "monitoring_active": self.is_monitoring,
                "connected_clients": len(self.active_connections),
                "tracked_wallets_count": len(self.tracked_wallets), # This should reflect count from load_tracked_wallets
                "top_holders": top_holders_list,
                "recent_transactions": recent_txns_list,
                "protocol_usage": protocol_stats,
                "most_active_wallets": active_wallets_list,
                "holder_count": holder_count,
                "timestamp": datetime.utcnow().isoformat()
            }
            await self.broadcast(json.dumps(dashboard_data, default=custom_json_encoder))
            logger.info("Dashboard data broadcasted.")
        except Exception as e:
            logger.error(f"Error broadcasting dashboard data: {e}\n{traceback.format_exc()}")


manager = WalletManager()

@app.on_event("startup")
async def startup_event():
    logger.info("Application starting up...")
    await manager.load_tracked_wallets() # this will load our tracked wallets 
    await manager.start_monitoring()

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Application shutting down...")
    await manager.stop_monitoring()
    client.close()
    logger.info("MongoDB connection closed.")


@api_router.get("/status")
async def get_status():
    return {
        "status": "online",
        "monitoring_active": manager.is_monitoring,
        "connected_clients": len(manager.active_connections),
        "tracked_wallets": len(manager.tracked_wallets),
        "last_discovery_run": manager.last_discovery_run.isoformat() if manager.last_discovery_run else "N/A"
    }

@api_router.get("/token-holders/{mint_address}")
async def get_token_holders(mint_address: str):
    try:
        logger.info(f"Attempting to fetch token holders for mint: {mint_address} from MongoDB.")
        holders_data = await db.token_holders.find_one({"token_address": mint_address})
        
        if holders_data:
            logger.info(f"Found token holders data in DB for {mint_address}. Data keys: {holders_data.keys()}")
            # Ensure _id is converted to string for existing docs
            if '_id' in holders_data:
                holders_data['_id'] = str(holders_data['_id'])
            
            # Ensure 'holders' key exists and is iterable
            if 'holders' not in holders_data or not isinstance(holders_data['holders'], list):
                logger.error(f"Token holders data for {mint_address} is missing 'holders' array or it's not a list.")
                raise HTTPException(status_code=500, detail="Corrupted token holders data in DB.")

            snapshot_model = TokenHolderSnapshot(**holders_data)
            # Return the list of holders directly
            return [h.model_dump(by_alias=True) for h in snapshot_model.holders]
        else:
            logger.warning(f"No token holders snapshot found in DB for mint: {mint_address}.")
            raise HTTPException(status_code=404, detail="Token holders snapshot not found for this mint.")
    except Exception as e:
        logger.error(f"Error getting token holders from DB for {mint_address}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve token holders.")

@api_router.get("/wallets/{wallet_address}/transactions")
async def get_wallet_transactions(wallet_address: str, limit: int = 20):
    try:
        tx_data = await db.realtime_transactions.find({"wallet": wallet_address}).sort("timestamp", -1).limit(limit).to_list(limit)
        transactions = []
        for doc in tx_data:
            if '_id' in doc:
                doc['_id'] = str(doc['_id'])
            transactions.append(RealtimeTransaction(**doc).model_dump(by_alias=True))
        
        protocol_stats_wallet = await db.realtime_transactions.aggregate([
            {"$match": {"wallet": wallet_address}},
            {"$group": {"_id": "$protocol", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]).to_list(10)

        return {
            "wallet_address": wallet_address,
            "transactions": transactions,
            "protocol_usage": {p["_id"]: p["count"] for p in protocol_stats_wallet}
        }
    except Exception as e:
        logger.error(f"Error fetching transactions for wallet {wallet_address}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve wallet transactions.")


@api_router.get("/analytics/dashboard")
async def get_dashboard_data():
    try:
        total_wallets = await db.wallets.count_documents({"active": True})
        total_tx = await db.realtime_transactions.count_documents({})
        buy_count = await db.realtime_transactions.count_documents({"action_type": "buy"})
        sell_count = await db.realtime_transactions.count_documents({"action_type": "sell"})
        
        recent_tx_data = await db.realtime_transactions.find().sort("timestamp", -1).limit(20).to_list(20)
        recent_tx = []
        for doc in recent_tx_data:
            if '_id' in doc:
                doc['_id'] = str(doc['_id'])
            recent_tx.append(RealtimeTransaction(**doc).model_dump(by_alias=True))

        protocol_stats = await db.realtime_transactions.aggregate([
            {"$group": {"_id": "$protocol", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]).to_list(10)
        
        active_wallets_raw = await db.realtime_transactions.aggregate([
            {"$group": {"_id": "$wallet", "tx_count": {"$sum": 1}}},
            {"$sort": {"tx_count": -1}},
            {"$limit": 10}
        ]).to_list(10)
        active_wallets = [{"wallet_address": w["_id"], "tx_count": w["tx_count"]} for w in active_wallets_raw]
        
        holders_data_raw = await db.token_holders.find_one({"token_address": TOKEN_CONTRACT})
        top_holders = []
        holder_count = 0
        if holders_data_raw:
            if '_id' in holders_data_raw:
                holders_data_raw['_id'] = str(holders_data_raw['_id'])
            snapshot_model = TokenHolderSnapshot(**holders_data_raw)
            top_holders = [h.model_dump(by_alias=True) for h in snapshot_model.holders[:10]]
            holder_count = snapshot_model.holder_count

        return {
            "total_wallets": total_wallets,
            "total_transactions": total_tx,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "buy_sell_ratio": round(buy_count / max(sell_count, 1), 2),
            "recent_transactions": recent_tx,
            "protocol_usage": protocol_stats,
            "most_active_wallets": active_wallets,
            "top_token_holders": top_holders,
            "monitoring_active": manager.is_monitoring,
            "connected_clients": len(manager.active_connections),
            "holder_count": holder_count,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        traceback.print_exc()
        logger.error(f"Error in /analytics/dashboard: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error – check server log")
    
@api_router.get("/analytics/protocols")
async def get_protocol_analytics():
    try:
        protocol_stats = await db.realtime_transactions.aggregate([
            {"$group": {"_id": "$protocol", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]).to_list(20)
        
        yesterday = datetime.utcnow() - timedelta(days=1)
        hourly_stats = await db.realtime_transactions.aggregate([
            {"$match": {"timestamp": {"$gte": yesterday}}},
            {"$group": {
                "_id": {
                    "protocol": "$protocol",
                    "hour": {"$dateToString": {"format": "%Y-%m-%d %H:00", "date": "$timestamp"}}
                },
                "count": {"$sum": 1}
            }},
            {"$sort": {"_id.hour": 1}}
        ]).to_list(1000)
        return {"protocol_stats": protocol_stats, "hourly_breakdown": hourly_stats, "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        logger.error(f"Error getting protocol analytics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/analytics/volume")
async def get_volume_analytics():
    try:
        yesterday = datetime.utcnow() - timedelta(days=1)
        volume_stats = await db.realtime_transactions.aggregate([
            {"$match": {"timestamp": {"$gte": yesterday}}},
            {"$group": {
                "_id": None,
                "total_volume": {"$sum": "$amount"},
                "buy_volume": {"$sum": {"$cond": [{"$eq": ["$action_type", "buy"]}, "$amount", 0]}},
                "sell_volume": {"$sum": {"$cond": [{"$eq": ["$action_type", "sell"]}, "$amount", 0]}},
                "transaction_count": {"$sum": 1}
            }}
        ]).to_list(1)
        hourly_volume = await db.realtime_transactions.aggregate([
            {"$match": {"timestamp": {"$gte": yesterday}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d %H:00", "date": "$timestamp"}},
                "volume": {"$sum": "$amount"},
                "transactions": {"$sum": 1},
                "buy_volume": {"$sum": {"$cond": [{"$eq": ["$action_type", "buy"]}, "$amount", 0]}},
                "sell_volume": {"$sum": {"$cond": [{"$eq": ["$action_type", "sell"]}, "$amount", 0]}}
            }},
            {"$sort": {"_id": 1}}
        ]).to_list(24)
        top_volume_wallets = await db.realtime_transactions.aggregate([
            {"$match": {"timestamp": {"$gte": yesterday}}},
            {"$group": {
                "_id": "$wallet",
                "total_volume": {"$sum": "$amount"},
                "transaction_count": {"$sum": 1}
            }},
            {"$sort": {"total_volume": -1}},
            {"$limit": 20}
        ]).to_list(20)
        volume_data = volume_stats[0] if volume_stats else {"total_volume": 0, "buy_volume": 0, "sell_volume": 0, "transaction_count": 0}
        return {
            "volume_24h": volume_data,
            "hourly_breakdown": hourly_volume,
            "top_volume_wallets": top_volume_wallets,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting volume analytics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/realtime/status")
async def get_realtime_status():
    try:
        last_hour = datetime.utcnow() - timedelta(hours=1)
        recent_tx_count = await db.realtime_transactions.count_documents({"timestamp": {"$gte": last_hour}})
        return {
            "monitoring_active": manager.is_monitoring,
            "connected_clients": len(manager.active_connections),
            "tracked_wallets": len(manager.tracked_wallets),
            "monitored_token": TOKEN_CONTRACT,
            "recent_transactions_1h": recent_tx_count,
            "last_processed_slot": manager.last_processed_slot,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting real-time status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/ws/transactions")
async def websocket_endpoint(websocket: WebSocket):
    client_id = await manager.connect(websocket)
    try:
        await manager.send_personal_message(json.dumps({
            "type": "connection_established",
            "message": "Connected to TokenWise real-time feed",
            "monitoring_token": TOKEN_CONTRACT,
            "tracked_wallets": len(manager.tracked_wallets),
            "timestamp": datetime.utcnow().isoformat()
        }), websocket)
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if message:
                    data = json.loads(message)
                    cmd = data.get("command")
                    if cmd == "ping":
                        await manager.send_personal_message(json.dumps({"type": "pong", "timestamp": datetime.utcnow().isoformat()}), websocket)
                    elif cmd == "get_status":
                        await manager.send_personal_message(json.dumps({
                            "type": "status",
                            "monitoring_active": manager.is_monitoring,
                            "connected_clients": len(manager.active_connections),
                            "tracked_wallets": len(manager.tracked_wallets),
                            "timestamp": datetime.utcnow().isoformat()
                        }), websocket)
                    elif cmd == "get_recent_transactions":
                        limit = data.get("limit", 10)
                        recent_data = await db.realtime_transactions.find().sort("timestamp", -1).limit(limit).to_list(limit)
                        recent_txns = []
                        for doc in recent_data:
                            if '_id' in doc:
                                doc['_id'] = str(doc['_id'])
                            recent_txns.append(RealtimeTransaction(**doc).model_dump(by_alias=True))
                        await manager.send_personal_message(json.dumps({
                            "type": "recent_transactions",
                            "transactions": recent_txns,
                            "timestamp": datetime.utcnow().isoformat()
                        }, default=custom_json_encoder), websocket)
            except asyncio.TimeoutError:
                await manager.send_personal_message(json.dumps({"type": "keepalive", "timestamp": datetime.utcnow().isoformat()}), websocket)
    except WebSocketDisconnect:
        logger.info(f"WebSocket client {client_id} disconnected normally.")
    except Exception as e:
        logger.error(f"WebSocket error for client {client_id}: {e}", exc_info=True)
    finally:
        await manager.disconnect(websocket)


app.include_router(api_router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)