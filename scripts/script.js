const container = document.getElementById('stations-container');
const emptyState = document.getElementById('empty-state');
const modal = document.getElementById('config-modal');
const modalTitle = document.getElementById('modal-title');

let stations = [];
let editModeAddr = null; // Biến lưu trạng thái đang thêm mới hay đang sửa

let pressTimers = {};
let isLongPress = {};
let clickCounts = {};
let clickTimeouts = {};

// LOAD CẤU HÌNH VÀ XỬ LÝ GIAO DIỆN TRỐNG
async function loadConfig() {
    const res = await fetch('/api/config');
    stations = await res.json();
    
    if (stations.length === 0) {
        emptyState.style.display = 'block';
        container.style.display = 'none';
    } else {
        emptyState.style.display = 'none';
        container.style.display = 'flex';
        renderStations();
    }
}

// LOGIC BẬT/TẮT HỘP THOẠI (MODAL)
function openModal(addr = null) {
    modal.style.display = 'block';
    if (addr !== null) {
        editModeAddr = addr;
        modalTitle.textContent = "SỬA THÔNG TIN NÚT";
        const st = stations.find(s => s.btn_addr === addr);
        document.getElementById('inp-group').value = st.group || "Chung"; // Thêm dòng này
        document.getElementById('inp-type').value = st.type || "khac";
        document.getElementById('inp-ip').value = st.ip || "";
        document.getElementById('inp-phy-btn').value = st.phy_btn ?? "";
        document.getElementById('inp-phy-lamp').value = st.phy_lamp ?? "";
        document.getElementById('inp-wf').value = st.wf || "";
        document.getElementById('inp-nodes').value = st.nodes || "";
        document.getElementById('inp-name').value = st.name;
        document.getElementById('inp-btn').value = st.btn_addr;
        document.getElementById('inp-lamp').value = st.lamp_addr;
        if(typeof toggleInputs === 'function') toggleInputs();
        if(typeof toggleWfInput === 'function') toggleWfInput();
    } else {
        editModeAddr = null;
        modalTitle.textContent = "TẠO NÚT MỚI";
        document.getElementById('inp-group').value = "Chung"; 
        document.getElementById('inp-ip').value = ""; 
        document.getElementById('inp-phy-btn').value = ""; 
        document.getElementById('inp-phy-lamp').value = ""; 
        document.getElementById('inp-wf').value = ""; 
        document.getElementById('inp-nodes').value = ""; 
        document.getElementById('inp-name').value = '';
        document.getElementById('inp-btn').value = '';
        document.getElementById('inp-lamp').value = '';
        if(typeof toggleInputs === 'function') toggleInputs();
        if(typeof toggleWfInput === 'function') toggleWfInput();
    }
}

function closeModal() {
    modal.style.display = 'none';
    editModeAddr = null;
}

// GỌI API LƯU HOẶC SỬA
async function saveStation() {
    const group = document.getElementById('inp-group').value.trim() || "Chung";
    const btnType = document.getElementById('inp-type').value;
    const ip = document.getElementById('inp-ip').value.trim();
    const phyBtn = parseInt(document.getElementById('inp-phy-btn').value);
    const phyLamp = parseInt(document.getElementById('inp-phy-lamp').value);
    const wfCode = document.getElementById('inp-wf').value.trim();
    const nodes = document.getElementById('inp-nodes').value.trim();
    const name = document.getElementById('inp-name').value.trim();
    const btnAddr = parseInt(document.getElementById('inp-btn').value);
    const lampAddr = parseInt(document.getElementById('inp-lamp').value);

    if(!name || isNaN(btnAddr) || isNaN(lampAddr)) return alert("Vui lòng nhập đầy đủ thông tin!");
    if (btnType === 'start' && !wfCode) return alert("Vui lòng nhập mã Workflow (template_code)!");
    if(btnAddr < 1 || btnAddr > 50) return alert("Địa chỉ Nút phải từ 1 đến 50!");
    if(lampAddr < 51 || lampAddr > 100) return alert("Địa chỉ Đèn phải từ 51 đến 100!");

    const isBtnExists = stations.find(s => s.btn_addr === btnAddr && s.btn_addr !== editModeAddr);
    const isLampExists = stations.find(s => s.lamp_addr === lampAddr && s.btn_addr !== editModeAddr);
    
    if(isBtnExists) return alert("Địa chỉ Nút này đã được sử dụng!");
    if(isLampExists) return alert("Địa chỉ Đèn này đã được sử dụng!");

    // --- BƯỚC BẢO MẬT: HỎI MẬT KHẨU TRƯỚC KHI GỬI LÊN PYTHON ---
    const pass = prompt("Vui lòng nhập mật khẩu quản trị để lưu thay đổi:");
    if (pass === null) return; // Nếu người dùng bấm Cancel thì hủy thao tác

    const payload = { 
        group: group, type: btnType, ip: ip, wf: wfCode, nodes: nodes, name: name, 
        btn_addr: btnAddr, lamp_addr: lampAddr, password: pass,
        phy_btn: isNaN(phyBtn) ? null : phyBtn,
        phy_lamp: isNaN(phyLamp) ? null : phyLamp
    };
    
    let res;
    if (editModeAddr === null) {
        res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
    } else {
        res = await fetch(`/api/config/${editModeAddr}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
    }

    const data = await res.json();
    
    // Nếu Python phát hiện sai pass, báo lỗi và giữ nguyên bảng để nhập lại
    if (data.status === "error") {
        alert(data.message);
        return; 
    }

    closeModal();
    loadConfig();
}

// GỌI API XÓA
async function deleteStation(btnAddr) {
    if(confirm("Xóa NÚT này khỏi hệ thống? Dữ liệu không thể khôi phục.")) {
        
        // Yêu cầu mật khẩu trước khi xóa
        const pass = prompt("Vui lòng nhập mật khẩu quản trị để XÓA:");
        if (pass === null) return;

        const res = await fetch(`/api/config/${btnAddr}?password=${encodeURIComponent(pass)}`, { method: 'DELETE' });
        const data = await res.json();
        
        if (data.status === "error") {
            alert(data.message);
            return;
        }
        loadConfig();
    }
}

// GHI MODBUS
function writeRegister(address, value) {
    fetch('/api/write', { 
        method: 'POST', 
        headers: { 'Content-Type': 'application/json' }, 
        body: JSON.stringify({ address: address, value: value }) 
    });
}

// VẼ GIAO DIỆN
function renderStations() {
    let htmlContent = '';
    
    // 1. Phân loại các NÚT vào từng nhóm
    const groupedStations = {};
    stations.forEach(st => {
        const groupName = st.group || "Khu vực chung"; 
        if (!groupedStations[groupName]) {
            groupedStations[groupName] = [];
        }
        groupedStations[groupName].push(st);
    });

    // 2. Render HTML cho từng Nhóm
    for (const [group, items] of Object.entries(groupedStations)) {
        htmlContent += `
        <div class="robot-group">
            <div class="group-header-wrapper">
                <h2 class="group-title">${group}</h2>
                <div class="robot-badges">
                    <span id="badge-status-${group}" class="r-badge status-badge" style="display:none;"></span>
                    <span id="badge-batt-${group}" class="r-badge batt-badge" style="display:none;"></span>
                </div>
            </div>
            <div class="group-items-container">
        `;
        
        // Render các nút bấm con bên trong nhóm này
        items.forEach(st => {
            htmlContent += `
            <div class="station-card" id="card-${st.btn_addr}">
                <div class="action-btns">
                    <button class="icon-btn edit-btn" title="Sửa" onclick="openModal(${st.btn_addr})">✎</button>
                    <button class="icon-btn del-btn" title="Xóa" onclick="deleteStation(${st.btn_addr})">✖</button>
                </div>
                <div class="station-header-title" title="${st.name}">${st.name}</div>
                <div class="stats">
                    <div class="stat-line">Nút (Reg ${st.btn_addr}): <span id="regVal-${st.btn_addr}" class="val-txt" style="color: #f97316;">0</span></div>
                    <div class="stat-line">Đèn (Reg ${st.lamp_addr}): <span id="lampVal-${st.lamp_addr}" class="val-txt" style="color:#047857">OFF</span></div>
                </div>
                <div class="btn-wrapper">
                    <button id="btn-${st.btn_addr}" class="illuminated-btn lamp-off"><div>CALL</div></button>
                </div>
                <div class="override-zone">
                    <div style="font-size: 11px; color:#6b7280; margin-bottom:8px;">TEST TÍN HIỆU ĐÈN</div>
                    <div class="toggle-switch">
                        <label class="switch">
                            <input type="checkbox" id="toggle-${st.lamp_addr}">
                            <span class="slider"></span>
                        </label>
                    </div>
                </div>
            </div>`;
        });
        
        htmlContent += `</div></div>`;
    }
    
    container.innerHTML = htmlContent;

    // 3. Gắn sự kiện click nguyên bản
    stations.forEach(st => {
        const btn = document.getElementById(`btn-${st.btn_addr}`);
        const toggle = document.getElementById(`toggle-${st.lamp_addr}`);

        const handleDown = (e) => {
            e.preventDefault();
            btn.classList.add('active-press');
            isLongPress[st.btn_addr] = false;
            pressTimers[st.btn_addr] = setTimeout(() => {
                isLongPress[st.btn_addr] = true;
                writeRegister(st.btn_addr, 3);
            }, 700);
        };

        const handleUp = (e) => {
            e.preventDefault();
            if (!btn.classList.contains('active-press') && !isLongPress[st.btn_addr]) return;
            
            btn.classList.remove('active-press');
            clearTimeout(pressTimers[st.btn_addr]);

            if (isLongPress[st.btn_addr]) {
                writeRegister(st.btn_addr, 0);
                return;
            }

            clickCounts[st.btn_addr] = (clickCounts[st.btn_addr] || 0) + 1;
            if (clickCounts[st.btn_addr] === 1) {
                clickTimeouts[st.btn_addr] = setTimeout(() => {
                    writeRegister(st.btn_addr, 1); 
                    setTimeout(() => writeRegister(st.btn_addr, 0), 1000); 
                    clickCounts[st.btn_addr] = 0;
                }, 300);
            } else if (clickCounts[st.btn_addr] === 2) {
                clearTimeout(clickTimeouts[st.btn_addr]);
                writeRegister(st.btn_addr, 2); 
                setTimeout(() => writeRegister(st.btn_addr, 0), 1000); 
                clickCounts[st.btn_addr] = 0;
            }
        };

        btn.addEventListener('mousedown', handleDown);
        btn.addEventListener('touchstart', handleDown);
        btn.addEventListener('mouseup', handleUp);
        btn.addEventListener('touchend', handleUp);
        btn.addEventListener('mouseleave', handleUp);

        toggle.addEventListener('change', (e) => {
            writeRegister(st.lamp_addr, e.target.checked ? 1 : 0);
        });
    });
}

function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        // 1. CẬP NHẬT MODBUS (Giao diện nút nhấn & đèn báo)
        if (data.regs) {
            const regs = data.regs; 

            stations.forEach(st => {
                const btnVal = regs[st.btn_addr - 1]; 
                const lampVal = regs[st.lamp_addr - 1]; 
                const lampOn = lampVal > 0;

                // Cập nhật giá trị hiển thị dạng text
                document.getElementById(`regVal-${st.btn_addr}`).textContent = btnVal;
                
                const btnEl = document.getElementById(`btn-${st.btn_addr}`);
                const lampValEl = document.getElementById(`lampVal-${st.lamp_addr}`);
                const toggleEl = document.getElementById(`toggle-${st.lamp_addr}`);

                // Đổi class để thay đổi giao diện Nút/Đèn
                if (lampOn) {
                    btnEl.className = 'illuminated-btn lamp-on';
                    lampValEl.textContent = 'ON';
                    lampValEl.style.color = '#34d399';
                    toggleEl.checked = true;
                } else {
                    btnEl.className = 'illuminated-btn lamp-off';
                    lampValEl.textContent = 'OFF';
                    lampValEl.style.color = '#047857';
                    toggleEl.checked = false;
                }
            });
        }

        // 2. CẬP NHẬT CHỈ SỐ KUKA (Pin và Trạng thái)
        if (data.kuka_data) {
            for (const [robotId, info] of Object.entries(data.kuka_data)) {
                const statusBadge = document.getElementById(`badge-status-${robotId}`);
                const battBadge = document.getElementById(`badge-batt-${robotId}`);
                
                if (statusBadge && battBadge) {
                    statusBadge.style.display = 'inline-block';
                    battBadge.style.display = 'inline-block';
                    
                    // Logic dịch số status thành chữ và màu sắc chuẩn KUKA
                    let statusText = `Mã ${info.status}`;
                    let statusColor = "#6b7280"; // Màu xám mặc định
                    
                    switch (info.status) {
                        case 1: statusText = "Khởi hành"; statusColor = "#3b82f6"; break;
                        case 2: statusText = "Ngoại tuyến"; statusColor = "#4b5563"; break;
                        case 3: statusText = "Rảnh"; statusColor = "#0ea5e9"; break;
                        case 4: statusText = "Đang chạy"; statusColor = "#10b981"; break;
                        case 5: statusText = "Đang sạc"; statusColor = "#eab308"; break;
                        case 6: statusText = "Cập nhật"; statusColor = "#8b5cf6"; break;
                        case 7: statusText = "Bất thường"; statusColor = "#ef4444"; break;
                    }
                    
                    // THÊM MỚI: Ghi đè trạng thái hiển thị nếu có cờ Tạm dừng ảo từ Backend
                    if (info.is_paused) {
                        statusText = "Tạm dừng";
                        statusColor = "#f59e0b"; // Vàng cam
                    }
                    
                    statusBadge.textContent = statusText;
                    statusBadge.style.backgroundColor = statusColor;
                    
                    // Logic màu sắc cho % Pin
                    battBadge.textContent = `🔋 ${info.battery}%`;
                    if (info.battery <= 20) {
                        battBadge.style.color = "#ef4444"; // Đỏ nếu pin yếu (<= 20%)
                    } else if (info.battery <= 50) {
                        battBadge.style.color = "#eab308"; // Vàng nếu pin trung bình (<= 50%)
                    } else {
                        battBadge.style.color = "#34d399"; // Xanh lá nếu pin khỏe
                    }
                }
            }
        }
    };
    
    // Tự động kết nối lại nếu bị đứt mạng
    ws.onclose = () => setTimeout(initWebSocket, 2000);
}

loadConfig().then(() => {
    initWebSocket();
});
