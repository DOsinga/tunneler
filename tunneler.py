import json
import logging
import math
import random
import string
import time
from typing import Any

from django.conf import settings

from projects.common import Project, RedirectException

logger = logging.getLogger(__name__)


# Game world dimensions
FIELD_WIDTH = 640
FIELD_HEIGHT = 480

# Base dimensions
BASE_SIZE_X = 16
BASE_SIZE_Y = 19
BASE_DOOR_SIZE = 5

# Terrain types (as byte values for direct use in bytearray)
CHAR_EMPTY = ord(".")
CHAR_DIRT1 = ord("d")
CHAR_DIRT2 = ord("D")
CHAR_ROCK = ord("#")
CHAR_WALL = [0, ord("1"), ord("2"), ord("3"), ord("4")]  # Index by player number

# Map byte values to client terrain types (for changes array)
# Client expects: EMPTY=0, DIRT1=8, DIRT2=9, ROCK=10, WALL=30+player*10
CHAR_TO_CLIENT = {
    CHAR_EMPTY: 0,
    CHAR_DIRT1: 8,
    CHAR_DIRT2: 9,
    CHAR_ROCK: 10,
    CHAR_WALL[1]: 40,
    CHAR_WALL[2]: 50,
    CHAR_WALL[3]: 60,
    CHAR_WALL[4]: 70,
}


def field_get(field: bytearray, x: int, y: int) -> int:
    """Get terrain at position. Returns byte value."""
    return field[y * FIELD_WIDTH + x]


def field_set(field: bytearray, x: int, y: int, value: int) -> None:
    """Set terrain at position."""
    field[y * FIELD_WIDTH + x] = value


def is_diggable(terrain: int) -> bool:
    return terrain == CHAR_DIRT1 or terrain == CHAR_DIRT2


def is_solid(terrain: int) -> bool:
    return terrain == CHAR_ROCK or terrain in CHAR_WALL[1:]


# Default physics constants (used if no settings provided)
DEFAULT_SETTINGS = {
    "tankSpeed": 30.0,
    "digSpeed": 10.0,
    "bulletSpeed": 60.0,
    "fireDelay": 150,  # ms
    "shotDamage": 14,  # %
    "startAmmo": 30,
    "grenadeCostAmmo": 20,
    "grenadeDamage": 84,  # % shield damage at center
    "grenadeRadius": 8,
    "pickupAmmo": 10,
    "pickupCount": 50,
    "repairShieldOut": 90,  # sec
    "repairShieldIn": 6,  # sec
    "deathTime": 4.0,  # sec
}


def get_game_constants(settings: dict) -> dict:
    """Convert client settings to server-side constants."""
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    shot_damage = s["shotDamage"] / 100.0
    return {
        "tank_speed": float(s["tankSpeed"]),
        "dig_speed": float(s["digSpeed"]),
        "bullet_speed": float(s["bulletSpeed"]),
        "fire_delay": s["fireDelay"] / 1000.0,  # ms -> sec
        "shot_damage": shot_damage,
        "start_ammo": int(s["startAmmo"]),
        "grenade_cost_ammo": int(s["grenadeCostAmmo"]),
        "grenade_damage": s["grenadeDamage"] / 100.0,
        "grenade_radius": int(s["grenadeRadius"]),
        "pickup_ammo": int(s["pickupAmmo"]),
        "pickup_count": int(s["pickupCount"]),
        "repair_shield_out": 1.0 / float(s["repairShieldOut"]),
        "repair_shield_in": 1.0 / float(s["repairShieldIn"]),
        "dig_shield_cost": 1.2 / float(s["repairShieldOut"]),  # slightly more than outdoor recovery
        "death_time": float(s["deathTime"]),
    }



# Direction tables
DIR_X = [1.0, 0.707, 0.0, -0.707, -1.0, -0.707, 0.0, 0.707]
DIR_Y = [0.0, 0.707, 1.0, 0.707, 0.0, -0.707, -1.0, -0.707]

# AI Constants
AI_VISION_RADIUS = 15  # How far the AI can "see" around itself
AI_ATTACK_RANGE = 40  # Distance at which AI will start shooting
AI_RETREAT_THRESHOLD = 0.3  # Retreat if energy+shields below this vs enemy

# AI Modes
AI_LEAVING = "LEAVING"  # Navigating out of base
AI_EXPLORE = "EXPLORE"
AI_ATTACK = "ATTACK"
AI_RETREAT = "RETREAT"
AI_HEAL = "HEAL"

BOT_NAMES = ["ALPHA", "BETA", "GAMMA", "DELTA", "OMEGA", "SIGMA", "THETA", "ZETA"]


def create_bot_state(base_x: int, base_y: int) -> dict:
    """Create initial AI state for a bot."""
    return {
        "explored": set(),  # (x, y) cells we've seen
        "base_pos": (base_x, base_y),
        "known_enemies": {},  # player_num -> {"x", "y", "shields", "time"}
        "mode": AI_LEAVING,  # Start by leaving the base
        "stuck_counter": 0,  # Detect if stuck
        "last_pos": None,
        "last_dir": 6,  # Start facing up (toward door)
        "has_left_base": False,  # Track if we've ever left the base
        "explore_target": None,  # Current exploration target
    }


def is_blocked_by_rock_or_wall(
    x: float, y: float, direction: int, field: bytearray
) -> bool:
    """Check if moving in this direction would hit rock or wall (not dirt - we can dig that)."""
    # Check several pixels ahead in this direction, considering tank size
    for dist in range(1, 8):
        check_x = x + DIR_X[direction] * dist
        check_y = y + DIR_Y[direction] * dist
        # Check tank corners in movement direction
        for offset in [-3, 0, 3]:
            perp = (direction + 2) % 8
            fx = int(check_x + DIR_X[perp] * offset) % FIELD_WIDTH
            fy = int(check_y + DIR_Y[perp] * offset) % FIELD_HEIGHT
            terrain = field_get(field, fx, fy)
            if is_solid(terrain):  # Rock or wall
                return True
    return False


def score_direction(
    x: float, y: float, direction: int, field: bytearray, target_dir: int
) -> float:
    """Score a direction: prefer target direction, prefer empty over dirt, avoid rocks."""
    # Check if blocked by rock/wall
    if is_blocked_by_rock_or_wall(x, y, direction, field):
        return -1000

    score = 0.0

    # Bonus for being close to target direction
    dir_diff = abs(direction - target_dir)
    if dir_diff > 4:
        dir_diff = 8 - dir_diff
    score -= dir_diff * 10  # Prefer directions closer to target

    # Check terrain ahead - prefer empty (tunneled) over dirt
    for dist in range(1, 10):
        check_x = int(x + DIR_X[direction] * dist) % FIELD_WIDTH
        check_y = int(y + DIR_Y[direction] * dist) % FIELD_HEIGHT
        terrain = field_get(field, check_x, check_y)
        if terrain == CHAR_EMPTY:
            score += 1  # Prefer tunneled paths

    return score


def find_explore_target(ai_state: dict, tank: dict) -> tuple | None:
    """Find an unexplored area to explore."""
    x, y = tank["x"], tank["y"]
    explored = ai_state["explored"]

    best_target = None
    best_score = -1

    # Sample directions and find one with most unexplored cells
    for direction in range(8):
        for dist in [40, 60, 80]:
            target_x = x + DIR_X[direction] * dist
            target_y = y + DIR_Y[direction] * dist

            # Wrap target into field
            target_x = target_x % FIELD_WIDTH
            target_y = target_y % FIELD_HEIGHT

            # Count unexplored cells near this target
            score = 0
            for dy in range(-15, 16, 5):
                for dx in range(-15, 16, 5):
                    if (int(target_x + dx) % FIELD_WIDTH, int(target_y + dy) % FIELD_HEIGHT) not in explored:
                        score += 1

            if score > best_score:
                best_score = score
                best_target = (target_x, target_y)

    return best_target


def move_toward(
    tank: dict,
    target_x: float,
    target_y: float,
    field: bytearray,
    current_dir: int = -1,
    debug: bool = False,
) -> dict:
    """Move toward target, avoiding rocks/walls, preferring tunneled paths."""
    keys = {
        "up": False,
        "down": False,
        "left": False,
        "right": False,
        "fire": False,
        "grenade": False,
    }

    target_dir = get_direction_to(tank["x"], tank["y"], target_x, target_y)
    if target_dir < 0:
        return keys  # Already at target

    # Score all 8 directions
    best_dir = target_dir
    best_score = -float("inf")
    all_scores = []

    for d in range(8):
        score = score_direction(tank["x"], tank["y"], d, field, target_dir)
        # Hysteresis: bonus for continuing in current direction
        if d == current_dir:
            score += 5
        all_scores.append((d, score))
        if score > best_score:
            best_score = score
            best_dir = d

    if debug:
        dir_names = ["R", "DR", "D", "DL", "L", "UL", "U", "UR"]
        scores_str = ", ".join(f"{dir_names[d]}:{s:.0f}" for d, s in all_scores)
        logger.info(
            f"move_toward ({tank['x']:.0f},{tank['y']:.0f})->({target_x:.0f},{target_y:.0f}) target_dir={dir_names[target_dir]} scores=[{scores_str}] best={dir_names[best_dir]}"
        )

    if best_score > -1000:  # Found a valid direction
        keys = dir_to_keys(best_dir)

    return keys


def update_ai_vision(
    ai_state: dict, tank: dict, field: bytearray, all_tanks: dict, pnum: str
):
    """Update what the AI can see from its current position."""
    x, y = int(tank["x"]), int(tank["y"])

    # Mark cells in vision radius as explored
    for dy in range(-AI_VISION_RADIUS, AI_VISION_RADIUS + 1):
        for dx in range(-AI_VISION_RADIUS, AI_VISION_RADIUS + 1):
            if dx * dx + dy * dy <= AI_VISION_RADIUS * AI_VISION_RADIUS:
                fx, fy = (x + dx) % FIELD_WIDTH, (y + dy) % FIELD_HEIGHT
                ai_state["explored"].add((fx, fy))

    # Check if any enemy tanks are in explored areas
    now = time.time()
    for other_pnum, other_tank in all_tanks.items():
        if other_pnum == pnum:
            continue
        if other_tank["deathTimer"] > 0:
            # Enemy is dead, forget them
            if other_pnum in ai_state["known_enemies"]:
                del ai_state["known_enemies"][other_pnum]
            continue

        ox, oy = int(other_tank["x"]), int(other_tank["y"])
        if (ox, oy) in ai_state["explored"]:
            # We can see this enemy!
            ai_state["known_enemies"][other_pnum] = {
                "x": other_tank["x"],
                "y": other_tank["y"],
                "shields": other_tank["shields"],
                "time": now,
            }


def wrap_delta(d: float, size: int) -> float:
    """Shortest signed offset on a wrapped axis."""
    d = d % size
    if d > size / 2:
        d -= size
    return d


def get_direction_to(from_x: float, from_y: float, to_x: float, to_y: float) -> int:
    """Get the direction (0-7) from one point to another (toroidal)."""
    dx = wrap_delta(to_x - from_x, FIELD_WIDTH)
    dy = wrap_delta(to_y - from_y, FIELD_HEIGHT)
    if abs(dx) < 0.1 and abs(dy) < 0.1:
        return -1  # Already there
    angle = math.atan2(dy, dx)
    # Convert angle to direction (0=right, 2=down, 4=left, 6=up)
    dir_idx = round(angle / (math.pi / 4)) % 8
    return dir_idx


def can_shoot_at(tank: dict, target_x: float, target_y: float) -> bool:
    """Check if target is roughly in front of the tank (toroidal)."""
    dx = wrap_delta(target_x - tank["x"], FIELD_WIDTH)
    dy = wrap_delta(target_y - tank["y"], FIELD_HEIGHT)
    dist = math.sqrt(dx * dx + dy * dy)
    if dist < 3:
        return True  # Very close, always shoot

    # Check if target is within ~30 degrees of where we're facing
    target_dir = get_direction_to(tank["x"], tank["y"], target_x, target_y)
    tank_dir = tank["rotation"]

    # Direction difference (accounting for wrap-around)
    diff = abs(target_dir - tank_dir)
    if diff > 4:
        diff = 8 - diff

    return diff <= 1  # Within 1 direction step (~45 degrees)


def process_ai_input(
    ai_state: dict, tank: dict, pnum: str, field: bytearray, all_tanks: dict
) -> dict:
    """Determine AI input for this tick. Returns keys dict."""
    keys = {
        "up": False,
        "down": False,
        "left": False,
        "right": False,
        "fire": False,
        "grenade": False,
    }

    if tank["deathTimer"] > 0:
        return keys  # Dead, do nothing

    # Update vision
    update_ai_vision(ai_state, tank, field, all_tanks, pnum)

    # Calculate our total "strength" (shields only, ammo doesn't regenerate)
    my_strength = tank["shields"]

    # Find closest known enemy
    closest_enemy = None
    closest_dist = float("inf")
    enemy_strength = 0

    for enemy_pnum, enemy_info in ai_state["known_enemies"].items():
        dx = wrap_delta(enemy_info["x"] - tank["x"], FIELD_WIDTH)
        dy = wrap_delta(enemy_info["y"] - tank["y"], FIELD_HEIGHT)
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < closest_dist:
            closest_dist = dist
            closest_enemy = enemy_info
            enemy_strength = enemy_info["shields"]

    # Check if we're at base
    base_x, base_y = ai_state["base_pos"]
    at_base = (
        abs(tank["x"] - base_x) < BASE_SIZE_X and abs(tank["y"] - base_y) < BASE_SIZE_Y
    )

    # Track if we've left the base
    if not at_base and not ai_state.get("has_left_base"):
        ai_state["has_left_base"] = True
        logger.info(f"Bot {pnum} has left the base!")

    # State machine
    old_mode = ai_state["mode"]

    # LEAVING state takes priority until we've actually left
    if ai_state["mode"] == AI_LEAVING:
        if ai_state.get("has_left_base"):
            ai_state["mode"] = AI_EXPLORE
    elif ai_state["mode"] == AI_HEAL:
        # After healing, go to LEAVING to exit the base
        if tank["shields"] >= 0.95:
            ai_state["mode"] = AI_LEAVING
            ai_state["has_left_base"] = False
    elif at_base and ai_state.get("has_left_base") and tank["shields"] < 0.3:
        # Only heal if we've left before and are low on resources
        ai_state["mode"] = AI_HEAL
    elif closest_enemy is None:
        ai_state["mode"] = AI_EXPLORE
    else:
        # Hysteresis: need bigger difference to CHANGE mode
        current_mode = ai_state["mode"]
        if current_mode == AI_ATTACK:
            # Only retreat if significantly weaker
            if my_strength < enemy_strength - 0.4 and ai_state.get("has_left_base"):
                ai_state["mode"] = AI_RETREAT
        elif current_mode == AI_RETREAT:
            # Only attack if significantly stronger
            if my_strength > enemy_strength + 0.4:
                ai_state["mode"] = AI_ATTACK
        else:
            # Not in combat mode yet, use normal thresholds
            if my_strength > enemy_strength + AI_RETREAT_THRESHOLD:
                ai_state["mode"] = AI_ATTACK
            elif my_strength < enemy_strength - AI_RETREAT_THRESHOLD and ai_state.get(
                "has_left_base"
            ):
                ai_state["mode"] = AI_RETREAT
            else:
                ai_state["mode"] = AI_ATTACK

    if ai_state["mode"] != old_mode:
        logger.info(f"Bot {pnum} mode: {old_mode} -> {ai_state['mode']}")

    # Stuck detection - if not moving, pick a new explore target
    if ai_state["last_pos"]:
        dx = wrap_delta(tank["x"] - ai_state["last_pos"][0], FIELD_WIDTH)
        dy = wrap_delta(tank["y"] - ai_state["last_pos"][1], FIELD_HEIGHT)
        moved = math.sqrt(dx * dx + dy * dy)
        if moved < 0.2:
            ai_state["stuck_counter"] += 1
        else:
            ai_state["stuck_counter"] = 0
    ai_state["last_pos"] = (tank["x"], tank["y"])

    # If stuck for too long, pick a new target
    if ai_state["stuck_counter"] > 50:
        ai_state["explore_target"] = None
        ai_state["stuck_counter"] = 0

    # Execute based on mode
    current_dir = ai_state.get("last_dir", -1)

    if ai_state["mode"] == AI_LEAVING:
        # Move toward just outside the base door (door is at top of base)
        door_x = base_x
        door_y = base_y - BASE_SIZE_Y - 10
        keys = move_toward(tank, door_x, door_y, field, current_dir, debug=True)

    elif ai_state["mode"] == AI_HEAL:
        # Stay at base, don't move
        pass

    elif ai_state["mode"] == AI_EXPLORE:
        # Pick exploration target if we don't have one or reached it
        target = ai_state.get("explore_target")
        edx = abs(wrap_delta(tank["x"] - target[0], FIELD_WIDTH)) if target else 999
        edy = abs(wrap_delta(tank["y"] - target[1], FIELD_HEIGHT)) if target else 999
        if not target or (edx < 10 and edy < 10):
            target = find_explore_target(ai_state, tank)
            ai_state["explore_target"] = target
            logger.info(f"Bot {pnum} new explore target: {target}")

        if target:
            keys = move_toward(
                tank, target[0], target[1], field, current_dir, debug=True
            )

    elif ai_state["mode"] == AI_ATTACK:
        if closest_enemy:
            keys = move_toward(
                tank, closest_enemy["x"], closest_enemy["y"], field, current_dir
            )

            # Shoot if enemy is in front
            if closest_dist < AI_ATTACK_RANGE and can_shoot_at(
                tank, closest_enemy["x"], closest_enemy["y"]
            ):
                keys["fire"] = True

            # Use grenade if close and have ammo
            if closest_dist < 15 and tank["ammo"] >= 20:
                keys["grenade"] = True

    elif ai_state["mode"] == AI_RETREAT:
        keys = move_toward(tank, base_x, base_y, field, current_dir)

        # Still shoot if enemy is in front while retreating
        if closest_enemy and closest_dist < AI_ATTACK_RANGE:
            if can_shoot_at(tank, closest_enemy["x"], closest_enemy["y"]):
                keys["fire"] = True

    # Track direction for hysteresis
    new_dir = get_rotation_from_input(keys)
    if new_dir >= 0:
        ai_state["last_dir"] = new_dir

    return keys


def dir_to_keys(direction: int) -> dict:
    """Convert a direction (0-7) to movement keys."""
    keys = {
        "up": False,
        "down": False,
        "left": False,
        "right": False,
        "fire": False,
        "grenade": False,
    }
    if direction < 0:
        return keys

    # 0=right, 1=down-right, 2=down, 3=down-left, 4=left, 5=up-left, 6=up, 7=up-right
    if direction in [7, 0, 1]:
        keys["right"] = True
    if direction in [3, 4, 5]:
        keys["left"] = True
    if direction in [1, 2, 3]:
        keys["down"] = True
    if direction in [5, 6, 7]:
        keys["up"] = True

    return keys


def generate_room_id() -> str:
    return "".join(random.choices(string.ascii_lowercase, k=6))


def generate_player_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def create_base(
    field: bytearray, x: int, y: int, player_num: int, changes: list
) -> None:
    """Create a base for a player, tracking changes."""
    wall_type = CHAR_WALL[player_num]

    # Clear the base area
    for dy in range(-BASE_SIZE_Y, BASE_SIZE_Y):
        for dx in range(-BASE_SIZE_X, BASE_SIZE_X):
            fx = (x + dx) % FIELD_WIDTH
            fy = (y + dy) % FIELD_HEIGHT
            if field_get(field, fx, fy) != CHAR_EMPTY:
                field_set(field, fx, fy, CHAR_EMPTY)
                changes.append([fx, fy, CHAR_EMPTY])

    # Draw walls
    for dy in range(-BASE_SIZE_Y, BASE_SIZE_Y):
        fy = (y + dy) % FIELD_HEIGHT
        for fx_raw in [x - BASE_SIZE_X, x + BASE_SIZE_X - 1]:
            fx = fx_raw % FIELD_WIDTH
            field_set(field, fx, fy, wall_type)
            changes.append([fx, fy, wall_type])

    for dx in range(-BASE_SIZE_X, -BASE_DOOR_SIZE + 1):
        fx = (x + dx) % FIELD_WIDTH
        for fy_raw in [y - BASE_SIZE_Y, y + BASE_SIZE_Y - 1]:
            fy = fy_raw % FIELD_HEIGHT
            field_set(field, fx, fy, wall_type)
            changes.append([fx, fy, wall_type])

    for dx in range(BASE_DOOR_SIZE, BASE_SIZE_X):
        fx = (x + dx) % FIELD_WIDTH
        for fy_raw in [y - BASE_SIZE_Y, y + BASE_SIZE_Y - 1]:
            fy = fy_raw % FIELD_HEIGHT
            field_set(field, fx, fy, wall_type)
            changes.append([fx, fy, wall_type])


def generate_field(player_count: int, seed: int) -> tuple[bytearray, dict]:
    """Generate the game field deterministically from seed. Returns (field, tanks)."""
    rng = random.Random(seed)

    # Pre-generate random bytes for dirt pattern (1 byte = 8 cells)
    num_bytes = (FIELD_WIDTH * FIELD_HEIGHT + 7) // 8
    random_bytes = rng.getrandbits(num_bytes * 8).to_bytes(num_bytes, "little")

    # Initialize field as bytearray — no border, world wraps
    field = bytearray(FIELD_WIDTH * FIELD_HEIGHT)
    byte_idx = 0
    bit_mask = 1
    for y in range(FIELD_HEIGHT):
        for x in range(FIELD_WIDTH):
            field_set(
                field,
                x,
                y,
                CHAR_DIRT1 if random_bytes[byte_idx] & bit_mask else CHAR_DIRT2,
            )
            bit_mask <<= 1
            if bit_mask > 128:
                bit_mask = 1
                byte_idx += 1

    # Add some random rock formations
    for _ in range(20):
        cx = rng.randint(0, FIELD_WIDTH - 1)
        cy = rng.randint(0, FIELD_HEIGHT - 1)
        for dy in range(-10, 11):
            for dx in range(-10, 11):
                if dx * dx + dy * dy < 100 and rng.random() < 0.7:
                    fx = (cx + dx) % FIELD_WIDTH
                    fy = (cy + dy) % FIELD_HEIGHT
                    field_set(field, fx, fy, CHAR_ROCK)

    # Generate base positions
    tanks = {}
    min_dist = 120

    for i in range(player_count):
        for _ in range(100):
            bx = rng.randint(30, FIELD_WIDTH - 30)
            by = rng.randint(30, FIELD_HEIGHT - 30)

            too_close = False
            for t in tanks.values():
                # Toroidal distance for base placement
                dx = abs(bx - t["baseX"])
                dy = abs(by - t["baseY"])
                dx = min(dx, FIELD_WIDTH - dx)
                dy = min(dy, FIELD_HEIGHT - dy)
                if math.sqrt(dx * dx + dy * dy) < min_dist:
                    too_close = True
                    break

            if not too_close:
                create_base(field, bx, by, i + 1, [])  # Don't track changes at init
                tanks[str(i + 1)] = {
                    "x": float(bx),
                    "y": float(by),
                    "baseX": bx,
                    "baseY": by,
                    "rotation": 6,
                    "ammo": 30,
                    "shields": 1.0,
                    "kills": 0,
                    "lastHitBy": None,
                    "deathTimer": 0.0,
                    "lastShot": 0.0,
                }
                break

    return field, tanks


def check_collision(
    field: bytearray,
    tanks: dict,
    x: float,
    y: float,
    exclude_player: int,
    rotation: int = 0,
) -> dict:
    """Check if position collides with terrain or other tanks.

    Checks 5 points at the front edge of the tank.
    Returns dig_fraction (0.0-1.0) indicating how much of the front is in dirt.
    """
    perp_rot = (rotation + 2) % 8  # Perpendicular direction

    # 5 check points across the front: gun at 4 pixels, wheels/body at 3 pixels
    check_points = [
        # Center front (gun sticks out 1 more)
        (x + DIR_X[rotation] * 4, y + DIR_Y[rotation] * 4),
        # Inner left and right (body)
        (
            x + DIR_X[rotation] * 3 - DIR_X[perp_rot] * 1,
            y + DIR_Y[rotation] * 3 - DIR_Y[perp_rot] * 1,
        ),
        (
            x + DIR_X[rotation] * 3 + DIR_X[perp_rot] * 1,
            y + DIR_Y[rotation] * 3 + DIR_Y[perp_rot] * 1,
        ),
        # Outer left and right (wheels)
        (
            x + DIR_X[rotation] * 3 - DIR_X[perp_rot] * 2,
            y + DIR_Y[rotation] * 3 - DIR_Y[perp_rot] * 2,
        ),
        (
            x + DIR_X[rotation] * 3 + DIR_X[perp_rot] * 2,
            y + DIR_Y[rotation] * 3 + DIR_Y[perp_rot] * 2,
        ),
    ]

    dirt_count = 0

    for px, py in check_points:
        fx = int(px) % FIELD_WIDTH
        fy = int(py) % FIELD_HEIGHT

        terrain = field_get(field, fx, fy)
        if is_solid(terrain):
            return {"blocked": True, "dig_fraction": 0.0}
        if is_diggable(terrain):
            dirt_count += 1

    # Check for tank collisions using the center point (toroidal distance)
    for pnum, tank in tanks.items():
        if int(pnum) == exclude_player:
            continue
        if tank["deathTimer"] > 0:
            continue
        dx = abs(x - tank["x"])
        dy = abs(y - tank["y"])
        dx = min(dx, FIELD_WIDTH - dx)
        dy = min(dy, FIELD_HEIGHT - dy)
        if dx < 6 and dy < 6:
            return {"blocked": True, "dig_fraction": 0.0}

    return {"blocked": False, "dig_fraction": dirt_count / 5.0}


def dig_terrain(field: bytearray, x: float, y: float, changes: list) -> None:
    """Dig terrain around position, recording changes."""
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            fx = int(x + dx) % FIELD_WIDTH
            fy = int(y + dy) % FIELD_HEIGHT
            if is_diggable(field_get(field, fx, fy)):
                field_set(field, fx, fy, CHAR_EMPTY)
                changes.append([fx, fy, CHAR_EMPTY])


def is_in_base(tank: dict) -> bool:
    dx = abs(tank["x"] - tank["baseX"])
    dy = abs(tank["y"] - tank["baseY"])
    return dx <= BASE_SIZE_X and dy <= BASE_SIZE_Y


def get_rotation_from_input(inp: dict) -> int:
    up = inp.get("up", False)
    down = inp.get("down", False)
    left = inp.get("left", False)
    right = inp.get("right", False)

    if up and right:
        return 7
    if up and left:
        return 5
    if down and right:
        return 1
    if down and left:
        return 3
    if right:
        return 0
    if down:
        return 2
    if left:
        return 4
    if up:
        return 6
    return -1


def _is_pickup_eligible(field: bytearray, x: int, y: int) -> bool:
    """Check if a position is eligible for a pickup (dirt or empty, with clearance from rock/wall)."""
    if is_solid(field_get(field, x, y)):
        return False
    # Check 1px clearance so the visual cross doesn't overlap into rock
    for dy in [-1, 0, 1]:
        for dx in [-1, 0, 1]:
            nx, ny = (x + dx) % FIELD_WIDTH, (y + dy) % FIELD_HEIGHT
            if is_solid(field_get(field, nx, ny)):
                return False
    return True


def spawn_pickups(field: bytearray, tanks: dict, count: int) -> list:
    """Spawn ammo pickups at random positions (dirt or empty), not inside any base."""
    pickups = []
    attempts = 0
    while len(pickups) < count and attempts < count * 20:
        attempts += 1
        x = random.randint(0, FIELD_WIDTH - 1)
        y = random.randint(0, FIELD_HEIGHT - 1)
        if not _is_pickup_eligible(field, x, y):
            continue
        # Check not inside any base
        in_base = False
        for tank in tanks.values():
            if (
                abs(x - tank["baseX"]) < BASE_SIZE_X + 5
                and abs(y - tank["baseY"]) < BASE_SIZE_Y + 5
            ):
                in_base = True
                break
        if not in_base:
            pickups.append({"x": x, "y": y})
    return pickups


def respawn_pickup(field: bytearray, tanks: dict) -> dict | None:
    """Respawn a single pickup at a new random position."""
    for _ in range(200):
        x = random.randint(0, FIELD_WIDTH - 1)
        y = random.randint(0, FIELD_HEIGHT - 1)
        if not _is_pickup_eligible(field, x, y):
            continue
        in_base = False
        for tank in tanks.values():
            if (
                abs(x - tank["baseX"]) < BASE_SIZE_X + 5
                and abs(y - tank["baseY"]) < BASE_SIZE_Y + 5
            ):
                in_base = True
                break
        if not in_base:
            return {"x": x, "y": y}
    return None


def process_game_tick(room_id: str, room_data: dict, dt: float) -> dict | None:
    """Process one game tick. Returns state delta to broadcast."""
    if room_data.get("game_over"):
        return None

    field = room_data["field"]  # bytearray
    tanks = room_data["tanks"]
    bullets = room_data.get("bullets", [])
    grenades = room_data.get("grenades", [])
    inputs = room_data.get("inputs", {})

    # Get game constants from settings
    c = get_game_constants(room_data.get("settings"))

    changes = []  # Field changes: [x, y, value]
    now = time.time()

    # Process AI inputs for bots
    ai_states = get_ai_states(room_id)
    for pnum, ai_state in ai_states.items():
        tank = tanks.get(pnum)
        if tank:
            try:
                ai_input = process_ai_input(ai_state, tank, pnum, field, tanks)
                inputs[pnum] = ai_input
            except Exception as e:
                logger.error(f"AI error for bot {pnum}: {e}")
                inputs[pnum] = {
                    "up": False,
                    "down": False,
                    "left": False,
                    "right": False,
                    "fire": False,
                    "grenade": False,
                }

    # Process each tank
    for pnum, tank in tanks.items():
        inp = inputs.get(pnum, {})

        if tank["deathTimer"] > 0:
            tank["deathTimer"] -= dt
            if tank["deathTimer"] <= 0:
                tank["x"] = float(tank["baseX"])
                tank["y"] = float(tank["baseY"])
                tank["ammo"] = c["start_ammo"]
                tank["shields"] = 1.0
                tank["rotation"] = 6
                tank["deathTimer"] = 0.0
                tank["lastHitBy"] = None
            continue

        # Movement
        rot = get_rotation_from_input(inp)
        if rot >= 0:
            tank["rotation"] = rot

            new_x = tank["x"] + DIR_X[rot] * 0.5
            new_y = tank["y"] + DIR_Y[rot] * 0.5

            # Check collision at front edge of tank in direction of movement
            collision = check_collision(
                field, tanks, new_x, new_y, int(pnum), rotation=rot
            )

            # Speed is proportional to how much dirt we're digging through
            # Even a little dirt slows you down: remap so 1/5 dirt → 3/7 fraction
            dirt_count = collision["dig_fraction"] * 5  # 0-5
            dig_fraction = (dirt_count + 2) / 7 if dirt_count >= 1 else 0.0
            turbo = inp.get("turbo", False)
            # Turbo dig: 2x dig speed, 3x shield cost
            effective_dig_speed = c["dig_speed"] * (2.0 if turbo else 1.0)
            speed = c["tank_speed"] - (c["tank_speed"] - effective_dig_speed) * dig_fraction

            if not collision["blocked"]:
                tank["x"] = (tank["x"] + DIR_X[rot] * speed * dt) % FIELD_WIDTH
                tank["y"] = (tank["y"] + DIR_Y[rot] * speed * dt) % FIELD_HEIGHT
                dig_terrain(field, tank["x"], tank["y"], changes)
                # Digging costs shields (slightly more than outdoor recovery)
                if dig_fraction > 0:
                    cost_mult = 3.0 if turbo else 1.0
                    tank["shields"] -= c["dig_shield_cost"] * dig_fraction * cost_mult * dt

        # Shooting
        fire = inp.get("fire", False)
        turbo = inp.get("turbo", False)
        if fire and (now - tank["lastShot"]) > c["fire_delay"]:
            ammo_cost = 3 if turbo else 1
            if tank["ammo"] >= ammo_cost:
                tank["lastShot"] = now
                tank["ammo"] -= ammo_cost
                rot = tank["rotation"]
                if turbo:
                    # Two parallel bullets, 1 pixel apart perpendicular to direction
                    perp = (rot + 2) % 8
                    for offset in [-1, 1]:
                        bullets.append(
                            {
                                "x": tank["x"] + DIR_X[rot] * 4 + DIR_X[perp] * offset,
                                "y": tank["y"] + DIR_Y[rot] * 4 + DIR_Y[perp] * offset,
                                "rot": rot,
                                "owner": int(pnum),
                                "age": 0,
                            }
                        )
                else:
                    bullets.append(
                        {
                            "x": tank["x"] + DIR_X[rot] * 4,
                            "y": tank["y"] + DIR_Y[rot] * 4,
                            "rot": rot,
                            "owner": int(pnum),
                            "age": 0,
                        }
                    )

        # Grenade dropping
        grenade = inp.get("grenade", False)
        if grenade and tank["ammo"] >= c["grenade_cost_ammo"]:
            # Check if we just pressed grenade (not holding)
            if not tank.get("grenade_held", False):
                tank["grenade_held"] = True
                tank["ammo"] -= c["grenade_cost_ammo"]
                grenades.append(
                    {
                        "x": tank["x"],
                        "y": tank["y"],
                        "owner": int(pnum),
                    }
                )
        else:
            tank["grenade_held"] = False

        # Repair shields (faster inside base, ammo doesn't regenerate)
        if is_in_base(tank):
            tank["shields"] = min(1.0, tank["shields"] + c["repair_shield_in"] * dt)
        else:
            tank["shields"] = min(1.0, tank["shields"] + c["repair_shield_out"] * dt)

        # Death check (shields only - energy/ammo depletion doesn't kill)
        if tank["shields"] <= 0:
            tank["deathTimer"] = c["death_time"]
            tank["shields"] = 0.0
            # Credit kill to lastHitBy
            last_hit = tank.get("lastHitBy")
            if last_hit is not None:
                killer = tanks.get(str(last_hit))
                if killer:
                    killer["kills"] += 1
            tank["lastHitBy"] = None

    # Process ammo pickup collection
    pickups = room_data.get("pickups", [])
    for pnum, tank in tanks.items():
        if tank["deathTimer"] > 0:
            continue
        for i in range(len(pickups) - 1, -1, -1):
            p = pickups[i]
            pdx = abs(tank["x"] - p["x"])
            pdy = abs(tank["y"] - p["y"])
            pdx = min(pdx, FIELD_WIDTH - pdx)
            pdy = min(pdy, FIELD_HEIGHT - pdy)
            if pdx < 3 and pdy < 3:
                tank["ammo"] = min(100, tank["ammo"] + c["pickup_ammo"])
                new_p = respawn_pickup(field, tanks)
                if new_p:
                    pickups[i] = new_p
                else:
                    pickups.pop(i)
    room_data["pickups"] = pickups

    # Process bullets - trace line from old to new position
    new_bullets = []
    for bullet in bullets:
        old_x = bullet["x"]
        old_y = bullet["y"]
        new_x = old_x + DIR_X[bullet["rot"]] * c["bullet_speed"] * dt
        new_y = old_y + DIR_Y[bullet["rot"]] * c["bullet_speed"] * dt

        # Trace line from old to new position
        dx = new_x - old_x
        dy = new_y - old_y
        dist = max(abs(dx), abs(dy), 1)
        steps = int(dist) + 1

        bullet_alive = True
        hit_tank = False
        bounced = False

        for step in range(steps):
            t = step / steps
            px = old_x + dx * t
            py = old_y + dy * t
            fx = int(px) % FIELD_WIDTH
            fy = int(py) % FIELD_HEIGHT

            terrain = field_get(field, fx, fy)

            if is_diggable(terrain):
                field_set(field, fx, fy, CHAR_EMPTY)
                changes.append([fx, fy, CHAR_EMPTY])
                bullet_alive = False
                break

            if is_solid(terrain):
                # Bounce off rock/wall — check sides from the position before the rock
                rot = bullet["rot"]
                prev_x = px - DIR_X[rot]
                prev_y = py - DIR_Y[rot]
                left_dir = (rot + 6) % 8
                right_dir = (rot + 2) % 8
                lx = int(prev_x + DIR_X[left_dir]) % FIELD_WIDTH
                ly = int(prev_y + DIR_Y[left_dir]) % FIELD_HEIGHT
                rx = int(prev_x + DIR_X[right_dir]) % FIELD_WIDTH
                ry = int(prev_y + DIR_Y[right_dir]) % FIELD_HEIGHT
                left_blocked = is_solid(field_get(field, lx, ly))
                right_blocked = is_solid(field_get(field, rx, ry))
                if left_blocked and not right_blocked:
                    bullet["rot"] = right_dir
                elif right_blocked and not left_blocked:
                    bullet["rot"] = left_dir
                else:
                    # Both blocked or neither: reverse
                    bullet["rot"] = (rot + 4) % 8
                bullet["x"] = prev_x % FIELD_WIDTH
                bullet["y"] = prev_y % FIELD_HEIGHT
                bounced = True
                break

            # Check tank hit (toroidal distance)
            for pnum, tank in tanks.items():
                if int(pnum) == bullet["owner"] and bullet.get("age", 0) < 3:
                    continue
                if tank["deathTimer"] > 0:
                    continue
                bdx = abs(px - tank["x"])
                bdy = abs(py - tank["y"])
                bdx = min(bdx, FIELD_WIDTH - bdx)
                bdy = min(bdy, FIELD_HEIGHT - bdy)
                if bdx < 3 and bdy < 3:
                    tank["shields"] -= c["shot_damage"]
                    if int(pnum) != bullet["owner"]:
                        tank["lastHitBy"] = bullet["owner"]
                    hit_tank = True
                    bullet_alive = False
                    break
            if hit_tank:
                break

            # Check grenade hit - only owner can detonate their own grenades
            for grenade in grenades:
                if grenade["owner"] == bullet["owner"]:
                    gpx = abs(px - grenade["x"])
                    gpy = abs(py - grenade["y"])
                    gpx = min(gpx, FIELD_WIDTH - gpx)
                    gpy = min(gpy, FIELD_HEIGHT - gpy)
                    if gpx < 2 and gpy < 2:
                        grenade["exploded"] = True
                        bullet_alive = False
                        break

        if bullet_alive or bounced:
            if not bounced:
                bullet["x"] = new_x % FIELD_WIDTH
                bullet["y"] = new_y % FIELD_HEIGHT
            bullet["age"] = bullet.get("age", 0) + 1
            new_bullets.append(bullet)

    room_data["bullets"] = new_bullets

    # Check grenade proximity to enemy tanks (toroidal distance)
    for grenade in grenades:
        if grenade.get("exploded", False):
            continue
        for pnum, tank in tanks.items():
            if int(pnum) == grenade["owner"]:
                continue  # Owner doesn't trigger their own grenade
            if tank["deathTimer"] > 0:
                continue
            gdx = abs(tank["x"] - grenade["x"])
            gdy = abs(tank["y"] - grenade["y"])
            gdx = min(gdx, FIELD_WIDTH - gdx)
            gdy = min(gdy, FIELD_HEIGHT - gdy)
            dist = math.sqrt(gdx * gdx + gdy * gdy)
            if dist < 5:
                grenade["exploded"] = True
                break

    # Process grenade explosions
    new_grenades = []
    explosions = []
    grenade_radius = c["grenade_radius"]
    for grenade in grenades:
        if grenade.get("exploded", False):
            # Grenade explodes - damage all tanks in radius
            explosions.append(
                {"x": grenade["x"], "y": grenade["y"], "radius": grenade_radius}
            )
            for pnum, tank in tanks.items():
                if tank["deathTimer"] > 0:
                    continue
                gdx = abs(tank["x"] - grenade["x"])
                gdy = abs(tank["y"] - grenade["y"])
                gdx = min(gdx, FIELD_WIDTH - gdx)
                gdy = min(gdy, FIELD_HEIGHT - gdy)
                dist = math.sqrt(gdx * gdx + gdy * gdy)
                if dist < grenade_radius:
                    # Damage falls off with distance
                    damage = c["grenade_damage"] * (1.0 - dist / grenade_radius)
                    tank["shields"] -= damage
                    if int(pnum) != grenade["owner"]:
                        tank["lastHitBy"] = grenade["owner"]
            # Dig out terrain around explosion
            for dy in range(-grenade_radius, grenade_radius + 1):
                for dx in range(-grenade_radius, grenade_radius + 1):
                    if dx * dx + dy * dy <= grenade_radius * grenade_radius:
                        fx = int(grenade["x"] + dx) % FIELD_WIDTH
                        fy = int(grenade["y"] + dy) % FIELD_HEIGHT
                        terrain = field_get(field, fx, fy)
                        if is_diggable(terrain):
                            field_set(field, fx, fy, CHAR_EMPTY)
                            changes.append([fx, fy, CHAR_EMPTY])
        else:
            new_grenades.append(grenade)

    room_data["grenades"] = new_grenades

    # Win condition check: first to 2 * num_players + 1 kills wins
    num_players = len(tanks)
    kill_target = 2 * num_players + 1
    for pnum, tank in tanks.items():
        if tank["kills"] >= kill_target:
            room_data["game_over"] = True
            # Return game_over message instead of normal state_update
            return {
                "type": "game_over",
                "winner": int(pnum),
                "tanks": tanks,
                "broadcast": True,
            }

    # Convert changes to client terrain format
    client_changes = [[x, y, CHAR_TO_CLIENT.get(v, 0)] for x, y, v in changes]

    return {
        "type": "state_update",
        "tanks": tanks,
        "bullets": new_bullets,
        "grenades": new_grenades,
        "explosions": explosions,
        "pickups": room_data.get("pickups", []),
        "changes": client_changes,
        "broadcast": True,
    }


# AI state storage (in-memory only, not persisted - contains sets)
_AI_STATES: dict[str, dict] = {}  # room_id -> {player_num: ai_state}


def get_ai_states(room_id: str) -> dict:
    return _AI_STATES.get(room_id, {})


def set_ai_states(room_id: str, states: dict) -> None:
    _AI_STATES[room_id] = states


# Room state storage
if settings.DEBUG:
    _ROOMS: dict[str, dict] = {}
    _INPUTS: dict[str, dict] = {}  # room_id -> {player_num: keys}
    _ACKS: dict[str, list] = {}  # room_id -> [msg_id, ...]

    def get_room(room_id: str) -> dict:
        if room_id not in _ROOMS:
            _ROOMS[room_id] = {"players": {}, "host": None, "started": False}
        return _ROOMS[room_id]

    def save_room(room_id: str, data: dict) -> None:
        # In debug mode, just store directly (bytearray is fine in memory)
        _ROOMS[room_id] = data

    def set_input(room_id: str, player_num: str, keys: dict) -> None:
        if room_id not in _INPUTS:
            _INPUTS[room_id] = {}
        _INPUTS[room_id][player_num] = keys

    def get_all_inputs(room_id: str) -> dict:
        return _INPUTS.get(room_id, {})

    def add_ack(room_id: str, msg_id: str) -> None:
        if room_id not in _ACKS:
            _ACKS[room_id] = []
        _ACKS[room_id].append(msg_id)

    def pop_acks(room_id: str) -> list:
        acks = _ACKS.get(room_id, [])
        _ACKS[room_id] = []
        return acks

else:
    import redis

    _redis = redis.Redis(host="127.0.0.1", port=6379, db=0)
    ROOM_PREFIX = "tunneler:room:"
    INPUT_PREFIX = "tunneler:inputs:"
    ACK_PREFIX = "tunneler:acks:"
    ROOM_TTL = 3600 * 4
    INPUT_TTL = 60  # Inputs expire after 60s of inactivity
    ACK_TTL = 10  # Acks expire quickly

    def get_room(room_id: str) -> dict:
        data = _redis.get(ROOM_PREFIX + room_id)
        if data:
            room = json.loads(data)
            # Convert field string back to bytearray
            if "field" in room and isinstance(room["field"], str):
                room["field"] = bytearray(room["field"].encode("latin-1"))
            return room
        return {"players": {}, "host": None, "started": False}

    def save_room(room_id: str, data: dict) -> None:
        to_save = data
        # Convert bytearray field to string for JSON storage
        if "field" in data and isinstance(data["field"], bytearray):
            to_save = data.copy()
            to_save["field"] = data["field"].decode("latin-1")
        _redis.setex(ROOM_PREFIX + room_id, ROOM_TTL, json.dumps(to_save))

    def set_input(room_id: str, player_num: str, keys: dict) -> None:
        key = INPUT_PREFIX + room_id
        _redis.hset(key, player_num, json.dumps(keys))
        _redis.expire(key, INPUT_TTL)

    def get_all_inputs(room_id: str) -> dict:
        raw = _redis.hgetall(INPUT_PREFIX + room_id)
        return {k.decode(): json.loads(v) for k, v in raw.items()}

    def add_ack(room_id: str, msg_id: str) -> None:
        key = ACK_PREFIX + room_id
        _redis.rpush(key, msg_id)
        _redis.expire(key, ACK_TTL)

    def pop_acks(room_id: str) -> list:
        key = ACK_PREFIX + room_id
        # Get all and delete atomically using pipeline
        pipe = _redis.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        results = pipe.execute()
        return [a.decode() for a in results[0]]


class Tunneler(Project):
    def fill_dict(self, request, d):
        room = request.GET.get("room")
        player = request.GET.get("player")
        if not room:
            raise RedirectException(
                f"/projects/tunneler?room={generate_room_id()}&player={generate_player_id()}"
            )
        if not player:
            raise RedirectException(
                f"/projects/tunneler?room={room}&player={generate_player_id()}"
            )
        d["room"] = room
        d["player"] = player

    def receive(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        msg_type = payload.get("type")
        room = payload.get("room")
        room_data = get_room(room)

        if msg_type == "rejoin":
            # Auto-rejoin check on page load
            player_id = payload.get("player_id")
            if room_data.get("started"):
                player_num = room_data.get("player_numbers", {}).get(player_id)
                if player_num:
                    return {
                        "type": "game_started",
                        "player_numbers": room_data["player_numbers"],
                        "players": room_data["players"],
                        "host": room_data["host"],
                        "field_packed": room_data["field"].decode("latin-1"),
                        "tanks": room_data["tanks"],
                        "bullets": room_data.get("bullets", []),
                        "grenades": room_data.get("grenades", []),
                        "pickups": room_data.get("pickups", []),
                        "rejoined": True,
                        "broadcast": False,
                    }
            return {"type": "not_in_game", "broadcast": False}

        if msg_type == "join":
            player_id = payload.get("player_id")
            name = payload.get("name")

            if room_data.get("started"):
                player_num = room_data.get("player_numbers", {}).get(player_id)
                if player_num:
                    return {
                        "type": "game_started",
                        "player_numbers": room_data["player_numbers"],
                        "players": room_data["players"],
                        "host": room_data["host"],
                        "field_packed": room_data["field"].decode("latin-1"),
                        "tanks": room_data["tanks"],
                        "bullets": room_data.get("bullets", []),
                        "grenades": room_data.get("grenades", []),
                        "pickups": room_data.get("pickups", []),
                        "rejoined": True,
                        "broadcast": False,
                    }
                else:
                    return {"type": "game_in_progress", "broadcast": False}

            if len(room_data["players"]) >= 4 and player_id not in room_data["players"]:
                return {"type": "room_full", "broadcast": False}

            room_data["players"][player_id] = {"name": name}
            if room_data["host"] is None:
                room_data["host"] = player_id

            save_room(room, room_data)
            return {
                "type": "player_list",
                "players": room_data["players"],
                "host": room_data["host"],
                "broadcast": True,
            }

        elif msg_type == "leave":
            player_id = payload.get("player_id")
            # Don't remove players from an in-progress game (they might rejoin)
            if room_data.get("started"):
                return None
            if player_id in room_data["players"]:
                del room_data["players"][player_id]
                if room_data["host"] == player_id:
                    room_data["host"] = next(iter(room_data["players"]), None)

            save_room(room, room_data)
            return {
                "type": "player_list",
                "players": room_data["players"],
                "host": room_data["host"],
                "broadcast": True,
            }

        elif msg_type == "add_bot":
            player_id = payload.get("player_id")
            # Only host can add bots
            if player_id != room_data["host"]:
                return None
            if room_data.get("started"):
                return None
            if len(room_data["players"]) >= 4:
                return {"type": "room_full", "broadcast": False}

            # Generate bot ID and name
            bot_count = sum(1 for p in room_data["players"].values() if p.get("is_bot"))
            bot_id = f"bot_{bot_count + 1}"
            bot_name = BOT_NAMES[bot_count % len(BOT_NAMES)]

            room_data["players"][bot_id] = {"name": bot_name, "is_bot": True}

            save_room(room, room_data)
            return {
                "type": "player_list",
                "players": room_data["players"],
                "host": room_data["host"],
                "broadcast": True,
            }

        elif msg_type == "start_game":
            t0 = time.time()
            player_id = payload.get("player_id")
            if player_id != room_data["host"]:
                return None

            room_data["started"] = True
            player_ids = list(room_data["players"].keys())
            player_numbers = {pid: i + 1 for i, pid in enumerate(player_ids)}
            room_data["player_numbers"] = player_numbers

            # Store settings from host
            room_data["settings"] = payload.get("settings", {})

            seed = random.randint(0, 2**31)
            room_data["seed"] = seed

            field, tanks = generate_field(len(player_ids), seed)
            room_data["field"] = field
            room_data["tanks"] = tanks
            room_data["bullets"] = []
            room_data["grenades"] = []
            room_data["inputs"] = {}
            room_data["last_tick"] = time.time()

            # Spawn ammo pickups
            gc = get_game_constants(room_data.get("settings"))
            room_data["pickups"] = spawn_pickups(field, tanks, gc["pickup_count"])

            # Initialize AI states for bots (stored separately, not in room_data)
            ai_states = {}
            for pid, pinfo in room_data["players"].items():
                if pinfo.get("is_bot"):
                    pnum = player_numbers[pid]
                    tank = tanks[str(pnum)]
                    ai_states[str(pnum)] = create_bot_state(
                        tank["baseX"], tank["baseY"]
                    )
            set_ai_states(room, ai_states)

            save_room(room, room_data)
            logger.info(
                f"[{room}] start_game with settings={room_data['settings']} took {time.time() - t0:.3f}s"
            )

            return {
                "type": "game_started",
                "player_numbers": player_numbers,
                "players": room_data["players"],
                "field_packed": field.decode("latin-1"),
                "tanks": tanks,
                "pickups": room_data.get("pickups", []),
                "broadcast": True,
            }

        elif msg_type == "input":
            player_id = payload.get("player_id")
            if not room_data.get("started"):
                return None

            player_num = room_data.get("player_numbers", {}).get(player_id)
            if player_num is None:
                return None

            # Store input for this player (separate from room state)
            keys = payload.get("keys", {})
            set_input(room, str(player_num), keys)

            # Track message for latency measurement
            msg_id = payload.get("msg_id")
            if msg_id:
                add_ack(room, msg_id)

            logger.info(f"[{room}] input P{player_num}: {keys}")
            return None

        elif msg_type == "tick":
            player_id = payload.get("player_id")
            if not room_data.get("started"):
                return None

            # Only accept ticks from host, unless host is inactive (>1 sec since last tick)
            now = time.time()
            last_tick = room_data.get("last_tick", now)
            is_host = player_id == room_data.get("host")
            host_inactive = (now - last_tick) > 1.0

            if not is_host:
                if not host_inactive:
                    return None
                # Host is inactive, this player becomes the new host
                if (
                    player_id in room_data.get("player_numbers", {}).values()
                    or player_id in room_data.get("player_numbers", {}).keys()
                ):
                    room_data["host"] = player_id
                    logger.info(
                        f"[{room}] Host transferred to {player_id} (previous host inactive)"
                    )
                else:
                    return None

            # Calculate dt
            dt = min(now - last_tick, 0.1)  # Cap at 100ms
            room_data["last_tick"] = now

            # Get inputs from separate storage (no race condition)
            inputs = get_all_inputs(room)
            room_data["inputs"] = inputs

            # Log inputs being processed
            active_inputs = {p: k for p, k in inputs.items() if any(k.values())}
            if active_inputs:
                logger.info(f"[{room}] tick dt={dt:.3f}s inputs={active_inputs}")

            # Process game tick
            result = process_game_tick(room, room_data, dt)

            if result is None:
                return None

            save_room(room, room_data)

            # Include acks for latency measurement
            acks = pop_acks(room)
            if acks:
                result["acks"] = acks

            # Include current host so clients can update their isHost status
            result["host"] = room_data["host"]

            return result

        elif msg_type == "new_game":
            player_id = payload.get("player_id")
            if player_id != room_data.get("host"):
                return None

            # Reset room to lobby state
            room_data["started"] = False
            room_data.pop("game_over", None)
            room_data.pop("field", None)
            room_data.pop("tanks", None)
            room_data.pop("bullets", None)
            room_data.pop("grenades", None)
            room_data.pop("pickups", None)
            room_data.pop("inputs", None)
            room_data.pop("player_numbers", None)
            room_data.pop("seed", None)
            room_data.pop("last_tick", None)
            room_data.pop("settings", None)

            # Clear AI states
            set_ai_states(room, {})

            save_room(room, room_data)
            return {
                "type": "return_to_lobby",
                "players": room_data["players"],
                "host": room_data["host"],
                "broadcast": True,
            }

        return None
