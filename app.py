import os
import sys
import socket
import json
import uuid
import uvicorn
import asyncio
import logging
import datetime
import time
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
ADMIN_PASS = "123456" 

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

robot_status_flags = {} 
robot_info_cache = {}   
robot_paused_state = {}
robot_mission_cache = {} # Lưu missionCode khi Start
cancel_lamp_hold_until = {}

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
                    "nodeCode": data[0].get("nodeCode", ""), 
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
            if data.get("success"): return True, "OK", mis_code
            return False, data.get("message", "Unknown Error"), None
        return False, f"HTTP {res.status_code}", None
    except Exception as e:
        return False, str(e), None

def cancel_workflow_mission(mission_code):
    url = f"{KUKA_BASE_URL}/missionCancel"
    req_id = str(uuid.uuid4()).replace("-", "")[:32]
    
    payload = {
        "requestId": req_id,
        "missionCode": mission_code,
        "cancelMode": "FORCE"
    }
    
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
    app_logger.info("=== KUKA MASTER LOOP ĐÃ KHỞI ĐỘNG ===")
    while True:
        try:
            cfg = load_config()
            robot_ids = set([st["group"] for st in cfg if st.get("type") in ["pause", "recover", "start", "cancel"] and st.get("group")])
            
            for rid in robot_ids:
                info = await asyncio.to_thread(get_robot_info, rid)
                if info:
                    status = info["status"]
                    occupy = info["occupyStatus"]
                    current_node = info.get("nodeCode", "")
                    
                    if status == 3 and occupy == 0:
                        robot_paused_state[rid] = False
                        robot_mission_cache.pop(rid, None) # Xóa missionCode khi xe rảnh
                        
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
                    start_stations = [s for s in cfg if s.get("group") == rid and s.get("type") == "start"]
                    for st in start_stations:
                        allowed_nodes_str = st.get("nodes", "")
                        allowed_nodes = [n.strip() for n in allowed_nodes_str.split(",") if n.strip()]
                        is_at_valid_node = True if not allowed_nodes else any(node in current_node for node in allowed_nodes)
                        
                        if status == 3 and occupy == 0 and is_at_valid_node:
                            set_lamp_state(st, 0)
                        else:
                            set_lamp_state(st, 1)
                            
                    # LOGIC ĐÈN NÚT CANCEL (Sáng = Được hủy, Tắt = Không có gì để hủy)
                    cancel_stations = [s for s in cfg if s.get("group") == rid and s.get("type") == "cancel"]
                    for st in cancel_stations:
                        # Ưu tiên 1: Đang trong 3 giây báo thành công -> Giữ đèn Sáng
                        if time.time() < cancel_lamp_hold_until.get(rid, 0):
                            set_lamp_state(st, 1)
                        # Ưu tiên 2: Xét theo trạng thái xe
                        else:
                            has_mission = robot_mission_cache.get(rid) is not None
                            if (status == 4 or occupy == 1) and has_mission:
                                set_lamp_state(st, 1) # SÁNG = Xe đang bận, cho phép hủy
                            else:
                                set_lamp_state(st, 0) # TẮT = Đã hủy xong hoặc đang rảnh

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
    if ip not in physical_clients:
        physical_clients[ip] = ModbusClient(host=ip, port=502, auto_open=True, timeout=0.5)
    return physical_clients[ip]

def set_lamp_state(station, state):
    # 1. Đổi màu đèn ảo trên Web (luôn dùng lamp_addr của Web)
    context[0].setValues(3, station["lamp_addr"], [state])
    
    # 2. Đổi màu đèn vật lý ngoài xưởng (nếu có khai báo)
    ip = station.get("ip")
    phy_lamp = station.get("phy_lamp") # <-- Đọc địa chỉ đèn vật lý
    
    if ip and phy_lamp is not None:
        try:
            client = get_physical_client(ip)
            client.write_single_register(phy_lamp, state) # <-- Ghi vào thanh ghi vật lý
        except Exception as e:
            app_logger.error(f"Lỗi điều khiển đèn vật lý {ip}: {e}")


async def scan_single_node(st):
    ip = st.get("ip")
    web_btn_addr = st.get("btn_addr")
    btn_type = st.get("type", "")
    
    # Lấy địa chỉ nút vật lý được khai báo
    phy_btn = st.get("phy_btn") 
    
    # CHỈ quét nếu nút này thuộc loại Pause/Recover và CÓ cấu hình nút vật lý
    if not (ip and web_btn_addr and phy_btn is not None and btn_type in ["pause", "recover"]):
        return

    client = get_physical_client(ip)
    try:
        # 1. ĐỌC TỪ ĐỊA CHỈ PHẦN CỨNG VẬT LÝ
        regs = await asyncio.to_thread(client.read_holding_registers, phy_btn, 1)
        
        if regs and regs[0] > 0:
            app_logger.info(f"Phát hiện nhấn nút {btn_type.upper()} vật lý tại {ip} (Reg {phy_btn})")
            
            # Xóa trạng thái phần cứng vật lý về 0
            await asyncio.to_thread(client.write_single_register, phy_btn, 0)
            
            # 2. ÁNH XẠ: Bắn tín hiệu sang ĐỊA CHỈ WEB ẢO để chạy logic hệ thống
            url = "http://127.0.0.1:8000/api/write"
            await asyncio.to_thread(requests.post, url, json={"address": web_btn_addr, "value": 1})
            await asyncio.sleep(0.5)
            await asyncio.to_thread(requests.post, url, json={"address": web_btn_addr, "value": 0})
            
    except Exception:
        pass

async def physical_button_scanner():
    """Vòng lặp chính quản lý quét Modbus vật lý"""
    app_logger.info("=== PHYSICAL BUTTON SCANNER ĐÃ KHỞI ĐỘNG (SONG SONG) ===")
    while True:
        try:
            cfg = load_config()
            
            # Gom tất cả các NÚT cần quét vào một danh sách công việc (tasks)
            tasks = [scan_single_node(st) for st in cfg]
            
            if tasks:
                # asyncio.gather sẽ chạy TẤT CẢ các tasks cùng MỘT LÚC
                await asyncio.gather(*tasks)
                
        except Exception as e:
            app_logger.error(f"Lỗi trong vòng lặp scanner: {e}")
            
        # Đợi 300ms rồi quét đồng loạt vòng tiếp theo
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
        except Exception as e:
            pass
        await asyncio.sleep(0.05)

@asynccontextmanager
async def lifespan(app: FastAPI):
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

            if 51 <= addr <= 100: # Nếu địa chỉ này thuộc vùng ĐÈN
                cfg = load_config()
                st_lamp = next((s for s in cfg if s.get("lamp_addr") == addr), None)
                if st_lamp and st_lamp.get("ip") and st_lamp.get("phy_lamp") is not None:
                    try:
                        client = get_physical_client(st_lamp["ip"])
                        client.write_single_register(st_lamp["phy_lamp"], val)
                    except Exception: 
                        pass
                    
            if val > 0:
                cfg = load_config()
                station = next((s for s in cfg if s.get("btn_addr") == addr), None)
                
                if station and station.get("type") in ["pause", "recover", "start", "cancel"]:
                    robot_id = station.get("group", "")
                    btn_type = station["type"]
                    
                    cached_info = robot_info_cache.get(robot_id)
                    if not cached_info:
                        app_logger.warning(f"TỪ CHỐI LỆNH: Mất kết nối tới Robot {robot_id}")
                        return {"status": "error"}
                        
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
                            app_logger.info(f"ACTION TRIGGERED: START Workflow '{wf_code}' cho Robot {robot_id}")
                            is_success, msg, mis_code = await asyncio.to_thread(dispatch_workflow_mission, wf_code, robot_id)
                            
                            if is_success:
                                app_logger.info(f"SUCCESS: Đã Start Robot {robot_id}.")
                                robot_mission_cache[robot_id] = mis_code # Lưu missionCode
                                cached_info["status"] = 4 
                                cached_info["occupyStatus"] = 1
                                set_lamp_state(station, 1)
                                robot_status_flags[robot_id] = True 
                            else:
                                app_logger.warning(f"FAILED: Lỗi Start Robot {robot_id}. Lý do: {msg}")
                        else:
                            app_logger.warning(f"TỪ CHỐI LỆNH START: Robot {robot_id} không sẵn sàng hoặc sai vị trí.")
                            
                    # --- 2. LOGIC NÚT CANCEL ---
                    elif btn_type == "cancel":
                        mis_code = robot_mission_cache.get(robot_id)
                        if mis_code and (status == 4 or occupy == 1):
                            app_logger.info(f"ACTION TRIGGERED: CANCEL Mission '{mis_code}' cho Robot {robot_id}")
                            is_success, msg = await asyncio.to_thread(cancel_workflow_mission, mis_code)
                            
                            if is_success:
                                app_logger.info(f"SUCCESS: Đã HỦY nhiệm vụ của Robot {robot_id}.")
                                # 1. Xóa Mission ngay lập tức
                                robot_mission_cache.pop(robot_id, None) 
                                
                                # 2. Bắt đầu đếm 3 giây giữ đèn sáng
                                cancel_lamp_hold_until[robot_id] = time.time() + 3.0
                                set_lamp_state(station, 1) 
                                
                                # 3. Đưa xe về trạng thái Rảnh ngay lập tức để nút Start sáng lại
                                cached_info["status"] = 3
                                cached_info["occupyStatus"] = 0
                                robot_status_flags[robot_id] = False
                                
                                if robot_info_cache:
                                    ws_msg = {"kuka_data": robot_info_cache}
                                    for ws in list(active_websockets):
                                        try: await ws.send_json(ws_msg)
                                        except Exception: active_websockets.discard(ws)
                            else:
                                app_logger.warning(f"FAILED: Lỗi Cancel Robot {robot_id}. Lý do: {msg}")
                        else:
                            app_logger.warning(f"TỪ CHỐI LỆNH HỦY: Robot {robot_id} không bận hoặc không có Mission được lưu.")
                            
                    # --- 3. LOGIC NÚT PAUSE / RECOVER ---
                    else:
                        action = "PAUSE" if btn_type == "pause" else "RECOVER"
                        if robot_status_flags.get(robot_id, False):
                            app_logger.info(f"ACTION TRIGGERED: {action} cho Robot {robot_id}")
                            
                            if action == "RECOVER":
                                set_lamp_state(station, 1)
                                
                            is_success, msg = await asyncio.to_thread(control_kuka_robot, action, robot_id)
                            
                            if is_success:
                                if action == "PAUSE":
                                    app_logger.info(f"SUCCESS: Đã Pause Robot {robot_id}.")
                                    set_lamp_state(station, 1)
                                    robot_paused_state[robot_id] = True 
                                    cached_info["is_paused"] = True
                                        
                                elif action == "RECOVER":
                                    app_logger.info(f"SUCCESS: Đã Recover Robot {robot_id}.")
                                    set_lamp_state(station, 0) 
                                    
                                    pause_st = next((s for s in cfg if s.get("group") == robot_id and s.get("type") == "pause"), None)
                                    if pause_st: set_lamp_state(pause_st, 0)
                                    
                                    robot_paused_state[robot_id] = False 
                                    cached_info["is_paused"] = False
                                        
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
