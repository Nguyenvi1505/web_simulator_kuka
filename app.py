import os
import sys
import socket
import json
import uuid
import uvicorn
import asyncio
import logging
import datetime
import requests
from contextlib import asynccontextmanager
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer
from pyModbusTCP.client import ModbusClient

# ==========================================
# 1. CẤU HÌNH LOG (LỌC DỮ LIỆU THÔNG MINH)
# ==========================================
if not os.path.exists("logs"):
    os.makedirs("logs")

current_date = datetime.datetime.now().strftime("%Y%m%d")
log_filepath = os.path.join("logs", f"{current_date}_modbus_traffic.log")

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

log_formatter = logging.Formatter("%(asctime)s | [%(levelname)s] %(name)s: %(message)s")

file_handler = logging.FileHandler(log_filepath, encoding="utf-8")
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

# ==========================================
# 2. QUẢN LÝ CẤU HÌNH JSON (CÓ BẢO MẬT)
# ==========================================
CONFIG_FILE = "config.json"
ADMIN_PASS = "123456" # Mật khẩu quản trị Thêm/Sửa/Xóa

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_config(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# ==========================================
# 3. KUKA API & MASTER LOOP (WATCHDOG)
# ==========================================
KUKA_BASE_URL = "http://192.168.221.250:5000/interfaces/api/amr"
KUKA_TOKEN = "36b8ac34198ea45463f77f469fd9fe5d27d310ff66b84635df13dd9a1b444117"
KUKA_HEADERS = {"Content-Type": "application/json", "Authorization": KUKA_TOKEN}

robot_status_flags = {} # Cờ đánh dấu trạng thái (Chỉ = True khi xe Executing)
robot_info_cache = {}   # Bộ đệm để bắn dữ liệu Pin/Trạng thái lên Web
robot_paused_state = {}

def get_robot_info(robot_id):
    try:
        url = f"{KUKA_BASE_URL}/robotQuery"
        res = requests.post(url, headers=KUKA_HEADERS, json={"robotId": robot_id}, timeout=2.0)
        if res.status_code == 200 and res.json().get("success"):
            data = res.json().get("data", [])
            if data: 
                return {
                    "status": data[0].get("status"),
                    "occupyStatus": data[0].get("occupyStatus", 0),
                    "nodeCode": data[0].get("nodeCode", ""), # <--- Lấy thêm mã nodeCode hiện tại của xe
                    "battery": data[0].get("batteryLevel", 0.0)
                }
    except Exception as e:
        app_logger.error(f"API Error (Get Info - Xe {robot_id}): {e}")
    return None

def control_kuka_robot(action, robot_id):
    url = f"{KUKA_BASE_URL}/pauseMission" if action == "PAUSE" else f"{KUKA_BASE_URL}/recoverMission"
    try:
        res = requests.post(url, headers=KUKA_HEADERS, json={"robotId": robot_id}, timeout=5.0)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"): return True, "OK"
            return False, data.get("message", "Unknown Error")
        return False, f"HTTP {res.status_code}"
    except Exception as e:
        return False, str(e)

def dispatch_workflow_mission(template_code, robot_id=None):
    """Kích hoạt Workflow dựa trên code bạn cung cấp"""
    url = f"{KUKA_BASE_URL}/submitMission"
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
        res = requests.post(url, headers=KUKA_HEADERS, json=payload, timeout=5.0)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"): return True, "OK"
            return False, data.get("message", "Unknown Error")
        return False, f"HTTP {res.status_code}"
    except Exception as e:
        return False, str(e)
    
async def kuka_master_loop():
    """Luồng chạy ngầm để liên tục cập nhật trạng thái Robot & Đèn"""
    app_logger.info("=== KUKA MASTER LOOP ĐÃ KHỞI ĐỘNG ===")
    while True:
        try:
            cfg = load_config()
            robot_ids = set([st["group"] for st in cfg if st.get("type") in ["pause", "recover", "start"] and st.get("group")])
            
            for rid in robot_ids:
                info = await asyncio.to_thread(get_robot_info, rid)
                if info:
                    status = info["status"]
                    occupy = info["occupyStatus"]
                    current_node = info.get("nodeCode", "")
                    
                    # Nếu xe đã chạy xong nhiệm vụ (Rảnh hoàn toàn), tự động xóa cờ Tạm dừng (nếu bị kẹt)
                    if status == 3 and occupy == 0:
                        robot_paused_state[rid] = False
                        
                    # Chèn cờ Tạm dừng ảo vào dữ liệu gửi lên Web
                    info["is_paused"] = robot_paused_state.get(rid, False)
                    robot_info_cache[rid] = info 
                    
                    current_flag = robot_status_flags.get(rid, False)
                    
                    # LOGIC BẢO VỆ NÚT PAUSE/RECOVER (GIỮ NGUYÊN)
                    if status == 4:
                        if not current_flag:
                            app_logger.info(f"Robot {rid} is EXECUTING (Status 4). Enabling Pause/Recover.")
                            robot_status_flags[rid] = True
                    else:
                        if current_flag:
                            app_logger.info(f"Robot {rid} status changed to {status}. Disabling Pause/Recover.")
                            robot_status_flags[rid] = False
                            
                    # LOGIC ĐÈN NÚT START (GIỮ NGUYÊN)
                    start_stations = [s for s in cfg if s.get("group") == rid and s.get("type") == "start"]
                    for st in start_stations:
                        allowed_nodes_str = st.get("nodes", "")
                        allowed_nodes = [n.strip() for n in allowed_nodes_str.split(",") if n.strip()]
                        is_at_valid_node = True if not allowed_nodes else any(node in current_node for node in allowed_nodes)
                        
                        if status == 3 and occupy == 0 and is_at_valid_node:
                            set_lamp_state(st, 0)
                        else:
                            set_lamp_state(st, 1)

            if robot_info_cache:
                msg = {"kuka_data": robot_info_cache}
                for ws in list(active_websockets):
                    try: await ws.send_json(msg)
                    except Exception: active_websockets.discard(ws)
                    
        except Exception as e:
            app_logger.error(f"Master loop crashed: {e}")
            
        await asyncio.sleep(2.0)

# ==========================================
# 4. GIAO TIẾP VỚI CÁC NÚT VẬT LÝ (PYMODBUSTCP)
# ==========================================
physical_clients = {}

def get_physical_client(ip):
    """Giữ kết nối Modbus tới các hộp nút nhấn vật lý"""
    if ip not in physical_clients:
        physical_clients[ip] = ModbusClient(host=ip, port=502, auto_open=True, timeout=0.5)
    return physical_clients[ip]

def set_lamp_state(station, state):
    """Bật/Tắt đèn đồng bộ trên cả Web Server và Thiết bị phần cứng"""
    # Đèn trên Web
    context[0].setValues(3, station["lamp_addr"], [state])
    
    # Đèn phần cứng (nếu có khai báo IP)
    ip = station.get("ip")
    if ip:
        try:
            client = get_physical_client(ip)
            client.write_single_register(station["lamp_addr"], state)
        except Exception as e:
            app_logger.error(f"Lỗi điều khiển đèn vật lý {ip}: {e}")

async def physical_button_scanner():
    """Luồng quét liên tục các Nút nhấn vật lý (300ms/lần)"""
    app_logger.info("=== PHYSICAL BUTTON SCANNER ĐÃ KHỞI ĐỘNG ===")
    while True:
        try:
            cfg = load_config()
            for st in cfg:
                ip = st.get("ip")
                btn_addr = st.get("btn_addr")
                btn_type = st.get("type", "")
                robot_id = st.get("group", "")
                
                if ip and btn_addr and btn_type in ["pause", "recover", "start"]:
                    
                    # LOGIC CŨ: Nút Pause/Recover chỉ được quét khi Cờ mở (Status = 4)
                    if btn_type in ["pause", "recover"] and not robot_status_flags.get(robot_id, False):
                        continue
                        
                    client = get_physical_client(ip)
                    regs = await asyncio.to_thread(client.read_holding_registers, btn_addr, 1)
                    
                    if regs and regs[0] > 0:
                        app_logger.info(f"Phát hiện nhấn nút {btn_type.upper()} vật lý tại {ip}")
                        await asyncio.to_thread(client.write_single_register, btn_addr, 0)
                        
                        url = "http://127.0.0.1:8000/api/write"
                        await asyncio.to_thread(requests.post, url, json={"address": btn_addr, "value": 1})
        except Exception:
            pass
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
    """Gửi thay đổi bộ nhớ Modbus (cho các nút Khác) lên WebSocket"""
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
        except Exception as e:
            pass
        await asyncio.sleep(0.05)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Khởi chạy 4 tiến trình ngầm cực mạnh
    modbus_task = asyncio.create_task(StartAsyncTcpServer(context=context, address=("0.0.0.0", 502)))
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
# 6. API QUẢN LÝ CẤU HÌNH (CÓ PASS BẢO MẬT)
# ==========================================
@app.get("/api/config")
async def get_config():
    return load_config()

@app.post("/api/config")
async def add_config(payload: dict):
    if payload.pop("password", "") != ADMIN_PASS: return {"status": "error", "message": "Sai mật khẩu! Từ chối truy cập."}
    cfg = load_config()
    cfg.append(payload)
    save_config(cfg)
    app_logger.info(f"Đã tạo NÚT mới: {payload['name']}")
    return {"status": "success"}

@app.put("/api/config/{old_addr}")
async def edit_config(old_addr: int, payload: dict):
    if payload.pop("password", "") != ADMIN_PASS: return {"status": "error", "message": "Sai mật khẩu! Từ chối truy cập."}
    cfg = load_config()
    for i, st in enumerate(cfg):
        if st["btn_addr"] == old_addr:
            cfg[i] = payload
            break
    save_config(cfg)
    app_logger.info(f"Đã sửa NÚT (Địa chỉ cũ: {old_addr}) -> {payload['name']}")
    return {"status": "success"}

@app.delete("/api/config/{btn_addr}")
async def delete_config(btn_addr: int, password: str = ""):
    if password != ADMIN_PASS: return {"status": "error", "message": "Sai mật khẩu! Từ chối xóa NÚT."}
    cfg = load_config()
    cfg = [c for c in cfg if c["btn_addr"] != btn_addr]
    save_config(cfg)
    app_logger.info(f"Đã xóa cấu hình NÚT có địa chỉ Nút: {btn_addr}")
    return {"status": "success"}

# ==========================================
# 7. API TRUNG TÂM & CHUẨN HÓA LỆNH KUKA
# ==========================================
@app.post("/api/write")
async def write_register(payload: dict):
    try:
        addr = int(payload.get("address", 1)) 
        val = int(payload.get("value", 0))
        
        if 1 <= addr <= 100:
            context[0].setValues(3, addr, [val]) 
            app_logger.info(f"Ra lệnh GHI -> Địa chỉ {addr}: Giá trị {val}")

            if val > 0:
                cfg = load_config()
                station = next((s for s in cfg if s.get("btn_addr") == addr), None)
                
                if station and station.get("type") in ["pause", "recover", "start"]:
                    robot_id = station.get("group", "")
                    btn_type = station["type"]
                    
                    # --- 1. LOGIC RIÊNG CHO NÚT START ---
                    if btn_type == "start":
                        cached_info = robot_info_cache.get(robot_id)
                        if not cached_info:
                            app_logger.warning(f"TỪ CHỐI LỆNH: Mất kết nối tới Robot {robot_id}")
                            return {"status": "error"}
                            
                        status = cached_info.get("status")
                        occupy = cached_info.get("occupyStatus")
                        current_node = cached_info.get("nodeCode", "")
                        
                        allowed_nodes_str = station.get("nodes", "")
                        allowed_nodes = [n.strip() for n in allowed_nodes_str.split(",") if n.strip()]
                        is_at_valid_node = True if not allowed_nodes else any(node in current_node for node in allowed_nodes)
                        
                        if status == 3 and occupy == 0 and is_at_valid_node:
                            wf_code = station.get("wf")
                            app_logger.info(f"ACTION TRIGGERED: START Workflow '{wf_code}' cho Robot {robot_id}")
                            is_success, msg = await asyncio.to_thread(dispatch_workflow_mission, wf_code, robot_id)
                            
                            if is_success:
                                app_logger.info(f"SUCCESS: Đã Start Robot {robot_id}.")
                                cached_info["status"] = 4 
                                cached_info["occupyStatus"] = 1
                                set_lamp_state(station, 1) # Bật đèn Start (Khóa lại)
                                robot_status_flags[robot_id] = True # Mở khóa cho Pause/Recover
                            else:
                                app_logger.warning(f"FAILED: Lỗi Start Robot {robot_id}. Lý do: {msg}")
                        else:
                            app_logger.warning(f"TỪ CHỐI LỆNH START: Robot {robot_id} không sẵn sàng hoặc sai vị trí.")
                    
                    # --- 2. LOGIC CŨ CHO NÚT PAUSE / RECOVER ---
                    else:
                        action = "PAUSE" if btn_type == "pause" else "RECOVER"
                        
                        # Chỉ cho phép bấm khi cờ bảo vệ mở (xe đang Status 4)
                        if robot_status_flags.get(robot_id, False):
                            app_logger.info(f"ACTION TRIGGERED: {action} cho Robot {robot_id}")
                            
                            # Nhấn Recover thì bật đèn Recover trước để báo hiệu
                            if action == "RECOVER":
                                set_lamp_state(station, 1)
                                
                            is_success, msg = await asyncio.to_thread(control_kuka_robot, action, robot_id)
                            
                            if is_success:
                                if action == "PAUSE":
                                    app_logger.info(f"SUCCESS: Đã Pause Robot {robot_id}.")
                                    set_lamp_state(station, 1)
                                    
                                    # THÊM MỚI: Bật cờ Tạm dừng
                                    robot_paused_state[robot_id] = True 
                                    if cached_info is not None:
                                        cached_info["is_paused"] = True
                                        
                                elif action == "RECOVER":
                                    app_logger.info(f"SUCCESS: Đã Recover Robot {robot_id}.")
                                    set_lamp_state(station, 0) 
                                    
                                    pause_st = next((s for s in cfg if s.get("group") == robot_id and s.get("type") == "pause"), None)
                                    if pause_st: set_lamp_state(pause_st, 0)
                                    
                                    # THÊM MỚI: Tắt cờ Tạm dừng
                                    robot_paused_state[robot_id] = False 
                                    if cached_info is not None:
                                        cached_info["is_paused"] = False
                                        
                                # ĐỒNG BỘ GIAO DIỆN WEB TỨC THÌ (Bắn thẳng data đã ép cờ is_paused)
                                if robot_info_cache:
                                    ws_msg = {"kuka_data": robot_info_cache}
                                    for ws in list(active_websockets):
                                        try: await ws.send_json(ws_msg)
                                        except Exception: active_websockets.discard(ws)
                            else:
                                app_logger.warning(f"FAILED: Lỗi {action} Robot {robot_id}. Lý do: {msg}")
                                if action == "RECOVER": set_lamp_state(station, 0)
                        else:
                            app_logger.warning(f"BỎ QUA LỆNH: Nút {action} cho Robot {robot_id} bị vô hiệu do trạng thái khác 4.")

        return {"status": "success"}
    except Exception as e:
        app_logger.error(f"Lỗi ghi API: {e}")
        return {"status": "error"}
    
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
    app_logger.info("=================================================================")
    app_logger.info(" [MODBUS TCP SERVER] Đang lắng nghe tại cổng 502 (FULL HR)")
    app_logger.info(" [KUKA API SYSTEM] Luồng đồng bộ & API đã kích hoạt")
    app_logger.info(f" [WEB UI - SCADA] Truy cập cục bộ: http://localhost:8000")
    app_logger.info(f" [WEB UI - SCADA] Truy cập từ máy khác: http://{local_ip}:8000")
    app_logger.info("=================================================================")
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
