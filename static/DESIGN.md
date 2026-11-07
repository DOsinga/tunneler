# Tunneler - Design Document

A multiplayer tank game inspired by the classic DOS game Tunneler. Players control tanks that dig through dirt, navigate tunnels, and battle each other.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Client (Browser)                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ tunneler.css│  │   game.js   │  │        main.js          │  │
│  │   Styling   │  │  Rendering  │  │  WebSocket / Lobby UI   │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
└────────────────────────────┬────────────────────────────────────┘
                             │ WebSocket
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Server (Django Channels)                     │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                       tunneler.py                           ││
│  │  - Room management        - Game physics                    ││
│  │  - Player inputs          - Bullet/tank collision           ││
│  │  - Field generation       - State serialization             ││
│  │  - AI bots                - Ammo pickups                    ││
│  └─────────────────────────────────────────────────────────────┘│
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                    Redis (Production)                       ││
│  │  - Room state (JSON)      - Player inputs (Hash)            ││
│  │  - Message acks (List)                                      ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

## File Structure

```
static/projects/tunneler/
├── tunneler.css     # All styling (1024x768 fixed resolution, retro theme)
├── game.js          # Game engine (rendering, state, input handling)
├── main.js          # Lobby UI, WebSocket handling, entry point
├── tick-worker.js   # Web Worker for tick timing
└── DESIGN.md        # This document

projects/
├── tunneler.py      # Server-side game logic
├── common.py        # WebSocket consumer base class (AsyncJsonWebsocketConsumer)
└── templates/projects/
    └── tunneler.html  # HTML template (loads static files)
```

## Game Constants

| Constant | Value | Description |
|----------|-------|-------------|
| FIELD_WIDTH | 640 | World width in pixels |
| FIELD_HEIGHT | 480 | World height in pixels |
| VIEW_WIDTH | 112 | Visible area width (896/8) |
| VIEW_HEIGHT | 75 | Visible area height (600/8) |
| TICK_MS | 33 | Server tick interval (~30 Hz) |
| TANK_SPEED | 30.0 | Tank movement speed |
| DIG_SPEED | 10.0 | Speed when digging |
| BULLET_SPEED | 60.0 | Bullet movement speed |

## Terrain Types

| Type | Value | Char | Description |
|------|-------|------|-------------|
| EMPTY | 0 | '.' | Tunneled space |
| DIRT1 | 8 | 'd' | Diggable terrain (brown) |
| DIRT2 | 9 | 'D' | Diggable terrain (tan) |
| ROCK | 10 | '#' | Indestructible |
| WALL | 30+ | '1'-'4' | Player base walls (30+player*10) |

## Client Architecture

### game.js - Game Engine

**Constants & Configuration:**
- Display dimensions and scaling factors
- Terrain and player color definitions
- Tank sprite data (7x7 pixel grid)
- Direction lookup tables (8 directions)

**Rendering System:**
- `renderView()`: Renders terrain, bullets, pickups, grenades, explosions, and tanks centered on a position
- `drawTank()`: Rotates and draws tank sprite at given position
- `scheduleRender()`: Uses requestAnimationFrame to prevent redundant renders
- `updateOverlays()`: Updates player HUD (ammo count, shields, kills)

**State Management:**
- `field[][]`: 2D array of terrain values
- `tanks{}`: Tank state keyed by player number
- `bullets[]`: Active bullet list
- `pickups[]`: Ammo pickup positions from server
- `grenades[]`: Active grenade list
- `explosions[]`: Visual-only explosion effects
- `myPlayerId`: Current player's number

**Input Handling:**
- Keyboard: WASD / Arrow keys + Space/Control for fire, G for grenade
- Touch: Drag to move, two fingers to fire (mobile)
- Input changes trigger immediate `sendInput()` to server

### main.js - Lobby & Networking

**Lobby System:**
- Room creation via URL parameter
- Player name entry and join flow
- Host controls game start and can add AI bots
- Configurable game settings (stored in localStorage)
- Animated background with tank in tunnel

**WebSocket Protocol:**

| Message Type | Direction | Description |
|-------------|-----------|-------------|
| `join` | C→S | Player joins room with name |
| `rejoin` | C→S | Auto-rejoin check on page load |
| `leave` | C→S | Player disconnects |
| `add_bot` | C→S | Host adds AI bot |
| `player_list` | S→C | Updated player list (broadcast) |
| `start_game` | C→S | Host starts game (includes settings) |
| `game_started` | S→C | Initial game state (broadcast) |
| `input` | C→S | Player key state + msg_id |
| `tick` | C→S | Host requests server tick |
| `state_update` | S→C | Game state delta (broadcast) |
| `game_over` | S→C | Winner announcement + final scores |
| `new_game` | C→S | Host requests return to lobby |
| `return_to_lobby` | S→C | Reset to lobby (broadcast) |

**Latency Tracking:**
- Each input message includes a unique `msg_id`
- Server echoes acks in next state_update
- Client calculates round-trip time from ack

**Winner Overlay:**
- Shown when a player reaches the kill target (2 × num_players + 1)
- Displays scoreboard sorted by kills
- Host gets "NEW GAME" button, others see "WAITING FOR HOST..."

## Server Architecture

### tunneler.py - Game Server

**Room Management:**
- Rooms identified by 6-letter ID
- Max 4 players per room
- First player becomes host
- Host drives game ticks
- Host takeover: if host is inactive >1s, another player becomes host

**Field Generation:**
- Seeded random for deterministic generation
- Rock border around edges
- Random dirt pattern (DIRT1/DIRT2)
- Random rock formations
- Player bases placed with minimum distance

**Game Loop:**
The host client sends `tick` messages at ~30Hz. On each tick:

1. Retrieve inputs from Redis hash (avoids race condition)
2. Process AI inputs for bots
3. Process each tank:
   - Handle respawn if dead (restores ammo + shields)
   - Apply movement based on input
   - Check collisions (terrain, tanks)
   - Dig terrain if moving through dirt (dig speed remap: even 1/5 dirt gives noticeable slowdown)
   - Handle shooting (costs 1 ammo per shot, cooldown)
   - Handle grenade dropping (costs ammo)
   - Repair shields at base (faster) or in field (slower)
4. Process ammo pickup collection (proximity check, respawn collected pickups)
5. Process bullets:
   - Move in direction, trace line
   - Dig through dirt
   - Stop at rock/walls
   - Damage tanks on hit (tracks lastHitBy for kill credit)
6. Process grenade proximity detonation and explosions
7. Check win condition (first to kill target wins)
8. Return state delta with changes

**Ammo System:**
- Tanks start with 30 ammo
- Shooting costs 1 ammo, grenades cost 20 ammo
- Ammo does NOT regenerate — must collect pickups
- 50 ammo pickups spawned on the map at game start
- Pickups respawn at new positions when collected
- Running out of ammo doesn't kill — only shield depletion kills

**Kill Tracking & Win Condition:**
- `lastHitBy` tracks who last damaged each tank
- On death, kill credited to `lastHitBy` attacker
- First player to reach `2 × num_players + 1` kills wins
- Game over triggers winner overlay, host can start new game

**AI Bots:**
- State machine: LEAVING → EXPLORE → ATTACK/RETREAT → HEAL
- Vision system: tracks explored cells, detects enemies in explored areas
- Stuck detection with automatic re-targeting
- Strength-based decision making (shield level)

**Redis Data Model (Production):**

```
tunneler:room:{room_id}     - JSON blob of room state
                             - players, host, started, field, tanks, etc.
                             - TTL: 4 hours

tunneler:inputs:{room_id}   - Hash of player inputs
                             - Key: player_num, Value: JSON keys
                             - TTL: 60 seconds (refreshed on input)

tunneler:acks:{room_id}     - List of pending message acks
                             - TTL: 10 seconds
```

**Why Separate Input Storage:**

Originally, inputs were stored inside the room JSON. This caused a race condition:

```
Thread A (input handler):          Thread B (tick handler):
1. GET room                        1. GET room
2. room.inputs[p1] = keys          2. Process game tick
3. SET room                        3. SET room  ← Overwrites Thread A's input!
```

Solution: Store inputs in a Redis hash using HSET/HGETALL, which are atomic operations. Tick handler only reads inputs, never writes them.

**Critical Architecture Note:**
The `receive()` method in tunneler.py is called synchronously from an async
WebSocket consumer (common.py line 65). This means tick processing blocks the
async event loop. The host's WebSocket connection handles both `tick` and `input`
messages sequentially, so slow tick processing can delay host input handling
and cause rising latency specifically for the host player.

## Display Layout

```
┌──────────────────────────────────────────────────────────────────┐
│                         1024 x 768                                │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐     │
│  │ Mini View  │ │ Mini View  │ │ Mini View  │ │  (unused)  │     │
│  │  224x150   │ │  224x150   │ │  224x150   │ │            │     │
│  │  Player 2  │ │  Player 3  │ │  Player 4  │ │            │     │
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘     │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                                                            │  │
│  │                      Main Canvas                           │  │
│  │                       896 x 600                            │  │
│  │                    (VIEW: 112 x 75)                        │  │
│  │                     8x pixel scale                         │  │
│  │                                                            │  │
│  │  ┌──────┐                                   ┌───────────┐  │  │
│  │  │PING: │                                   │ Player HUD│  │  │
│  │  │ 42ms │                                   │           │  │  │
│  │  └──────┘                                   └───────────┘  │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

**Pixel Scaling:**
- Main canvas: 896x600 CSS pixels
- View size: 112x75 game pixels
- Scale factor: 896/112 = 600/75 = 8x exactly
- Integer scaling prevents fractional pixel artifacts

## Tank Sprite

The tank is a 7x7 pixel sprite with rotation support:

```
  0 1 2 3 4 5 6
0 . . . . . . .
1 T T T T T T .    T = Track (color 2)
2 . B B B B . .    B = Body (color 1)
3 . B B X X X X    X = Barrel (color 3)
4 . B B B B . .
5 T T T T T T .
6 . . . . . . .
```

Rotation is handled by rotating each pixel position around center (3,3) using standard rotation matrix:
```
rx = px * cos(angle) - py * sin(angle)
ry = px * sin(angle) + py * cos(angle)
```

## Game Mechanics

**Movement:**
- 8-directional movement (keyboard: 4 dirs + diagonals)
- Speed reduced when digging (remapped: even slight dirt gives noticeable slowdown)
- Cannot move through rock or other tanks

**Combat:**
- Shooting costs 1 ammo per shot
- Bullets damage shields
- Grenades: area damage, costs 20 ammo, detonated by shooting or enemy proximity
- When shields reach 0, tank dies (ammo depletion does not kill)
- Death timer: 4 seconds before respawn at base
- Kill credited to last player who damaged the dying tank

**Ammo System:**
- Start with 30 ammo, max 100
- Collect blue cross pickups scattered on the map (+10 ammo each)
- Ammo does not regenerate at base
- Grenades cost 20 ammo

**Base Mechanics:**
- Each player has a base (colored walls)
- Shields regenerate inside base (faster) and outside (slower)
- Respawn point after death (full shields + ammo restored)

## Protocol Details

### Field Packing

The 640x480 field (307,200 cells) is packed into a string for efficient transmission:
- Each cell becomes one character
- Total size: ~300KB

```python
_TERRAIN_TO_CHAR = {
    TERRAIN_EMPTY: '.',
    TERRAIN_DIRT1: 'd',
    TERRAIN_DIRT2: 'D',
    TERRAIN_ROCK: '#',
    # Player walls: '1', '2', '3', '4'
}
```

### State Updates

Instead of sending the full field each tick, only changes are sent:
```json
{
  "type": "state_update",
  "tanks": { ... },
  "bullets": [ ... ],
  "grenades": [ ... ],
  "explosions": [ ... ],
  "pickups": [ ... ],
  "changes": [[x, y, newValue], ...],
  "acks": ["msg-id-1", "msg-id-2"],
  "host": "player_id"
}
```

## Mobile Support

Touch controls:
- Drag to set movement direction (8-way, based on angle from touch start)
- Two or more fingers = fire
- Fullscreen button for immersive play
- Prevents accidental scrolling via touch event handling
