import os
import sys
import socket
import json
import uvicorn
import asyncio
import logging
import datetime
import traceback
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

# --- 1. CẤU HÌNH LOG (BẢN KỶ LUẬT SẮT) ---
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
        # 1. Luôn cho qua Cảnh báo / Lỗi hệ thống và Web UI
        if record.levelno >= logging.WARNING: return True
        if record.name == "WebSimulator": return True
        
        if "pymodbus" in record.name:
            msg = record.getMessage()
            
            # 2. Lọc sạch đuôi rác " old_data:" của pymodbus
            if " old_data:" in msg:
                msg = msg.split(" old_data:")[0]
                record.msg = msg 

            if "Connected" in msg or "Server listening" in msg:
                return True

            # 3. LOGIC LỌC DỮ LIỆU ĐỘNG (CHỈ IN KHI CÓ SỰ THAY ĐỔI)
            if msg.startswith("recv:") or msg.startswith("send:"):
                parts = msg.split()
                if len(parts) >= 9:
                    # Lấy mã giao dịch (TxID) để ghép cặp Nhận - Gửi
                    tx_id = parts[1] + "-" + parts[2]
                    
                    # Bóc tách phần Dữ liệu (Bỏ qua 3 byte đầu chứa chữ recv/send và TxID)
                    payload = " ".join(parts[3:]) 
                    
                    if msg.startswith("recv:"):
                        # Lưu Request này làm chìa khóa gốc
                        channel = "REQ:" + payload
                        self.tx_to_channel[tx_id] = channel
                        
                        # Chống tràn bộ nhớ nếu Master ngắt kết nối giữa chừng
                        if len(self.tx_to_channel) > 100: self.tx_to_channel.clear()
                        
                        # Nếu Request y hệt lần trước -> Im lặng (Block)
                        if self.last_seen.get(channel) == payload:
                            return False
                        self.last_seen[channel] = payload
                        return True
                        
                    elif msg.startswith("send:"):
                        # Lấy lại chìa khóa gốc để kiểm tra Response tương ứng
                        channel = self.tx_to_channel.get(tx_id, "UNKNOWN")
                        if tx_id in self.tx_to_channel:
                            del self.tx_to_channel[tx_id]
                            
                        resp_key = "RESP:" + channel
                        
                        # NẾU DỮ LIỆU GỬI ĐI Y HỆT LẦN TRƯỚC -> IM LẶNG!
                        if self.last_seen.get(resp_key) == payload:
                            return False
                            
                        # NẾU DỮ LIỆU THAY ĐỔI (VD: 0 nhảy lên 1) -> IN RA!
                        self.last_seen[resp_key] = payload
                        return True
                        
            # Chặn đứng các log nội bộ khác (bao gồm cả Uvicorn WebSocket)
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

pymodbus_logger = logging.getLogger("pymodbus.logging")
pymodbus_logger.setLevel(logging.DEBUG) 
pymodbus_logger.addHandler(file_handler)
pymodbus_logger.addHandler(console_handler)
pymodbus_logger.propagate = False

logging.getLogger("uvicorn.error").addHandler(file_handler)

# --- 2. QUẢN LÝ CẤU HÌNH JSON ĐỘNG ---
CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_config(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# --- 3. KHỞI TẠO BỘ NHỚ MODBUS (100 HOLDING REGISTERS) ---
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
    except Exception:
        return "127.0.0.1"

def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'): return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

async def monitor_modbus_state():
    last_regs = None
    while True:
        try:
            regs = context[0].getValues(3, 1, count=100)
            if regs != last_regs:
                if last_regs is not None:
                    app_logger.info("Phát hiện thay đổi trên vùng nhớ Modbus (1-100)")
                last_regs = regs
                msg = {"regs": regs}
                for ws in list(active_websockets):
                    try:
                        await ws.send_json(msg)
                    except Exception as e:
                        active_websockets.discard(ws)
        except Exception as e:
            app_logger.error(f"Lỗi quét Modbus: {e}")
        await asyncio.sleep(0.05)

@asynccontextmanager
async def lifespan(app: FastAPI):
    modbus_task = asyncio.create_task(StartAsyncTcpServer(context=context, address=("0.0.0.0", 502)))
    asyncio.create_task(monitor_modbus_state())
    yield
    modbus_task.cancel()

app = FastAPI(lifespan=lifespan)

app.mount("/styles", StaticFiles(directory=get_resource_path("styles")), name="styles")
app.mount("/scripts", StaticFiles(directory=get_resource_path("scripts")), name="scripts")

@app.get("/")
async def index():
    return FileResponse(get_resource_path("templates/index.html"))

# --- 4. API QUẢN LÝ CẤU HÌNH (Đã thêm chức năng Sửa) ---
@app.get("/api/config")
async def get_config():
    return load_config()

@app.post("/api/config")
async def add_config(payload: dict):
    cfg = load_config()
    cfg.append(payload)
    save_config(cfg)
    app_logger.info(f"Đã tạo trạm mới: {payload['name']}")
    return {"status": "success"}

@app.put("/api/config/{old_addr}")
async def edit_config(old_addr: int, payload: dict):
    cfg = load_config()
    for i, st in enumerate(cfg):
        if st["btn_addr"] == old_addr:
            cfg[i] = payload
            break
    save_config(cfg)
    app_logger.info(f"Đã sửa trạm (Địa chỉ cũ: {old_addr}) -> {payload['name']}")
    return {"status": "success"}

@app.delete("/api/config/{btn_addr}")
async def delete_config(btn_addr: int):
    cfg = load_config()
    cfg = [c for c in cfg if c["btn_addr"] != btn_addr]
    save_config(cfg)
    app_logger.info(f"Đã xóa cấu hình trạm có địa chỉ Nút: {btn_addr}")
    return {"status": "success"}

# --- 5. API ĐIỀU KHIỂN TỪ WEB ---
@app.post("/api/write")
async def write_register(payload: dict):
    try:
        addr = int(payload.get("address", 1)) 
        val = int(payload.get("value", 0))
        if 1 <= addr <= 100:
            context[0].setValues(3, addr, [val]) 
            app_logger.info(f"Web UI ra lệnh GHI -> Địa chỉ {addr}: Giá trị {val}")
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

if __name__ == "__main__":
    if sys.platform == 'win32':
        import warnings
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    local_ip = get_local_ip()
    app_logger.info("=================================================================")
    app_logger.info(" [MODBUS TCP SERVER] Đang lắng nghe tại cổng 502 (FULL HR)")
    app_logger.info(f" [WEB UI - SCADA] Truy cập cục bộ: http://localhost:8000")
    app_logger.info(f" [WEB UI - SCADA] Truy cập từ máy khác: http://{local_ip}:8000")
    app_logger.info("=================================================================")
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
