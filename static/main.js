// Tunneler - Main Entry Point
// Contains lobby UI, WebSocket handling, and lobby animation
// Depends on: game.js (must be loaded first)

// ========== ROOM STATE ==========
// Note: 'room' and 'playerId' variables are defined in the HTML template via Django
// Player ID is in the URL, so reloading preserves it
let playerName = null;
let ws = null;
let lobbyPlayers = {};

// ========== GAME SETTINGS ==========
const DEFAULT_SETTINGS = {
    tankSpeed: 30,
    digSpeed: 10,
    bulletSpeed: 60,
    fireDelay: 150,        // ms
    shotDamage: 14,        // %
    startAmmo: 30,
    grenadeCostAmmo: 20,
    grenadeDamage: 84,     // %
    grenadeRadius: 8,
    pickupAmmo: 10,
    pickupCount: 50,
    repairShieldOut: 90,   // sec
    repairShieldIn: 6,     // sec
    deathTime: 4,          // sec
};

let gameSettings = { ...DEFAULT_SETTINGS };

function loadSettings() {
    try {
        const saved = localStorage.getItem('tunneler_settings');
        if (saved) {
            const parsed = JSON.parse(saved);
            gameSettings = { ...DEFAULT_SETTINGS, ...parsed };
        }
    } catch (e) {
        console.error('Failed to load settings:', e);
    }
    updateSettingsUI();
}

function saveSettings() {
    try {
        localStorage.setItem('tunneler_settings', JSON.stringify(gameSettings));
    } catch (e) {
        console.error('Failed to save settings:', e);
    }
}

function updateSettingsUI() {
    const fields = {
        'set-tank-speed': 'tankSpeed',
        'set-dig-speed': 'digSpeed',
        'set-bullet-speed': 'bulletSpeed',
        'set-fire-delay': 'fireDelay',
        'set-shot-damage': 'shotDamage',
        'set-start-ammo': 'startAmmo',
        'set-grenade-cost-ammo': 'grenadeCostAmmo',
        'set-grenade-damage': 'grenadeDamage',
        'set-grenade-radius': 'grenadeRadius',
        'set-pickup-ammo': 'pickupAmmo',
        'set-pickup-count': 'pickupCount',
        'set-repair-shield-out': 'repairShieldOut',
        'set-repair-shield-in': 'repairShieldIn',
        'set-death-time': 'deathTime',
    };
    for (const [id, key] of Object.entries(fields)) {
        const el = document.getElementById(id);
        if (el) el.value = gameSettings[key];
    }
}

function readSettingsFromUI() {
    const fields = {
        'set-tank-speed': 'tankSpeed',
        'set-dig-speed': 'digSpeed',
        'set-bullet-speed': 'bulletSpeed',
        'set-fire-delay': 'fireDelay',
        'set-shot-damage': 'shotDamage',
        'set-start-ammo': 'startAmmo',
        'set-grenade-cost-ammo': 'grenadeCostAmmo',
        'set-grenade-damage': 'grenadeDamage',
        'set-grenade-radius': 'grenadeRadius',
        'set-pickup-ammo': 'pickupAmmo',
        'set-pickup-count': 'pickupCount',
        'set-repair-shield-out': 'repairShieldOut',
        'set-repair-shield-in': 'repairShieldIn',
        'set-death-time': 'deathTime',
    };
    for (const [id, key] of Object.entries(fields)) {
        const el = document.getElementById(id);
        if (el) gameSettings[key] = parseFloat(el.value) || DEFAULT_SETTINGS[key];
    }
    saveSettings();
}

function openSettings() {
    updateSettingsUI();
    document.getElementById('settings-modal').classList.remove('hidden');
}

function closeSettings() {
    readSettingsFromUI();
    document.getElementById('settings-modal').classList.add('hidden');
}

function resetSettings() {
    gameSettings = { ...DEFAULT_SETTINGS };
    updateSettingsUI();
    saveSettings();
}

// ========== TIMING & LOGGING ==========
let t0 = null;
function log(msg) {
    if (t0 === null) t0 = performance.now();
    console.log(`[${((performance.now() - t0) / 1000).toFixed(3)}s] ${msg}`);
}

// ========== LOBBY FUNCTIONS ==========
function copyRoomLink() {
    // Copy room-only URL (without player param) so others get their own player ID
    const url = new URL(window.location.href);
    url.searchParams.delete('player');
    const text = url.toString();

    // Try modern clipboard API first, fall back to execCommand
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
            showCopyFeedback(true);
        }).catch(() => {
            fallbackCopy(text);
        });
    } else {
        fallbackCopy(text);
    }
}

function fallbackCopy(text) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    try {
        document.execCommand('copy');
        showCopyFeedback(true);
    } catch (e) {
        showCopyFeedback(false);
    }
    document.body.removeChild(textarea);
}

function showCopyFeedback(success) {
    const btn = document.querySelector('#room-link button');
    if (!btn) return;
    const original = btn.textContent;
    btn.textContent = success ? 'COPIED!' : 'FAILED';
    setTimeout(() => { btn.textContent = original; }, 1500);
}

function joinRoom() {
    const nameInput = document.getElementById('name-input');
    const name = nameInput.value.trim().toUpperCase();
    if (!name) { alert('Please enter your name'); return; }
    playerName = name;
    document.getElementById('name-form').classList.add('hidden');
    document.getElementById('waiting-room').classList.remove('hidden');
    connectWebSocket();
}

function updatePlayerList(playersData, hostId) {
    const list = document.getElementById('player-list');
    list.innerHTML = '';
    isHost = (hostId === playerId);

    for (const [pid, player] of Object.entries(playersData)) {
        const li = document.createElement('li');
        li.textContent = player.name;
        if (pid === hostId) li.classList.add('host');
        if (player.is_bot) li.classList.add('bot');
        list.appendChild(li);
    }

    const hostButtons = document.getElementById('host-buttons');
    const status = document.getElementById('status');
    if (isHost) {
        hostButtons.classList.remove('hidden');
        status.textContent = '';
    } else {
        hostButtons.classList.add('hidden');
        status.textContent = 'WAITING FOR HOST...';
    }
}

function startGame() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        t0 = performance.now();
        log('sending start_game with settings');
        ws.send(JSON.stringify({ type: 'start_game', room, player_id: playerId, settings: gameSettings }));
    }
}

function addBot() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        log('requesting bot');
        ws.send(JSON.stringify({ type: 'add_bot', room, player_id: playerId }));
    }
}

function showGameArea() {
    log('showGameArea');
    document.getElementById('lobby').classList.add('hidden');
    document.getElementById('game-area').classList.remove('hidden');
    setTimeout(() => initGame(), 100);
}

// ========== WEBSOCKET ==========
function connectWebSocket() {
    const ws_scheme = window.location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(ws_scheme + '://' + window.location.host + '/ws/projects/tunneler');

    ws.onopen = function() {
        ws.send(JSON.stringify({ type: 'join', room, player_id: playerId, name: playerName }));
    };

    ws.onclose = function(e) { console.log('WebSocket closed:', e.code); };
    ws.onerror = function(e) { console.error('WebSocket error:', e); };

    ws.onmessage = function(e) {
        const data = JSON.parse(e.data);

        if (data.type === 'player_list') {
            updatePlayerList(data.players, data.host);
            lobbyPlayers = data.players;
        } else if (data.type === 'room_full') {
            alert('Room is full');
            document.getElementById('name-form').innerHTML = '<p style="color: var(--retro-accent);">ROOM FULL</p>';
        } else if (data.type === 'game_in_progress') {
            alert('Game already started');
            document.getElementById('name-form').innerHTML = '<p style="color: var(--retro-accent);">GAME IN PROGRESS</p>';
        } else if (data.type === 'game_started') {
            log('game_started received' + (data.rejoined ? ' (rejoin)' : ''));
            myPlayerId = data.player_numbers[playerId];
            // Use server-provided players data for rejoin, fall back to lobbyPlayers
            const playersData = data.players || lobbyPlayers;
            players = {};
            for (const [pid, num] of Object.entries(data.player_numbers)) {
                players[pid] = { name: playersData[pid]?.name || 'P' + num, number: num };
            }
            field = unpackField(data.field_packed);
            tanks = data.tanks;
            bullets = data.bullets || [];
            grenades = data.grenades || [];
            pickups = data.pickups || [];
            explosions = [];
            showGameArea();
        } else if (data.type === 'state_update') {
            stateUpdateCount++;
            lastStateUpdate = performance.now();
            tanks = data.tanks;
            bullets = data.bullets || [];
            grenades = data.grenades || [];
            explosions = data.explosions || [];
            if (data.pickups) pickups = data.pickups;

            // Update host status from server
            if (data.host) {
                const wasHost = isHost;
                isHost = (data.host === playerId);
                if (isHost && !wasHost) log('became host');
                if (!isHost && wasHost) log('no longer host');
            }

            if (data.changes) {
                for (const [x, y, val] of data.changes) {
                    if (field[y]) field[y][x] = val;
                }
            }

            processAcks(data.acks);
            scheduleRender();
        } else if (data.type === 'game_over') {
            tanks = data.tanks;
            gameRunning = false;
            if (tickWorker) { tickWorker.terminate(); tickWorker = null; }
            showWinnerOverlay(data.winner);
        } else if (data.type === 'return_to_lobby') {
            hideWinnerOverlay();
            gameRunning = false;
            if (tickWorker) { tickWorker.terminate(); tickWorker = null; }
            document.getElementById('game-area').classList.add('hidden');
            document.getElementById('lobby').classList.remove('hidden');
            updatePlayerList(data.players, data.host);
            lobbyPlayers = data.players;
        }
    };
}

// Track last state update for host takeover
let lastStateUpdate = 0;

// ========== INPUT & LATENCY ==========
let inputSeq = 0;
const pendingAcks = {};
let latencyMs = null;
let lastLatencyUpdate = 0;

function sendInput() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        inputSeq++;
        const msgId = `${playerId}-${inputSeq}`;
        pendingAcks[msgId] = performance.now();
        ws.send(JSON.stringify({ type: 'input', room, player_id: playerId, keys, msg_id: msgId }));
    }
}

function processAcks(acks) {
    if (!acks || acks.length === 0) return;
    const now = performance.now();
    for (const msgId of acks) {
        if (pendingAcks[msgId]) {
            latencyMs = now - pendingAcks[msgId];
            lastLatencyUpdate = now;
            delete pendingAcks[msgId];
            log(`LATENCY: ${Math.round(latencyMs)}ms (from ${msgId})`);
        }
    }
    // Clean up old pending acks
    for (const [id, ts] of Object.entries(pendingAcks)) {
        if (now - ts > 5000) delete pendingAcks[id];
    }
    updateLatencyDisplay();
}

function updateLatencyDisplay() {
    const el = document.getElementById('latency-display');
    if (!el) return;

    const now = performance.now();
    if (latencyMs !== null) {
        el.textContent = `PING: ${Math.round(latencyMs)}ms`;
        el.classList.toggle('stale', now - lastLatencyUpdate > 3000);
    }
}

// ========== WINNER OVERLAY ==========
function showWinnerOverlay(winnerNum) {
    // Find winner name
    let winnerName = 'P' + winnerNum;
    for (const [pid, p] of Object.entries(players)) {
        if (p.number === winnerNum) { winnerName = p.name; break; }
    }

    // Build scoreboard sorted by kills
    const sorted = Object.entries(players).sort((a, b) => {
        const ta = tanks[a[1].number] || {};
        const tb = tanks[b[1].number] || {};
        return (tb.kills || 0) - (ta.kills || 0);
    });
    const rows = sorted.map(([pid, p]) => {
        const t = tanks[p.number] || {};
        const isWinner = p.number === winnerNum;
        return `<div class="score-row ${isWinner ? 'winner' : ''}">
            <div class="score-color" style="background: ${PLAYER_COLORS[p.number]?.body || '#888'}"></div>
            <span class="score-name">${p.name}</span>
            <span class="score-kills">${t.kills || 0} KILLS</span>
        </div>`;
    }).join('');

    const newGameBtn = isHost ? `<button class="new-game-btn" onclick="requestNewGame()">NEW GAME</button>` : `<div class="waiting-host">WAITING FOR HOST...</div>`;

    const overlay = document.createElement('div');
    overlay.id = 'winner-overlay';
    overlay.innerHTML = `
        <div class="winner-panel">
            <h2>${winnerName} WINS!</h2>
            <div class="scoreboard">${rows}</div>
            ${newGameBtn}
        </div>
    `;
    document.getElementById('game-container').appendChild(overlay);
}

function hideWinnerOverlay() {
    const overlay = document.getElementById('winner-overlay');
    if (overlay) overlay.remove();
}

function requestNewGame() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'new_game', room, player_id: playerId }));
    }
}

// ========== HOST TICK ==========
let tickSeq = 0;
let stateUpdateCount = 0;
let lastStatsTime = 0;

function sendTick() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        tickSeq++;
        const now = performance.now();

        // Log stats every 5 seconds (less spam)
        if (now - lastStatsTime > 5000) {
            const elapsed = (now - lastStatsTime) / 1000;
            const ups = (stateUpdateCount / elapsed).toFixed(1);
            log(`STATS: ${ups} updates/s, latency=${latencyMs ? Math.round(latencyMs) + 'ms' : '--'}`);
            stateUpdateCount = 0;
            lastStatsTime = now;
        }

        ws.send(JSON.stringify({ type: 'tick', room, player_id: playerId }));
    }
}

// ========== EVENT LISTENERS ==========
window.addEventListener('beforeunload', function() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'leave', room, player_id: playerId }));
    }
});

document.addEventListener('DOMContentLoaded', function() {
    document.getElementById('name-input').addEventListener('keypress', function(e) {
        if (e.key === 'Enter') joinRoom();
    });

    // Load settings from localStorage
    loadSettings();

    // Initialize lobby background animation
    initLobbyAnimation();

    // Scale game to fit viewport
    scaleGameToFit();
    window.addEventListener('resize', scaleGameToFit);

    // Try to auto-rejoin if we have a player ID in URL
    tryAutoRejoin();
});

function scaleGameToFit() {
    const container = document.getElementById('game-container');
    if (!container) return;

    const gameWidth = 1024;
    const gameHeight = 768;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    // Calculate scale to fit viewport
    const scaleX = viewportWidth / gameWidth;
    const scaleY = viewportHeight / gameHeight;
    const scale = Math.min(scaleX, scaleY, 1);  // Don't scale up beyond 1

    container.style.transform = `scale(${scale})`;
    container.style.transformOrigin = 'top center';

    // Center vertically if there's extra space
    if (scale < 1) {
        const scaledHeight = gameHeight * scale;
        const offsetY = Math.max(0, (viewportHeight - scaledHeight) / 2);
        container.style.marginTop = `${offsetY}px`;
    } else {
        container.style.marginTop = '0';
    }
}

function tryAutoRejoin() {
    const ws_scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const rejoinWs = new WebSocket(ws_scheme + '://' + window.location.host + '/ws/projects/tunneler');

    rejoinWs.onopen = function() {
        rejoinWs.send(JSON.stringify({ type: 'rejoin', room, player_id: playerId }));
    };

    rejoinWs.onmessage = function(e) {
        const data = JSON.parse(e.data);
        if (data.type === 'game_started') {
            // We're in an active game - rejoin it
            log('auto-rejoin successful');
            ws = rejoinWs;  // Use this connection
            myPlayerId = data.player_numbers[playerId];
            isHost = data.host === playerId;
            const playersData = data.players || {};
            players = {};
            for (const [pid, num] of Object.entries(data.player_numbers)) {
                players[pid] = { name: playersData[pid]?.name || 'P' + num, number: num };
            }
            field = unpackField(data.field_packed);
            tanks = data.tanks;
            bullets = data.bullets || [];
            grenades = data.grenades || [];
            pickups = data.pickups || [];
            explosions = [];
            showGameArea();
            // Set up remaining handlers
            rejoinWs.onclose = function(e) { console.log('WebSocket closed:', e.code); };
            rejoinWs.onerror = function(e) { console.error('WebSocket error:', e); };
            rejoinWs.onmessage = handleGameMessage;
        } else {
            // Not in a game, close this connection and wait for manual join
            rejoinWs.close();
        }
    };

    rejoinWs.onerror = function() {
        // Connection failed, just show lobby normally
    };
}

function handleGameMessage(e) {
    const data = JSON.parse(e.data);
    if (data.type === 'state_update') {
        stateUpdateCount++;
        lastStateUpdate = performance.now();
        tanks = data.tanks;
        bullets = data.bullets || [];
        grenades = data.grenades || [];
        explosions = data.explosions || [];
        if (data.pickups) pickups = data.pickups;

        // Update host status from server
        if (data.host) {
            const wasHost = isHost;
            isHost = (data.host === playerId);
            if (isHost && !wasHost) log('became host');
            if (!isHost && wasHost) log('no longer host');
        }

        if (data.changes) {
            for (const [x, y, val] of data.changes) {
                if (field[y]) field[y][x] = val;
            }
        }
        processAcks(data.acks);
        scheduleRender();
    } else if (data.type === 'game_over') {
        tanks = data.tanks;
        gameRunning = false;
        if (tickWorker) { tickWorker.terminate(); tickWorker = null; }
        showWinnerOverlay(data.winner);
    } else if (data.type === 'return_to_lobby') {
        hideWinnerOverlay();
        gameRunning = false;
        if (tickWorker) { tickWorker.terminate(); tickWorker = null; }
        document.getElementById('game-area').classList.add('hidden');
        document.getElementById('lobby').classList.remove('hidden');
        updatePlayerList(data.players, data.host);
        lobbyPlayers = data.players;
    }
}

// ========== LOBBY BACKGROUND ANIMATION ==========
function initLobbyAnimation() {
    const canvas = document.getElementById('lobby-bg');
    if (!canvas) return;

    canvas.width = 1024;
    canvas.height = 768;
    const ctx = canvas.getContext('2d');

    const ROCK = '#696969';
    const DIRT1 = '#8B4513';
    const DIRT2 = '#A0522D';
    const EMPTY = '#000000';
    const PIXEL = 8;

    const cols = Math.ceil(1024 / PIXEL);
    const rows = Math.ceil(768 / PIXEL);
    const border = 4;

    // Tunnel parameters
    const tunnelY = rows - 12;  // 12 pixels from bottom
    const tunnelHeight = 7;     // Tank is 7 pixels tall

    // Store the background pattern
    const bgPattern = [];

    // Use a seeded random for consistency
    let seed = 12345;
    const seededRandom = () => {
        seed = (seed * 1103515245 + 12345) & 0x7fffffff;
        return seed / 0x7fffffff;
    };

    // Generate background pattern
    for (let y = 0; y < rows; y++) {
        bgPattern[y] = [];
        for (let x = 0; x < cols; x++) {
            // Tunnel cuts through everything (including rock borders)
            if (y >= tunnelY && y < tunnelY + tunnelHeight) {
                bgPattern[y][x] = EMPTY;
            } else if (x < border || x >= cols - border || y < border || y >= rows - border) {
                bgPattern[y][x] = ROCK;
            } else {
                bgPattern[y][x] = seededRandom() < 0.5 ? DIRT1 : DIRT2;
            }
        }
    }

    // Add a random rock formation in the upper right area
    const rockCenterX = cols - 25;
    const rockCenterY = 20;
    for (let dy = -8; dy <= 8; dy++) {
        for (let dx = -8; dx <= 8; dx++) {
            const dist = dx * dx + dy * dy;
            if (dist < 64 && seededRandom() < 0.7) {
                const rx = rockCenterX + dx;
                const ry = rockCenterY + dy;
                if (rx >= border && rx < cols - border && ry >= border && ry < rows - border) {
                    bgPattern[ry][rx] = ROCK;
                }
            }
        }
    }

    // Draw static background
    function drawBackground() {
        for (let y = 0; y < rows; y++) {
            for (let x = 0; x < cols; x++) {
                ctx.fillStyle = bgPattern[y][x];
                ctx.fillRect(x * PIXEL, y * PIXEL, PIXEL, PIXEL);
            }
        }
    }

    // Tank colors (same as game)
    const tankColors = [
        { body: '#e74c3c', track: '#c0392b', barrel: '#ff6b6b' },
        { body: '#3498db', track: '#2980b9', barrel: '#5dade2' },
        { body: '#2ecc71', track: '#27ae60', barrel: '#58d68d' },
        { body: '#f39c12', track: '#d68910', barrel: '#f5b041' },
    ];

    const LOBBY_TANK_SPRITE = [
        [0,0,0,0,0,0,0],
        [2,2,2,2,2,2,0],
        [0,1,1,1,1,0,0],
        [0,1,1,3,3,3,3],
        [0,1,1,1,1,0,0],
        [2,2,2,2,2,2,0],
        [0,0,0,0,0,0,0]
    ];

    function drawTankAt(x, y, rotation, colors) {
        const colorMap = { 1: colors.body, 2: colors.track, 3: colors.barrel };
        const angle = rotation * Math.PI / 4;

        for (let sy = 0; sy < 7; sy++) {
            for (let sx = 0; sx < 7; sx++) {
                const val = LOBBY_TANK_SPRITE[sy][sx];
                if (val === 0) continue;
                const px = sx - 3, py = sy - 3;
                const rx = Math.round(px * Math.cos(angle) - py * Math.sin(angle));
                const ry = Math.round(px * Math.sin(angle) + py * Math.cos(angle));
                ctx.fillStyle = colorMap[val];
                ctx.fillRect((x + rx) * PIXEL, (y + ry) * PIXEL, PIXEL, PIXEL);
            }
        }
    }

    // Animation state
    let tankX = -10;
    let tankColor = tankColors[0];
    let tankRotation = 0;  // 0 = facing right
    let goingRight = true;
    let waitTime = 0;

    function animate() {
        // Don't animate if lobby is hidden
        if (document.getElementById('lobby').classList.contains('hidden')) {
            requestAnimationFrame(animate);
            return;
        }

        drawBackground();

        if (waitTime > 0) {
            waitTime--;
        } else {
            // Draw tank in tunnel (use Math.floor to avoid fractional pixel artifacts)
            const tankY = tunnelY + Math.floor(tunnelHeight / 2);
            drawTankAt(Math.floor(tankX), tankY, tankRotation, tankColor);

            // Move tank
            if (goingRight) {
                tankX += 0.3;
                if (tankX > cols + 10) {
                    // Pick random wait time and new direction/color
                    waitTime = Math.floor(Math.random() * 120) + 60;  // 1-3 seconds at 60fps
                    goingRight = Math.random() < 0.5;
                    tankX = goingRight ? -10 : cols + 10;
                    tankRotation = goingRight ? 0 : 4;
                    tankColor = tankColors[Math.floor(Math.random() * tankColors.length)];
                }
            } else {
                tankX -= 0.3;
                if (tankX < -10) {
                    waitTime = Math.floor(Math.random() * 120) + 60;
                    goingRight = Math.random() < 0.5;
                    tankX = goingRight ? -10 : cols + 10;
                    tankRotation = goingRight ? 0 : 4;
                    tankColor = tankColors[Math.floor(Math.random() * tankColors.length)];
                }
            }
        }

        requestAnimationFrame(animate);
    }

    // Start animation
    drawBackground();
    animate();
}
