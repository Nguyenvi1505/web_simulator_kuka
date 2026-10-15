import os
import sys
import socket
import json
import uuid
import uvicorn
import asyncio
import logging
import hashlib
import datetime
import time
import requests
from logging.handlers import TimedRotatingFileHandler
from contextlib import asynccontextmanager
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer
from pyModbusTCP.client import ModbusClient

# ==========================================
# 1. CẤU HÌNH LOG (LỌC DỮ LIỆU & TỰ ĐỘNG XOÁ SAU 10 NGÀY)
# ==========================================
if not os.path.exists("logs"):
    os.makedirs("logs")

log_filepath = os.path.join("logs", "kuka_scada.log")

class SmartDataChangeFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.tx_to_channel = {}
        self.last_seen = {}

    def filter(self, record):
        if record.levelno >= logging.WARNING: return True
        if record.name == "WebSimulator": return True
        
        if "pymodbus" in record.name:
            msg = record.getMessage()
            if " old_data:" in msg:
                msg = msg.split(" old_data:")[0]
                record.msg = msg 

            if "Connected" in msg or "Server listening" in msg: return True

            if msg.startswith("recv:") or msg.startswith("send:"):
                parts = msg.split()
                if len(parts) >= 9:
                    tx_id = parts[1] + "-" + parts[2]
                    payload = " ".join(parts[3:]) 
                    
                    if msg.startswith("recv:"):
                        channel = "REQ:" + payload
                        self.tx_to_channel[tx_id] = channel
                        if len(self.tx_to_channel) > 100: self.tx_to_channel.clear()
                        
                        if self.last_seen.get(channel) == payload: return False
                        self.last_seen[channel] = payload
                        return True
                        
                    elif msg.startswith("send:"):
                        channel = self.tx_to_channel.get(tx_id, "UNKNOWN")
                        if tx_id in self.tx_to_channel:
                            del self.tx_to_channel[tx_id]
                            
                        resp_key = "RESP:" + channel
                        if self.last_seen.get(resp_key) == payload: return False
                            
                        self.last_seen[resp_key] = payload
                        return True
            return False
        return False

log_formatter = logging.Formatter("%(asctime)s | [%(levelname)s] | %(filename)s:%(lineno)d | %(name)s: %(message)s")

file_handler = TimedRotatingFileHandler(log_filepath, when="midnight", interval=1, backupCount=10, encoding="utf-8")
file_handler.suffix = "%Y%m%d"
file_handler.setFormatter(log_formatter)
file_handler.addFilter(SmartDataChangeFilter())

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)
console_handler.addFilter(SmartDataChangeFilter())

app_logger = logging.getLogger("WebSimulator")
app_logger.setLevel(logging.DEBUG)
app_logger.addHandler(file_handler)
app_logger.addHandler(console_handler)
app_logger.propagate = False

logging.getLogger("uvicorn.error").addHandler(file_handler)
logging.getLogger("uvicorn.access").addHandler(file_handler)

# ==========================================
# 2. QUẢN LÝ CẤU HÌNH JSON (CÓ BẢO MẬT BẰNG HASH)
# ==========================================
CONFIG_FILE = "config.json"

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

DEFAULT_SYS = {
    "admin_pass_hash": "8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92",
    "kuka_base_url": "http://192.168.221.250:5000/interfaces/api/amr",
    "kuka_token": "36b8ac34198ea45463f77f469fd9fe5d27d310ff66b84635df13dd9a1b444117",
    "server_port": 8000,
    "modbus_port": 502
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                if isinstance(data, list):
                    new_data = {"system": DEFAULT_SYS, "stations": data}
                    save_config(new_data)
                    return new_data
                
                sys_cfg = data.get("system", {})
                if "admin_pass" in sys_cfg:
                    raw_pass = sys_cfg.pop("admin_pass")
                    sys_cfg["admin_pass_hash"] = hash_password(raw_pass)
                    data["system"] = sys_cfg
                    save_config(data)
                    app_logger.info("Đã tự động mã hóa mật khẩu trong config.json")
                    
                return data
        except Exception as e:
            app_logger.error(f"Lỗi đọc config.json: {e}", exc_info=True)
            
    return {"system": DEFAULT_SYS, "stations": []}

def save_config(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def get_sys_config():
    return load_config().get("system", DEFAULT_SYS)

def get_kuka_settings():
    sys_cfg = get_sys_config()
    base_url = sys_cfg.get("kuka_base_url", DEFAULT_SYS["kuka_base_url"])
    token = sys_cfg.get("kuka_token", DEFAULT_SYS["kuka_token"])
    headers = {"Content-Type": "application/json", "Authorization": token}
    return base_url, headers

# ==========================================
# 3. KUKA API & MASTER LOOP (WATCHDOG)
# ==========================================
robot_status_flags = {} 
robot_info_cache = {}   
robot_paused_state = {}
robot_mission_cache = {} 
cancel_lamp_hold_until = {}

def get_robot_info(robot_id):
    kuka_base_url, kuka_headers = get_kuka_settings()
    try:
        url = f"{kuka_base_url}/robotQuery"
        res = requests.post(url, headers=kuka_headers, json={"robotId": robot_id}, timeout=2.0)
        if res.status_code == 200 and res.json().get("success"):
            data = res.json().get("data", [])
            if data: 
                return {
                    "status": data[0].get("status"),
                    "occupyStatus": data[0].get("occupyStatus", 0),
                    "nodeCode": data[0].get("nodeCode", ""), 
                    "battery": data[0].get("batteryLevel", 0.0)
                }
    except Exception as e:
        app_logger.error(f"API Error (Get Info - Xe {robot_id}): {e}", exc_info=True)
    return None

def control_kuka_robot(action, robot_id):
    kuka_base_url, kuka_headers = get_kuka_settings()
    url = f"{kuka_base_url}/pauseMission" if action == "PAUSE" else f"{kuka_base_url}/recoverMission"
    try:
        res = requests.post(url, headers=kuka_headers, json={"robotId": robot_id}, timeout=5.0)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"): return True, "OK"
            return False, data.get("message", "Unknown Error")
        return False, f"HTTP {res.status_code}"
    except Exception as e:
        return False, str(e)

def dispatch_workflow_mission(template_code, robot_id=None):
    kuka_base_url, kuka_headers = get_kuka_settings()
    url = f"{kuka_base_url}/submitMission"
    req_id = str(uuid.uuid4()).replace("-", "")[:32]
    mis_code = f"MIS_{str(uuid.uuid4()).replace('-', '')[:10]}"

    payload = {
        "orgId": "UNIVERSAL",
        "requestId": req_id,
        "missionCode": mis_code,
        "missionType": "MOVE",
        "templateCode": str(template_code)
    }

    if robot_id:
        payload["robotIds"] = [str(robot_id)]

    try:
        res = requests.post(url, headers=kuka_headers, json=payload, timeout=5.0)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"): return True, "OK", mis_code
            return False, data.get("message", "Unknown Error"), None
        return False, f"HTTP {res.status_code}", None
    except Exception as e:
        return False, str(e), None

def cancel_workflow_mission(mission_code):
    kuka_base_url, kuka_headers = get_kuka_settings()
    url = f"{kuka_base_url}/missionCancel"
    req_id = str(uuid.uuid4()).replace("-", "")[:32]
    
    payload = {
        "requestId": req_id,
        "missionCode": mission_code,
        "cancelMode": "FORCE"
    }
    
    try:
        res = requests.post(url, headers=kuka_headers, json=payload, timeout=5.0)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"): return True, "OK"
            return False, data.get("message", "Unknown Error")
        return False, f"HTTP {res.status_code}"
    except Exception as e:
        return False, str(e)

async def kuka_master_loop():
    app_logger.info("=== KUKA MASTER LOOP ĐÃ KHỞI ĐỘNG ===")
    while True:
        try:
            stations = load_config().get("stations", [])
            robot_ids = set([st["group"] for st in stations if st.get("type") in ["pause", "recover", "start", "cancel"] and st.get("group")])
            
            for rid in robot_ids:
                info = await asyncio.to_thread(get_robot_info, rid)
                if info:
                    status = info["status"]
                    occupy = info["occupyStatus"]
                    current_node = info.get("nodeCode", "")
                    
                    if status == 3 and occupy == 0:
                        robot_paused_state[rid] = False
                        robot_mission_cache.pop(rid, None) 
                        
                    info["is_paused"] = robot_paused_state.get(rid, False)
                    robot_info_cache[rid] = info 
                    
                    current_flag = robot_status_flags.get(rid, False)
                    
                    if status == 4:
                        if not current_flag:
                            app_logger.info(f"Robot {rid} is EXECUTING (Status 4). Enabling Pause/Recover.")
                            robot_status_flags[rid] = True
                    else:
                        if current_flag:
                            app_logger.info(f"Robot {rid} status changed to {status}. Disabling Pause/Recover.")
                            robot_status_flags[rid] = False
                            
                    # LOGIC ĐÈN NÚT START
                    start_stations = [s for s in stations if s.get("group") == rid and s.get("type") == "start"]
                    for st in start_stations:
                        allowed_nodes_str = st.get("nodes", "")
                        allowed_nodes = [n.strip() for n in allowed_nodes_str.split(",") if n.strip()]
                        is_at_valid_node = True if not allowed_nodes else any(node in current_node for node in allowed_nodes)
                        
                        if status == 3 and occupy == 0 and is_at_valid_node:
                            set_lamp_state(st, 0)
                        else:
                            set_lamp_state(st, 1)
                            
                    # LOGIC ĐÈN NÚT CANCEL 
                    cancel_stations = [s for s in stations if s.get("group") == rid and s.get("type") == "cancel"]
                    for st in cancel_stations:
                        if time.time() < cancel_lamp_hold_until.get(rid, 0):
                            set_lamp_state(st, 1)
                        else:
                            has_mission = robot_mission_cache.get(rid) is not None
                            if (status == 4 or occupy == 1) and has_mission:
                                set_lamp_state(st, 1)
                            else:
                                set_lamp_state(st, 0)
                                
                else:
                    # BẮT BUỘC BÁO TRẠNG THÁI OFFLINE (Mất kết nối) VỀ WEB
                    robot_info_cache[rid] = {
                        "status": 0, 
                        "occupyStatus": 0, 
                        "nodeCode": "", 
                        "battery": 0, 
                        "is_paused": False
                    }
                    # ĐẢM BẢO TẮT TOÀN BỘ ĐÈN CỦA XE KHI MẤT KẾT NỐI
                    offline_stations = [s for s in stations if s.get("group") == rid]
                    for st in offline_stations:
                        set_lamp_state(st, 0)

            if robot_info_cache:
                msg = {"kuka_data": robot_info_cache}
                for ws in list(active_websockets):
                    try: await ws.send_json(msg)
                    except Exception: active_websockets.discard(ws)
                    
        except Exception as e:
            app_logger.error(f"Master loop crashed: {e}", exc_info=True)
            
        await asyncio.sleep(2.0)

# ==========================================
# 4. GIAO TIẾP VỚI CÁC NÚT VẬT LÝ (PYMODBUSTCP)
# ==========================================
physical_clients = {}

def get_physical_client(ip):
    if ip not in physical_clients:
        physical_clients[ip] = ModbusClient(host=ip, port=502, auto_open=True, timeout=0.5)
    return physical_clients[ip]

def set_lamp_state(station, state):
    context[0].setValues(3, station["lamp_addr"], [state])
    
    ip = station.get("ip")
    phy_lamp = station.get("phy_lamp") 
    
    if ip and phy_lamp is not None:
        try:
            client = get_physical_client(ip)
            client.write_single_register(phy_lamp, state)
        except Exception as e:
            app_logger.error(f"Lỗi điều khiển đèn vật lý {ip}: {e}", exc_info=True)

async def scan_single_node(st):
    ip = st.get("ip")
    web_btn_addr = st.get("btn_addr")
    btn_type = st.get("type", "")
    phy_btn = st.get("phy_btn") 
    
    if not (ip and web_btn_addr and phy_btn is not None and btn_type in ["pause", "recover"]):
        return

    client = get_physical_client(ip)
    try:
        regs = await asyncio.to_thread(client.read_holding_registers, phy_btn, 1)
        
        if regs and regs[0] > 0:
            app_logger.info(f"Phát hiện nhấn nút {btn_type.upper()} vật lý tại {ip} (Reg {phy_btn})")
            await asyncio.to_thread(client.write_single_register, phy_btn, 0)
            
            sys_cfg = get_sys_config()
            server_port = sys_cfg.get("server_port", 8000)
            url = f"http://127.0.0.1:{server_port}/api/write"
            
            await asyncio.to_thread(requests.post, url, json={"address": web_btn_addr, "value": 1})
            await asyncio.sleep(0.5)
            await asyncio.to_thread(requests.post, url, json={"address": web_btn_addr, "value": 0})
            
    except Exception:
        pass

async def physical_button_scanner():
    app_logger.info("=== PHYSICAL BUTTON SCANNER ĐÃ KHỞI ĐỘNG (SONG SONG) ===")
    while True:
        try:
            stations = load_config().get("stations", [])
            tasks = [scan_single_node(st) for st in stations]
            
            if tasks:
                await asyncio.gather(*tasks)
                
        except Exception as e:
            app_logger.error(f"Lỗi trong vòng lặp scanner: {e}", exc_info=True)
            
        await asyncio.sleep(0.3)

# ==========================================
# 5. KHỞI TẠO BỘ NHỚ WEB MODBUS & SERVER FASTAPI
# ==========================================
store = ModbusSlaveContext(
    di=ModbusSequentialDataBlock(0, [0] * 100),
    co=ModbusSequentialDataBlock(0, [0] * 100),
    hr=ModbusSequentialDataBlock(0, [0] * 100), 
    ir=ModbusSequentialDataBlock(0, [0] * 100),
)
context = ModbusServerContext(slaves=store, single=True)
active_websockets = set()

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception: return "127.0.0.1"

def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'): return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

async def monitor_modbus_state():
    last_regs = None
    while True:
        try:
            regs = context[0].getValues(3, 1, count=100)
            if regs != last_regs:
                last_regs = regs
                msg = {"regs": regs}
                for ws in list(active_websockets):
                    try: await ws.send_json(msg)
                    except Exception: active_websockets.discard(ws)
        except Exception:
            pass
        await asyncio.sleep(0.05)

@asynccontextmanager
async def lifespan(app: FastAPI):
    sys_cfg = get_sys_config()
    modbus_port = sys_cfg.get("modbus_port", 502)
    modbus_task = asyncio.create_task(StartAsyncTcpServer(context=context, address=("0.0.0.0", modbus_port)))
    
    asyncio.create_task(monitor_modbus_state())
    asyncio.create_task(kuka_master_loop()) 
    asyncio.create_task(physical_button_scanner())
    yield
    modbus_task.cancel()

app = FastAPI(lifespan=lifespan)
app.mount("/styles", StaticFiles(directory=get_resource_path("styles")), name="styles")
app.mount("/scripts", StaticFiles(directory=get_resource_path("scripts")), name="scripts")

@app.get("/")
async def index():
    return FileResponse(get_resource_path("templates/index.html"))

# ==========================================
# 6. API QUẢN LÝ CẤU HÌNH (CÓ PASS BẢO MẬT & TRUY VẾT IP)
# ==========================================
@app.get("/api/config")
async def get_config():
    return load_config().get("stations", [])

@app.post("/api/config")
async def add_config(payload: dict, request: Request):
    sys_cfg = get_sys_config()
    input_pass = payload.pop("password", "")
    client_ip = request.client.host if request.client else "Unknown"
    
    if hash_password(input_pass) != sys_cfg.get("admin_pass_hash"): 
        return {"status": "error", "message": "Sai mật khẩu! Từ chối truy cập."}
        
    cfg = load_config()
    cfg.setdefault("stations", []).append(payload)
    save_config(cfg)
    app_logger.info(f"[IP: {client_ip}] Đã TẠO NÚT MỚI: {payload['name']}")
    return {"status": "success"}

@app.put("/api/config/{old_addr}")
async def edit_config(old_addr: int, payload: dict, request: Request):
    sys_cfg = get_sys_config()
    input_pass = payload.pop("password", "")
    client_ip = request.client.host if request.client else "Unknown"
    
    if hash_password(input_pass) != sys_cfg.get("admin_pass_hash"): 
        return {"status": "error", "message": "Sai mật khẩu! Từ chối truy cập."}
        
    cfg = load_config()
    stations = cfg.get("stations", [])
    for i, st in enumerate(stations):
        if st["btn_addr"] == old_addr:
            stations[i] = payload
            break
            
    cfg["stations"] = stations
    save_config(cfg)
    app_logger.info(f"[IP: {client_ip}] Đã SỬA NÚT (Địa chỉ cũ: {old_addr}) -> {payload['name']}")
    return {"status": "success"}

@app.delete("/api/config/{btn_addr}")
async def delete_config(btn_addr: int, request: Request, password: str = ""):
    sys_cfg = get_sys_config()
    client_ip = request.client.host if request.client else "Unknown"
    
    if hash_password(password) != sys_cfg.get("admin_pass_hash"): 
        return {"status": "error", "message": "Sai mật khẩu! Từ chối xóa NÚT."}
        
    cfg = load_config()
    cfg["stations"] = [c for c in cfg.get("stations", []) if c["btn_addr"] != btn_addr]
    save_config(cfg)
    app_logger.info(f"[IP: {client_ip}] Đã XÓA CẤU HÌNH NÚT có địa chỉ Nút: {btn_addr}")
    return {"status": "success"}

# ==========================================
# 7. API TRUNG TÂM & CHUẨN HÓA LỆNH KUKA (CÓ TRUY VẾT IP)
# ==========================================
@app.post("/api/write")
async def write_register(payload: dict, request: Request):
    try:
        client_ip = request.client.host if request.client else "Unknown"
        addr = int(payload.get("address", 1)) 
        val = int(payload.get("value", 0))
        
        if 1 <= addr <= 100:
            context[0].setValues(3, addr, [val]) 
            app_logger.info(f"[IP: {client_ip}] Ra lệnh GHI Modbus -> Địa chỉ {addr}: Giá trị {val}")

            stations = load_config().get("stations", [])

            if 51 <= addr <= 100:
                st_lamp = next((s for s in stations if s.get("lamp_addr") == addr), None)
                if st_lamp and st_lamp.get("ip") and st_lamp.get("phy_lamp") is not None:
                    try:
                        client = get_physical_client(st_lamp["ip"])
                        client.write_single_register(st_lamp["phy_lamp"], val)
                    except Exception: 
                        pass
                    
            if val > 0:
                station = next((s for s in stations if s.get("btn_addr") == addr), None)
                
                if station and station.get("type") in ["pause", "recover", "start", "cancel"]:
                    robot_id = station.get("group", "")
                    btn_type = station["type"]
                    
                    cached_info = robot_info_cache.get(robot_id)
                    if not cached_info:
                        msg = f"Mất kết nối API Kuka với Robot {robot_id}."
                        app_logger.warning(f"[IP: {client_ip}] TỪ CHỐI LỆNH: {msg}")
                        return {"status": "error", "message": msg}
                        
                    status = cached_info.get("status")
                    occupy = cached_info.get("occupyStatus")
                    current_node = cached_info.get("nodeCode", "")
                    
                    # --- 1. LOGIC NÚT START ---
                    if btn_type == "start":
                        allowed_nodes_str = station.get("nodes", "")
                        allowed_nodes = [n.strip() for n in allowed_nodes_str.split(",") if n.strip()]
                        is_at_valid_node = True if not allowed_nodes else any(node in current_node for node in allowed_nodes)
                        
                        if status == 3 and occupy == 0 and is_at_valid_node:
                            wf_code = station.get("wf")
                            app_logger.info(f"[IP: {client_ip}] ACTION TRIGGERED: START Workflow '{wf_code}' cho Robot {robot_id}")
                            is_success, msg, mis_code = await asyncio.to_thread(dispatch_workflow_mission, wf_code, robot_id)
                            
                            if is_success:
                                app_logger.info(f"[KUKA API] SUCCESS: Đã Start Robot {robot_id}.")
                                robot_mission_cache[robot_id] = mis_code
                                cached_info["status"] = 4 
                                cached_info["occupyStatus"] = 1
                                set_lamp_state(station, 1)
                                robot_status_flags[robot_id] = True 
                            else:
                                err = f"KUKA API báo lỗi: {msg}"
                                app_logger.warning(f"[IP: {client_ip}] FAILED (KUKA): {err}")
                                return {"status": "error", "message": err}
                        else:
                            if not is_at_valid_node:
                                err = f"Robot đang đứng sai vị trí. Yêu cầu có mặt tại Node: {station.get('nodes')}."
                            else:
                                err = f"Robot không rảnh. (Status: {status})"
                            app_logger.warning(f"[IP: {client_ip}] TỪ CHỐI LỆNH START: {err}")
                            return {"status": "error", "message": err}
                            
                    # --- 2. LOGIC NÚT CANCEL ---
                    elif btn_type == "cancel":
                        mis_code = robot_mission_cache.get(robot_id)
                        if mis_code and (status == 4 or occupy == 1):
                            app_logger.info(f"[IP: {client_ip}] ACTION TRIGGERED: CANCEL Mission '{mis_code}' cho Robot {robot_id}")
                            is_success, msg = await asyncio.to_thread(cancel_workflow_mission, mis_code)
                            
                            if is_success:
                                app_logger.info(f"[KUKA API] SUCCESS: Đã HỦY nhiệm vụ của Robot {robot_id}.")
                                robot_mission_cache.pop(robot_id, None) 
                                cancel_lamp_hold_until[robot_id] = time.time() + 3.0
                                set_lamp_state(station, 1) 
                                
                                cached_info["status"] = 3
                                cached_info["occupyStatus"] = 0
                                robot_status_flags[robot_id] = False
                                
                                if robot_info_cache:
                                    ws_msg = {"kuka_data": robot_info_cache}
                                    for ws in list(active_websockets):
                                        try: await ws.send_json(ws_msg)
                                        except Exception: active_websockets.discard(ws)
                            else:
                                err = f"KUKA API báo lỗi: {msg}"
                                app_logger.warning(f"[IP: {client_ip}] FAILED (KUKA): {err}")
                                return {"status": "error", "message": err}
                        else:
                            err = "Robot hiện không có Mission nào để hủy!"
                            app_logger.warning(f"[IP: {client_ip}] TỪ CHỐI LỆNH HỦY: {err}")
                            return {"status": "error", "message": err}
                            
                    # --- 3. LOGIC NÚT PAUSE / RECOVER ---
                    else:
                        action = "PAUSE" if btn_type == "pause" else "RECOVER"
                        if robot_status_flags.get(robot_id, False):
                            app_logger.info(f"[IP: {client_ip}] ACTION TRIGGERED: {action} cho Robot {robot_id}")
                            
                            if action == "RECOVER":
                                set_lamp_state(station, 1)
                                
                            is_success, msg = await asyncio.to_thread(control_kuka_robot, action, robot_id)
                            
                            if is_success:
                                if action == "PAUSE":
                                    set_lamp_state(station, 1)
                                    robot_paused_state[robot_id] = True 
                                    cached_info["is_paused"] = True
                                elif action == "RECOVER":
                                    set_lamp_state(station, 0) 
                                    pause_st = next((s for s in stations if s.get("group") == robot_id and s.get("type") == "pause"), None)
                                    if pause_st: set_lamp_state(pause_st, 0)
                                    robot_paused_state[robot_id] = False 
                                    cached_info["is_paused"] = False
                                        
                                if robot_info_cache:
                                    ws_msg = {"kuka_data": robot_info_cache}
                                    for ws in list(active_websockets):
                                        try: await ws.send_json(ws_msg)
                                        except Exception: active_websockets.discard(ws)
                            else:
                                err = f"KUKA API báo lỗi: {msg}"
                                app_logger.warning(f"[IP: {client_ip}] FAILED (KUKA): {err}")
                                if action == "RECOVER": set_lamp_state(station, 0)
                                return {"status": "error", "message": err}
                        else:
                            err = f"Không thể {action}, Robot không trong trạng thái 'Đang chạy' (Status 4)."
                            app_logger.warning(f"[IP: {client_ip}] BỎ QUA LỆNH: {err}")
                            return {"status": "error", "message": err}

        return {"status": "success"}
    except Exception as e:
        app_logger.error(f"Lỗi ghi API: {e}", exc_info=True)
        return {"status": "error", "message": "Lỗi nội bộ Server Node!"}
     
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.add(websocket)
    try:
        regs = context[0].getValues(3, 1, count=100)
        await websocket.send_json({"regs": regs})
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        active_websockets.discard(websocket)

# ==========================================
# 8. HÀM MAIN KHỞI ĐỘNG HỆ THỐNG
# ==========================================
if __name__ == "__main__":
    if sys.platform == 'win32':
        import warnings
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    local_ip = get_local_ip()
    sys_cfg = get_sys_config()
    
    server_port = sys_cfg.get("server_port", 8000)
    modbus_port = sys_cfg.get("modbus_port", 502)
    
    app_logger.info("=================================================================")
    app_logger.info(f" [MODBUS TCP SERVER] Đang lắng nghe tại cổng {modbus_port} (FULL HR)")
    app_logger.info(" [KUKA API SYSTEM] Luồng đồng bộ & API đã kích hoạt")
    app_logger.info(f" [WEB UI - SCADA] Truy cập cục bộ: http://localhost:{server_port}")
    app_logger.info(f" [WEB UI - SCADA] Truy cập từ máy khác: http://{local_ip}:{server_port}")
    app_logger.info("=================================================================")
    
    uvicorn.run(app, host="0.0.0.0", port=server_port, access_log=True)
