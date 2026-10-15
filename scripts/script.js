const container = document.getElementById('stations-container');
const emptyState = document.getElementById('empty-state');
const modal = document.getElementById('config-modal');
const modalTitle = document.getElementById('modal-title');

let stations = [];
let editModeAddr = null;

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
        document.getElementById('inp-group').value = st.group || "Chung"; 
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

    const pass = prompt("Vui lòng nhập mật khẩu quản trị để lưu thay đổi:");
    if (pass === null) return; 

    const payload = { 
        group: group, type: btnType, ip: ip, wf: wfCode, nodes: nodes, name: name, 
        btn_addr: btnAddr, lamp_addr: lampAddr, password: pass,
        phy_btn: isNaN(phyBtn) ? null : phyBtn,
        phy_lamp: isNaN(phyLamp) ? null : phyLamp
    };
    
    let res = await fetch(editModeAddr === null ? '/api/config' : `/api/config/${editModeAddr}`, {
        method: editModeAddr === null ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (data.status === "error") {
        alert(data.message);
        return; 
    }

    closeModal();
    loadConfig();
}

async function deleteStation(btnAddr) {
    if(confirm("Xóa NÚT này khỏi hệ thống? Dữ liệu không thể khôi phục.")) {
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

// GHI MODBUS VÀ LẮNG NGHE LỖI KUKA API
async function writeRegister(address, value) {
    try {
        const res = await fetch('/api/write', { 
            method: 'POST', 
            headers: { 'Content-Type': 'application/json' }, 
            body: JSON.stringify({ address: address, value: value }) 
        });
        
        const data = await res.json();
        
        // Nếu nhấn nút (value > 0) và server trả về lỗi KUKA API
        if (value > 0 && data.status === "error") {
            showToast("LỖI ĐIỀU KHIỂN: " + data.message);
            
            const btn = document.getElementById(`btn-${address}`);
            if(btn) {
                btn.style.transform = "translateX(-5px)";
                setTimeout(() => btn.style.transform = "translateX(5px)", 50);
                setTimeout(() => btn.style.transform = "translateX(-5px)", 100);
                setTimeout(() => btn.style.transform = "translateX(5px)", 150);
                setTimeout(() => btn.style.transform = "none", 200);
            }
        }
    } catch (e) {
        if (value > 0) showToast("LỖI MẠNG: Không thể kết nối tới Server Node!");
    }
}

function showToast(message) {
    const toast = document.createElement("div");
    toast.className = "toast-msg";
    toast.innerText = message;
    document.body.appendChild(toast);
    
    setTimeout(() => toast.classList.add("show"), 10);
    setTimeout(() => {
        toast.classList.remove("show");
        setTimeout(() => toast.remove(), 300);
    }, 3500);
}

// ==========================================
// TÍNH NĂNG QUẢN LÝ THEO NHÓM (ROBOT ID)
// ==========================================
async function editGroup(oldGroupName) {
    const newGroupName = prompt(`Nhập ID Robot mới để thay thế cho [${oldGroupName}]:`, oldGroupName);
    if (!newGroupName || newGroupName.trim() === "" || newGroupName === oldGroupName) return;

    const pass = prompt(`Vui lòng nhập mật khẩu quản trị để đổi tên toàn bộ nút của [${oldGroupName}]:`);
    if (pass === null) return;

    const stationsInGroup = stations.filter(s => s.group === oldGroupName);
    const updatePromises = stationsInGroup.map(st => {
        const payload = { ...st, group: newGroupName.trim(), password: pass };
        return fetch(`/api/config/${st.btn_addr}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).then(res => res.json());
    });

    const results = await Promise.all(updatePromises);
    const errorRes = results.find(r => r.status === "error");
    if (errorRes) alert(errorRes.message);
    
    loadConfig(); 
}

async function deleteGroup(groupName) {
    if(!confirm(`⚠️ BẠN CÓ CHẮC CHẮN MUỐN XÓA TOÀN BỘ ROBOT [${groupName}]?\nTất cả nút Start, Pause, Cancel của xe này sẽ bị xóa. Dữ liệu không thể khôi phục.`)) return;

    const pass = prompt(`Vui lòng nhập mật khẩu quản trị để XÓA Robot [${groupName}]:`);
    if (pass === null) return;

    const stationsInGroup = stations.filter(s => s.group === groupName);
    const deletePromises = stationsInGroup.map(st => {
        return fetch(`/api/config/${st.btn_addr}?password=${encodeURIComponent(pass)}`, { method: 'DELETE' }).then(res => res.json());
    });

    const results = await Promise.all(deletePromises);
    const errorRes = results.find(r => r.status === "error");
    if (errorRes) alert(errorRes.message);
    
    loadConfig();
}

// HÀM MỞ POPUP (CÓ NHẬP SỐ HÀNG X CỘT)
function openMiniWindow(robotId) {
    const grid = prompt(
        `Nhập bố cục Hàng x Cột cho Robot [${robotId}]\nVí dụ: 2x2 (2 hàng, 2 cột), hoặc 1x4 (1 hàng, 4 cột).\n(Để trống sẽ tự động xếp 1 hàng ngang):`, 
        "2x2" 
    );
    
    if (grid === null) return; 

    let url = window.location.origin + window.location.pathname + '?mini=' + encodeURIComponent(robotId);
    let width = 750;
    let height = 280;
    
    if (grid.trim() !== "") {
        const parts = grid.toLowerCase().split('x');
        if (parts.length === 2 && !isNaN(parts[0]) && !isNaN(parts[1])) {
            const rows = parseInt(parts[0]);
            const cols = parseInt(parts[1]);
            
            url += `&cols=${cols}`;
            width = cols * 180 + 40; 
            height = rows * 200 + 60; 
        } else {
            alert("Sai định dạng! Vui lòng nhập theo dạng số (VD: 2x2).");
            return;
        }
    }
    
    window.open(url, "_blank", `width=${width},height=${height},menubar=no,toolbar=no,location=no,status=no,resizable=yes`);
}

function renderStations() {
    let htmlContent = '';
    
    const urlParams = new URLSearchParams(window.location.search);
    const miniGroup = urlParams.get('mini');
    const cols = urlParams.get('cols'); 

    if (miniGroup) {
        document.body.classList.add('mini-mode');
        document.title = "ĐK: " + miniGroup;
        
        if (cols && !document.getElementById('grid-style')) {
            const style = document.createElement('style');
            style.id = 'grid-style';
            style.innerHTML = `
                body.mini-mode .group-items-container {
                    display: grid !important;
                    grid-template-columns: repeat(${cols}, 1fr) !important;
                    gap: 12px !important;
                }
                body.mini-mode .station-card {
                    max-width: none !important;
                    width: 100% !important;
                    margin: 0 !important;
                }
            `;
            document.head.appendChild(style);
        }
    }

    const groupedStations = {};
    stations.forEach(st => {
        const groupName = st.group || "Khu vực chung"; 
        if (!groupedStations[groupName]) groupedStations[groupName] = [];
        groupedStations[groupName].push(st);
    });

    const groupsToRender = miniGroup ? { [miniGroup]: groupedStations[miniGroup] || [] } : groupedStations;

    for (const [group, items] of Object.entries(groupsToRender)) {
        if (items.length === 0) continue;

        const groupActionsHtml = miniGroup ? '' : `
            <div style="display: flex; gap: 8px; align-items: center;">
                <button class="pop-out-btn" onclick="openMiniWindow('${group}')" style="margin:left: 0;">↗ MỞ POPUP</button>
                <button class="icon-btn edit-btn" style="width: 27px; height: 27px;" title="Đổi tên Robot ID cho toàn bộ nút" onclick="editGroup('${group}')">✎</button>
                <button class="icon-btn del-btn" style="width: 27px; height: 27px;" title="Xóa toàn bộ Robot này" onclick="deleteGroup('${group}')">✖</button>
            </div>
        `;

        htmlContent += `
        <div class="robot-group" style="${miniGroup ? 'margin:0; border:none; background:transparent; padding:0; box-shadow:none;' : ''}">
            <div class="group-header-wrapper" style="${miniGroup ? 'justify-content: center; border-bottom: none; margin-bottom: 10px;' : ''}">
                
                <h2 class="group-title" style="${miniGroup ? 'font-size: 20px; color: #fff;' : ''}">${group}</h2>
                
                <div style="display: flex; align-items: center; gap: 20px;">
                    ${groupActionsHtml}
                    <!-- KHU VỰC VẼ BADGES TÌNH TRẠNG KUKA -->
                    <div class="robot-badges">
                        <span id="badge-status-${group}" class="r-badge status-badge" style="display:none;"></span>
                        <span id="badge-batt-${group}" class="r-badge batt-badge" style="display:none;"></span>
                    </div>
                </div>

            </div>
            <div class="group-items-container">
        `;
        
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

    stations.forEach(st => {
        const btn = document.getElementById(`btn-${st.btn_addr}`);
        if(!btn) return; 
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

        if(toggle) {
            toggle.addEventListener('change', (e) => {
                writeRegister(st.lamp_addr, e.target.checked ? 1 : 0);
            });
        }
    });
}

function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        // 1. CẬP NHẬT MODBUS
        if (data.regs) {
            const regs = data.regs; 

            stations.forEach(st => {
                const btnVal = regs[st.btn_addr - 1]; 
                const lampVal = regs[st.lamp_addr - 1]; 
                const lampOn = lampVal > 0;

                const regValEl = document.getElementById(`regVal-${st.btn_addr}`);
                if(regValEl) regValEl.textContent = btnVal;
                
                const btnEl = document.getElementById(`btn-${st.btn_addr}`);
                const lampValEl = document.getElementById(`lampVal-${st.lamp_addr}`);
                const toggleEl = document.getElementById(`toggle-${st.lamp_addr}`);

                if(!btnEl) return; 

                if (lampOn) {
                    btnEl.className = 'illuminated-btn lamp-on';
                    if(lampValEl) {
                        lampValEl.textContent = 'ON';
                        lampValEl.style.color = '#34d399';
                    }
                    if(toggleEl) toggleEl.checked = true;
                } else {
                    btnEl.className = 'illuminated-btn lamp-off';
                    if(lampValEl) {
                        instruction = 'OFF';
                        lampValEl.textContent = 'OFF';
                        lampValEl.style.color = '#047857';
                    }
                    if(toggleEl) toggleEl.checked = false;
                }
            });
        }

        // 2. CẬP NHẬT CHỈ SỐ KUKA
        if (data.kuka_data) {
            for (const [robotId, info] of Object.entries(data.kuka_data)) {
                const statusBadge = document.getElementById(`badge-status-${robotId}`);
                const battBadge = document.getElementById(`badge-batt-${robotId}`);
                
                if (statusBadge && battBadge) {
                    statusBadge.style.display = 'inline-block';
                    
                    let statusText = `Mã ${info.status}`;
                    let statusColor = "#6b7280"; 
                    
                    switch (info.status) {
                        case 0: statusText = "Mất kết nối"; statusColor = "#ef4444"; break; 
                        case 1: statusText = "Khởi hành"; statusColor = "#3b82f6"; break;
                        case 2: statusText = "Ngoại tuyến"; statusColor = "#4b5563"; break;
                        case 3: statusText = "Rảnh"; statusColor = "#0ea5e9"; break;
                        case 4: statusText = "Đang chạy"; statusColor = "#10b981"; break;
                        case 5: statusText = "Đang sạc"; statusColor = "#eab308"; break;
                        case 6: statusText = "Cập nhật"; statusColor = "#8b5cf6"; break;
                        case 7: statusText = "Bất thường"; statusColor = "#ef4444"; break;
                    }
                    
                    if (info.is_paused && info.status !== 0) {
                        statusText = "Tạm dừng";
                        statusColor = "#f59e0b"; 
                    }
                    
                    statusBadge.textContent = statusText;
                    statusBadge.style.backgroundColor = statusColor;
                    
                    if (info.status === 0) {
                        battBadge.style.display = 'none';
                    } else {
                        battBadge.style.display = 'inline-block';
                        let battColor = "#34d399"; 
                        if (info.battery <= 20) {
                            battColor = "#ef4444"; 
                        } else if (info.battery <= 50) {
                            battColor = "#eab308"; 
                        }

                        const battSvg = `<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" style="vertical-align: text-bottom; margin-right: 4px;"><path d="M17 6H3c-1.1 0-2 .9-2 2v8c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2v-8c0-1.1-.9-2-2-2zm4 4v4h-2v-4h2z"/></svg>`;
                        battBadge.innerHTML = `${battSvg}${info.battery}%`;
                        battBadge.style.color = battColor;
                        battBadge.style.borderColor = battColor;
                    }
                }
            }
        }
    };
    
    ws.onclose = () => setTimeout(initWebSocket, 2000);
}

loadConfig().then(() => {
    initWebSocket();
});
