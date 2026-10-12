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
        // Trạng thái: SỬA TRẠM
        editModeAddr = addr;
        modalTitle.textContent = "SỬA THÔNG TIN TRẠM";
        const st = stations.find(s => s.btn_addr === addr);
        document.getElementById('inp-name').value = st.name;
        document.getElementById('inp-btn').value = st.btn_addr;
        document.getElementById('inp-lamp').value = st.lamp_addr;
    } else {
        // Trạng thái: TẠO MỚI
        editModeAddr = null;
        modalTitle.textContent = "TẠO TRẠM MỚI";
        document.getElementById('inp-name').value = '';
        document.getElementById('inp-btn').value = '';
        document.getElementById('inp-lamp').value = '';
    }
}

function closeModal() {
    modal.style.display = 'none';
    editModeAddr = null;
}

// GỌI API LƯU HOẶC SỬA
async function saveStation() {
    const name = document.getElementById('inp-name').value.trim();
    const btnAddr = parseInt(document.getElementById('inp-btn').value);
    const lampAddr = parseInt(document.getElementById('inp-lamp').value);

    if(!name || isNaN(btnAddr) || isNaN(lampAddr)) return alert("Vui lòng nhập đầy đủ thông tin!");
    if(btnAddr < 1 || btnAddr > 50) return alert("Địa chỉ Nút phải từ 1 đến 50!");
    if(lampAddr < 51 || lampAddr > 100) return alert("Địa chỉ Đèn phải từ 51 đến 100!");

    // Check trùng lặp (Bỏ qua chính nó nếu đang ở chế độ Sửa)
    const isBtnExists = stations.find(s => s.btn_addr === btnAddr && s.btn_addr !== editModeAddr);
    const isLampExists = stations.find(s => s.lamp_addr === lampAddr && s.btn_addr !== editModeAddr);
    
    if(isBtnExists) return alert("Địa chỉ Nút này đã được sử dụng!");
    if(isLampExists) return alert("Địa chỉ Đèn này đã được sử dụng!");

    const payload = { name: name, btn_addr: btnAddr, lamp_addr: lampAddr };

    if (editModeAddr === null) {
        // THÊM MỚI (POST)
        await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
    } else {
        // CẬP NHẬT (PUT)
        await fetch(`/api/config/${editModeAddr}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
    }

    closeModal();
    loadConfig();
}

// GỌI API XÓA
async function deleteStation(btnAddr) {
    if(confirm("Xóa trạm này khỏi hệ thống? Dữ liệu không thể khôi phục.")) {
        await fetch(`/api/config/${btnAddr}`, { method: 'DELETE' });
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
    stations.forEach(st => {
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
    container.innerHTML = htmlContent;

    // Gắn sự kiện click
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
        const regs = data.regs; 

        stations.forEach(st => {
            const btnVal = regs[st.btn_addr - 1]; 
            const lampVal = regs[st.lamp_addr - 1]; 
            const lampOn = lampVal > 0;

            document.getElementById(`regVal-${st.btn_addr}`).textContent = btnVal;
            const btnEl = document.getElementById(`btn-${st.btn_addr}`);
            const lampValEl = document.getElementById(`lampVal-${st.lamp_addr}`);
            const toggleEl = document.getElementById(`toggle-${st.lamp_addr}`);

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
    };
    ws.onclose = () => setTimeout(initWebSocket, 2000);
}

loadConfig().then(() => {
    initWebSocket();
});
