// Tunneler - Game Engine
// Contains rendering, game state, and input handling

// ========== CONSTANTS ==========
const FIELD_WIDTH = 640;
const FIELD_HEIGHT = 480;
const VIEW_WIDTH = 112;  // 896/8 = 112, exact 8x pixel scale
const VIEW_HEIGHT = 75;
const MAIN_CANVAS_WIDTH = 896;  // 112 * 8 = exact 8x scale
const MAIN_CANVAS_HEIGHT = 600;
const MINI_CANVAS_WIDTH = 224;   // 112 * 2 = exact 2x scale
const MINI_CANVAS_HEIGHT = 150;  // 75 * 2 = exact 2x scale
const TICK_MS = 33;

// Terrain types
const TERRAIN_EMPTY = 0;
const TERRAIN_DIRT1 = 8;
const TERRAIN_DIRT2 = 9;
const TERRAIN_ROCK = 10;
const TERRAIN_WALL = 30;

// Direction tables (8 directions, 0=right, 2=down, 4=left, 6=up)
const DIR_X = [1, 0.707, 0, -0.707, -1, -0.707, 0, 0.707];
const DIR_Y = [0, 0.707, 1, 0.707, 0, -0.707, -1, -0.707];

// Tank sprite (7x7 grid: 0=transparent, 1=body, 2=track, 3=barrel)
const TANK_SPRITE = [
    [0,0,0,0,0,0,0],
    [2,2,2,2,2,2,0],
    [0,1,1,1,1,0,0],
    [0,1,1,3,3,3,3],
    [0,1,1,1,1,0,0],
    [2,2,2,2,2,2,0],
    [0,0,0,0,0,0,0]
];

// Player colors (1-4)
const PLAYER_COLORS = {
    1: { body: '#e74c3c', track: '#c0392b', barrel: '#ff6b6b' },
    2: { body: '#3498db', track: '#2980b9', barrel: '#5dade2' },
    3: { body: '#2ecc71', track: '#27ae60', barrel: '#58d68d' },
    4: { body: '#f39c12', track: '#d68910', barrel: '#f5b041' },
};

// Terrain colors
const TERRAIN_COLORS = {
    [TERRAIN_EMPTY]: '#000000',
    [TERRAIN_DIRT1]: '#8B4513',
    [TERRAIN_DIRT2]: '#A0522D',
    [TERRAIN_ROCK]: '#696969',
};

// ========== GAME STATE ==========
let field = [];
let tanks = {};
let bullets = [];
let grenades = [];
let explosions = [];  // Visual only, cleared each frame
let pickups = [];     // Ammo pickups from server
let myPlayerId = 1;
let tickWorker = null;
let renderPending = false;
let gameRunning = false;

// Input state
let keys = { up: false, down: false, left: false, right: false, fire: false, grenade: false, turbo: false };
let lastSentKeys = null;

// Canvas references
let mainCanvas, mainCtx;
let miniCanvases = {};

// Player info
let players = {};
let isHost = false;

// ========== FIELD UNPACKING ==========
function unpackField(packed) {
    const field = [];
    let i = 0;
    for (let y = 0; y < FIELD_HEIGHT; y++) {
        const row = [];
        for (let x = 0; x < FIELD_WIDTH; x++) {
            const c = packed[i++];
            if (c === '.') row.push(TERRAIN_EMPTY);
            else if (c === 'd') row.push(TERRAIN_DIRT1);
            else if (c === 'D') row.push(TERRAIN_DIRT2);
            else if (c === '#') row.push(TERRAIN_ROCK);
            else if (c >= '1' && c <= '4') row.push(TERRAIN_WALL + (parseInt(c) - 1) * 10);
            else row.push(TERRAIN_EMPTY);
        }
        field.push(row);
    }
    return field;
}

// ========== RENDERING ==========
function getTerrainColor(t) {
    if (t >= TERRAIN_WALL) {
        const pnum = Math.floor((t - TERRAIN_WALL) / 10) + 1;
        return PLAYER_COLORS[pnum]?.body || '#888';
    }
    return TERRAIN_COLORS[t] || '#000';
}

function drawTank(ctx, x, y, rotation, playerNum, scale) {
    const colors = PLAYER_COLORS[playerNum] || PLAYER_COLORS[1];
    const colorMap = { 1: colors.body, 2: colors.track, 3: colors.barrel };

    for (let sy = 0; sy < 7; sy++) {
        for (let sx = 0; sx < 7; sx++) {
            const val = TANK_SPRITE[sy][sx];
            if (val === 0) continue;
            const px = sx - 3, py = sy - 3;
            const angle = rotation * Math.PI / 4;
            const rx = Math.round(px * Math.cos(angle) - py * Math.sin(angle));
            const ry = Math.round(px * Math.sin(angle) + py * Math.cos(angle));
            ctx.fillStyle = colorMap[val];
            ctx.fillRect((x + rx) * scale, (y + ry) * scale, scale, scale);
        }
    }
}

function renderView(ctx, centerX, centerY, viewW, viewH, scale) {
    const halfW = Math.floor(viewW / 2);
    const halfH = Math.floor(viewH / 2);

    // Terrain (world wraps)
    for (let dy = -halfH; dy <= halfH; dy++) {
        for (let dx = -halfW; dx <= halfW; dx++) {
            const fx = ((Math.floor(centerX + dx) % FIELD_WIDTH) + FIELD_WIDTH) % FIELD_WIDTH;
            const fy = ((Math.floor(centerY + dy) % FIELD_HEIGHT) + FIELD_HEIGHT) % FIELD_HEIGHT;
            ctx.fillStyle = getTerrainColor(field[fy][fx]);
            ctx.fillRect((dx + halfW) * scale, (dy + halfH) * scale, scale, scale);
        }
    }

    // Helper: shortest wrapped offset
    function wrapDelta(val, size) {
        let d = val % size;
        if (d > size / 2) d -= size;
        if (d < -size / 2) d += size;
        return d;
    }

    // Bullets
    for (const b of bullets) {
        const bx = Math.floor(wrapDelta(b.x - centerX, FIELD_WIDTH)) + halfW;
        const by = Math.floor(wrapDelta(b.y - centerY, FIELD_HEIGHT)) + halfH;
        if (bx >= 0 && bx < viewW && by >= 0 && by < viewH) {
            ctx.fillStyle = '#ffff00';
            ctx.fillRect(bx * scale, by * scale, scale, scale);
            ctx.fillStyle = '#ff8800';
            ctx.fillRect((bx - DIR_X[b.rot]) * scale, (by - DIR_Y[b.rot]) * scale, scale, scale);
        }
    }

    // Grenades
    for (const g of grenades) {
        const gx = Math.floor(wrapDelta(g.x - centerX, FIELD_WIDTH)) + halfW;
        const gy = Math.floor(wrapDelta(g.y - centerY, FIELD_HEIGHT)) + halfH;
        if (gx >= 0 && gx < viewW && gy >= 0 && gy < viewH) {
            // Draw grenade as cross with owner's color
            const color = PLAYER_COLORS[g.owner]?.body || '#888';
            ctx.fillStyle = color;
            // 5x5 pixel cross
            for (let dy = -2; dy <= 2; dy++) {
                for (let dx = -2; dx <= 2; dx++) {
                    if (dx === 0 || dy === 0) {
                        ctx.fillRect((gx + dx) * scale, (gy + dy) * scale, scale, scale);
                    }
                }
            }
        }
    }

    // Ammo pickups (blue cross shape)
    for (const p of pickups) {
        const px = Math.floor(wrapDelta(p.x - centerX, FIELD_WIDTH)) + halfW;
        const py = Math.floor(wrapDelta(p.y - centerY, FIELD_HEIGHT)) + halfH;
        if (px >= 0 && px < viewW && py >= 0 && py < viewH) {
            ctx.fillStyle = '#3498db';
            for (let dy = -1; dy <= 1; dy++) {
                for (let dx = -1; dx <= 1; dx++) {
                    if (dx === 0 || dy === 0) {
                        ctx.fillRect((px + dx) * scale, (py + dy) * scale, scale, scale);
                    }
                }
            }
        }
    }

    // Explosions
    for (const e of explosions) {
        const ex = Math.floor(wrapDelta(e.x - centerX, FIELD_WIDTH)) + halfW;
        const ey = Math.floor(wrapDelta(e.y - centerY, FIELD_HEIGHT)) + halfH;
        // Draw explosion as expanding circle of orange/yellow
        const radius = e.radius || 8;  // Use radius from server, fallback to 8
        for (let dy = -radius; dy <= radius; dy++) {
            for (let dx = -radius; dx <= radius; dx++) {
                const dist = Math.sqrt(dx * dx + dy * dy);
                if (dist <= radius) {
                    const px = ex + dx;
                    const py = ey + dy;
                    if (px >= 0 && px < viewW && py >= 0 && py < viewH) {
                        // Color fades from white center to orange edge
                        const fade = dist / radius;
                        if (fade < 0.3) {
                            ctx.fillStyle = '#ffffff';
                        } else if (fade < 0.6) {
                            ctx.fillStyle = '#ffff00';
                        } else {
                            ctx.fillStyle = '#ff6600';
                        }
                        ctx.fillRect(px * scale, py * scale, scale, scale);
                    }
                }
            }
        }
    }

    // Tanks
    for (const [pnum, tank] of Object.entries(tanks)) {
        if (tank.deathTimer > 0) continue;
        const tx = Math.floor(wrapDelta(tank.x - centerX, FIELD_WIDTH)) + halfW;
        const ty = Math.floor(wrapDelta(tank.y - centerY, FIELD_HEIGHT)) + halfH;
        if (tx >= -3 && tx < viewW + 3 && ty >= -3 && ty < viewH + 3) {
            drawTank(ctx, tx, ty, tank.rotation, parseInt(pnum), scale);
        }
    }
}

function scheduleRender() {
    if (!renderPending && gameRunning) {
        renderPending = true;
        requestAnimationFrame(render);
    }
}

function render() {
    renderPending = false;
    const myTank = tanks[myPlayerId];
    if (!myTank) return;

    // Main view
    const mainScale = MAIN_CANVAS_WIDTH / VIEW_WIDTH;
    mainCtx.fillStyle = '#000';
    mainCtx.fillRect(0, 0, mainCanvas.width, mainCanvas.height);
    renderView(mainCtx, myTank.x, myTank.y, VIEW_WIDTH, VIEW_HEIGHT, mainScale);

    // Mini views
    const miniScale = MINI_CANVAS_WIDTH / VIEW_WIDTH;
    for (const [pnum, data] of Object.entries(miniCanvases)) {
        const tank = tanks[pnum];
        if (!tank) continue;
        const ctx = data.canvas.getContext('2d');
        ctx.fillStyle = '#000';
        ctx.fillRect(0, 0, data.canvas.width, data.canvas.height);
        renderView(ctx, tank.x, tank.y, VIEW_WIDTH, VIEW_HEIGHT, miniScale);
    }

    updateOverlays();
}

function updateOverlays() {
    // Main overlay - show all players
    const mainOverlay = document.getElementById('main-overlay');
    if (!mainOverlay) return;

    const playerList = Object.entries(players).sort((a, b) => {
        if (a[1].number === myPlayerId) return -1;
        if (b[1].number === myPlayerId) return 1;
        return a[1].number - b[1].number;
    });

    mainOverlay.innerHTML = playerList.map(([pid, p]) => {
        const tank = tanks[p.number];
        if (!tank) return '';
        const isCurrent = p.number === myPlayerId;
        const isDead = tank.deathTimer > 0;
        return `
            <div class="player-stat ${isCurrent ? 'current' : ''} ${isDead ? 'dead' : ''}">
                <div class="player-color" style="background: ${PLAYER_COLORS[p.number]?.body || '#888'}"></div>
                <span class="player-name">${p.name}</span>
                <div class="player-bars">
                    <div class="player-bar shield-bar"><div class="bar-fill" style="width: ${Math.max(0, tank.shields) * 100}%"></div></div>
                    <div class="player-bar ammo-bar"><div class="bar-fill" style="width: ${Math.min((tank.ammo || 0) / 100, 1.0) * 100}%"></div><span class="ammo-count">${tank.ammo || 0}</span></div>
                </div>
                <span class="player-kills">${tank.kills || 0}K</span>
            </div>
        `;
    }).join('');
}

// ========== GAME INITIALIZATION ==========
function initGame() {
    log('initGame started');
    gameRunning = true;
    mainCanvas = document.getElementById('main-canvas');
    mainCtx = mainCanvas.getContext('2d');

    // Mini canvases
    const miniViewsContainer = document.getElementById('mini-views');
    miniViewsContainer.innerHTML = '';
    miniCanvases = {};

    for (const [pnum, tank] of Object.entries(tanks)) {
        if (parseInt(pnum) === myPlayerId) continue;

        const container = document.createElement('div');
        container.className = 'mini-view-container';

        const miniCanvas = document.createElement('canvas');
        miniCanvas.className = 'mini-view';
        miniCanvas.width = MINI_CANVAS_WIDTH;
        miniCanvas.height = MINI_CANVAS_HEIGHT;
        miniCanvas.style.borderColor = PLAYER_COLORS[parseInt(pnum)]?.body || '#888';

        const overlay = document.createElement('div');
        overlay.className = 'mini-overlay';
        overlay.style.borderColor = PLAYER_COLORS[parseInt(pnum)]?.body || '#888';
        overlay.textContent = players[Object.keys(players).find(k => players[k].number === parseInt(pnum))]?.name || 'P' + pnum;

        container.appendChild(miniCanvas);
        container.appendChild(overlay);
        miniViewsContainer.appendChild(container);

        miniCanvases[pnum] = { canvas: miniCanvas, container };
    }
    log('canvas setup done');

    // Keyboard
    document.addEventListener('keydown', (e) => {
        let changed = false;
        if (e.key === 'ArrowUp' || e.key === 'w' || e.key === 'W') { if (!keys.up) changed = true; keys.up = true; e.preventDefault(); }
        if (e.key === 'ArrowDown' || e.key === 's' || e.key === 'S') { if (!keys.down) changed = true; keys.down = true; e.preventDefault(); }
        if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') { if (!keys.left) changed = true; keys.left = true; e.preventDefault(); }
        if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') { if (!keys.right) changed = true; keys.right = true; e.preventDefault(); }
        if (e.key === ' ' || e.key === 'Control') { if (!keys.fire) changed = true; keys.fire = true; e.preventDefault(); }
        if (e.key === 'g' || e.key === 'G') { if (!keys.grenade) changed = true; keys.grenade = true; e.preventDefault(); }
        if (e.key === 'Shift') { if (!keys.turbo) changed = true; keys.turbo = true; e.preventDefault(); }
        if (changed) sendInput();
    });

    document.addEventListener('keyup', (e) => {
        let changed = false;
        if (e.key === 'ArrowUp' || e.key === 'w' || e.key === 'W') { if (keys.up) changed = true; keys.up = false; }
        if (e.key === 'ArrowDown' || e.key === 's' || e.key === 'S') { if (keys.down) changed = true; keys.down = false; }
        if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') { if (keys.left) changed = true; keys.left = false; }
        if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') { if (keys.right) changed = true; keys.right = false; }
        if (e.key === ' ' || e.key === 'Control') { if (keys.fire) changed = true; keys.fire = false; }
        if (e.key === 'g' || e.key === 'G') { if (keys.grenade) changed = true; keys.grenade = false; }
        if (e.key === 'Shift') { if (keys.turbo) changed = true; keys.turbo = false; }
        if (changed) sendInput();
    });

    // Touch controls
    initTouchControls();

    // Initial render
    render();
    log('initial render done');

    // Tick worker - host sends ticks, others take over if host is inactive
    log('starting tick worker' + (isHost ? ' (host)' : ' (standby)'));
    const workerUrl = '/static/projects/tunneler/tick-worker.js';
    tickWorker = new Worker(workerUrl);
    tickWorker.onmessage = function(e) {
        if (e.data.type === 'tick') {
            // Send tick if we're host, or if no updates received for 1.5 seconds
            const timeSinceUpdate = performance.now() - lastStateUpdate;
            const hostInactive = lastStateUpdate > 0 && timeSinceUpdate > 1500;
            if (isHost || hostInactive) {
                sendTick();
            }
        }
    };
    tickWorker.postMessage({ command: 'start', tickMs: TICK_MS });
    log('initGame complete');
}

// ========== TOUCH CONTROLS ==========
let touchStartX = null;
let touchStartY = null;
let moveTouchId = null;
let activeTouchCount = 0;

function initTouchControls() {
    const isTouchDevice = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
    if (!isTouchDevice) return;

    // Hide old button-based controls
    const oldControls = document.getElementById('touch-controls');
    if (oldControls) oldControls.style.display = 'none';

    const gameArea = document.getElementById('game-area');
    if (!gameArea) return;

    gameArea.addEventListener('touchstart', handleTouchStart, { passive: false });
    gameArea.addEventListener('touchmove', handleTouchMove, { passive: false });
    gameArea.addEventListener('touchend', handleTouchEnd, { passive: false });
    gameArea.addEventListener('touchcancel', handleTouchEnd, { passive: false });
}

function handleTouchStart(e) {
    e.preventDefault();
    activeTouchCount = e.touches.length;

    // Two or more fingers = fire
    if (activeTouchCount >= 2) {
        if (!keys.fire) {
            keys.fire = true;
            sendInput();
        }
    }

    // Track first touch for movement
    for (const touch of e.changedTouches) {
        if (moveTouchId === null) {
            moveTouchId = touch.identifier;
            touchStartX = touch.clientX;
            touchStartY = touch.clientY;
        }
    }
}

function handleTouchMove(e) {
    e.preventDefault();

    for (const touch of e.changedTouches) {
        if (touch.identifier === moveTouchId && touchStartX !== null) {
            const dx = touch.clientX - touchStartX;
            const dy = touch.clientY - touchStartY;

            // Need minimum movement to set direction
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist > 20) {
                // Calculate angle and map to 8 directions
                const angle = Math.atan2(dy, dx);
                const dir = Math.round((angle / Math.PI) * 4 + 8) % 8;

                // Map direction to keys (0=right, 2=down, 4=left, 6=up)
                const newKeys = {
                    right: dir === 0 || dir === 1 || dir === 7,
                    down: dir === 1 || dir === 2 || dir === 3,
                    left: dir === 3 || dir === 4 || dir === 5,
                    up: dir === 5 || dir === 6 || dir === 7
                };

                let changed = false;
                for (const [key, val] of Object.entries(newKeys)) {
                    if (keys[key] !== val) {
                        keys[key] = val;
                        changed = true;
                    }
                }
                if (changed) sendInput();
            }
        }
    }
}

function handleTouchEnd(e) {
    e.preventDefault();
    activeTouchCount = e.touches.length;

    // Stop firing when less than 2 fingers
    if (activeTouchCount < 2 && keys.fire) {
        keys.fire = false;
        sendInput();
    }

    for (const touch of e.changedTouches) {
        if (touch.identifier === moveTouchId) {
            // Movement touch ended
            moveTouchId = null;
            touchStartX = null;
            touchStartY = null;
            keys.up = keys.down = keys.left = keys.right = false;
            sendInput();

            // If there's still another touch, use it for movement
            if (e.touches.length > 0) {
                const newTouch = e.touches[0];
                moveTouchId = newTouch.identifier;
                touchStartX = newTouch.clientX;
                touchStartY = newTouch.clientY;
            }
        }
    }
}

function toggleFullscreen() {
    const container = document.getElementById('game-container');

    // Check various fullscreen APIs
    const requestFS = container.requestFullscreen ||
                      container.webkitRequestFullscreen ||
                      container.mozRequestFullScreen ||
                      container.msRequestFullscreen;

    const exitFS = document.exitFullscreen ||
                   document.webkitExitFullscreen ||
                   document.mozCancelFullScreen ||
                   document.msExitFullscreen;

    const isFullscreen = document.fullscreenElement ||
                         document.webkitFullscreenElement ||
                         document.mozFullScreenElement ||
                         document.msFullscreenElement;

    if (!isFullscreen) {
        if (requestFS) {
            requestFS.call(container).then(() => {
                // Try to lock to landscape on mobile
                if (screen.orientation && screen.orientation.lock) {
                    screen.orientation.lock('landscape').catch(() => {});
                }
                // Rescale after entering fullscreen
                setTimeout(() => {
                    if (typeof scaleGameToFit === 'function') scaleGameToFit();
                }, 100);
            }).catch(() => {
                // Fallback: just make it fill the viewport without true fullscreen
                container.classList.add('pseudo-fullscreen');
            });
        } else {
            // No fullscreen API (iOS Safari) - use pseudo fullscreen
            container.classList.add('pseudo-fullscreen');
            document.body.style.overflow = 'hidden';
            window.scrollTo(0, 0);
        }
    } else {
        if (exitFS) {
            exitFS.call(document);
        }
        container.classList.remove('pseudo-fullscreen');
        document.body.style.overflow = '';
    }
}
