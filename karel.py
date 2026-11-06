"""
karel.py — a self-contained Karel the Robot environment.

STUDENT USAGE
    Place this file next to your code and write:

        from karel import *

    then use:
        move()           — step forward one cell
        turn_left()      — rotate 90° counter-clockwise
        turn_right()     — rotate 90° clockwise
        put_beeper()     — place a beeper on the current cell
        pick_beeper()    — pick up a beeper from the current cell
        display()        — print the world to the screen

    Sensor functions (return True / False):
        front_is_clear() / front_is_blocked()
        left_is_clear()  / left_is_blocked()
        right_is_clear() / right_is_blocked()
        beepers_present() / no_beepers_present()
        beepers_in_bag()  / no_beepers_in_bag()
        facing_north() / facing_south() / facing_east() / facing_west()
        not_facing_north() / not_facing_south() / …

    In your __main__ block:
        if __name__ == '__main__':
            new_world(cols=7, rows=7)   # set up the grid
            draw_house()               # run your function
            display()                  # see the result

GRADER USAGE
    import karel
    karel.new_world(cols=7, rows=7, robot_col=1, robot_row=1, robot_dir='E')
    student_module.draw_house()
    assert karel._w.beepers == {(2, 1): 1, (2, 2): 1, …}

WORLD FILE FORMAT  (.w)
    Dimension: <cols> <rows>
    Karel:     <col> <row> <direction>   # direction: E / N / W / S
    BeeperBag: <n | INFINITY>
    Beeper:    <col> <row> <count>
    Wall:      <col> <row> <direction>   # wall on that side of the cell
    # lines starting with # are ignored
"""
from __future__ import annotations

from pathlib import Path

# ── Direction tables ──────────────────────────────────────────────────────────

_TURN_LEFT  = {'E': 'N', 'N': 'W', 'W': 'S', 'S': 'E'}
_TURN_RIGHT = {'E': 'S', 'S': 'W', 'W': 'N', 'N': 'E'}
_OPPOSITE   = {'E': 'W', 'W': 'E', 'N': 'S', 'S': 'N'}
_DELTA      = {'E': (1, 0), 'N': (0, 1), 'W': (-1, 0), 'S': (0, -1)}
_ARROW      = {'E': '>', 'N': '^', 'W': '<', 'S': 'v'}

# ── Exception ─────────────────────────────────────────────────────────────────

class KarelError(Exception):
    """Raised when Karel attempts an impossible action."""

# ── World state ───────────────────────────────────────────────────────────────

class _World:
    def __init__(
        self,
        cols: int = 10,
        rows: int = 10,
        robot_col: int = 1,
        robot_row: int = 1,
        robot_dir: str = 'E',
        bag: int = -1,            # -1 means infinite beepers
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.beepers: dict[tuple[int, int], int] = {}
        self.walls:   set[tuple[tuple[int, int], str]] = set()
        self._start = (robot_col, robot_row, robot_dir, bag)
        self.robot_col = robot_col
        self.robot_row = robot_row
        self.robot_dir = robot_dir
        self.bag       = bag

    def reset(self) -> None:
        """Restore starting position and clear all beepers placed during the run."""
        self.robot_col, self.robot_row, self.robot_dir, self.bag = self._start
        self.beepers.clear()

    def in_bounds(self, col: int, row: int) -> bool:
        return 1 <= col <= self.cols and 1 <= row <= self.rows

    def wall_between(self, col: int, row: int, direction: str) -> bool:
        dc, dr = _DELTA[direction]
        nc, nr = col + dc, row + dr
        return (
            ((col, row), direction) in self.walls
            or ((nc, nr), _OPPOSITE[direction]) in self.walls
        )


_w: _World | None = None


def _world() -> _World:
    global _w
    if _w is None:
        _w = _World()
    return _w


# ── World setup ───────────────────────────────────────────────────────────────

def new_world(
    cols: int = 10,
    rows: int = 10,
    robot_col: int = 1,
    robot_row: int = 1,
    robot_dir: str = 'E',
    bag: int = -1,
) -> None:
    """Create a fresh world.  Call this before running any Karel function."""
    global _w
    _w = _World(cols, rows, robot_col, robot_row, robot_dir, bag)


def load_world(path: str) -> None:
    """Load a world from a .w text file (see module docstring for the format)."""
    global _w
    cols, rows = 10, 10
    robot_col, robot_row, robot_dir = 1, 1, 'E'
    bag: int = -1
    beepers: dict[tuple[int, int], int] = {}
    walls:   set[tuple[tuple[int, int], str]] = set()

    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, _, val = line.partition(':')
        parts = val.split()
        k = key.strip().lower()
        if k == 'dimension':
            cols, rows = int(parts[0]), int(parts[1])
        elif k == 'karel':
            robot_col, robot_row = int(parts[0]), int(parts[1])
            if len(parts) > 2:
                robot_dir = parts[2].upper()
        elif k == 'beeperbag':
            bag = -1 if parts[0].upper() == 'INFINITY' else int(parts[0])
        elif k == 'beeper':
            beepers[(int(parts[0]), int(parts[1]))] = int(parts[2])
        elif k == 'wall':
            walls.add(((int(parts[0]), int(parts[1])), parts[2].upper()))

    _w = _World(cols, rows, robot_col, robot_row, robot_dir, bag)
    _w.beepers = beepers
    _w.walls   = walls


def reset() -> None:
    """Reset Karel to its starting position and clear all beepers placed during the run."""
    _world().reset()


# ── Actions ───────────────────────────────────────────────────────────────────

def move() -> None:
    """Move Karel one step forward.  Raises KarelError if the path is blocked."""
    w = _world()
    if not front_is_clear():
        raise KarelError(
            f"Karel at ({w.robot_col}, {w.robot_row}) facing {w.robot_dir} "
            "tried to move but the path is blocked."
        )
    dc, dr = _DELTA[w.robot_dir]
    w.robot_col += dc
    w.robot_row += dr


def turn_left() -> None:
    """Rotate Karel 90° counter-clockwise."""
    _world().robot_dir = _TURN_LEFT[_world().robot_dir]


def turn_right() -> None:
    """Rotate Karel 90° clockwise."""
    _world().robot_dir = _TURN_RIGHT[_world().robot_dir]


def put_beeper() -> None:
    """Place one beeper on Karel's current cell."""
    w = _world()
    if w.bag == 0:
        raise KarelError("Karel tried to put a beeper but its bag is empty.")
    if w.bag > 0:
        w.bag -= 1
    pos = (w.robot_col, w.robot_row)
    w.beepers[pos] = w.beepers.get(pos, 0) + 1


def pick_beeper() -> None:
    """Pick up one beeper from Karel's current cell."""
    w = _world()
    pos = (w.robot_col, w.robot_row)
    if w.beepers.get(pos, 0) == 0:
        raise KarelError(
            f"Karel tried to pick a beeper at {pos} but there are none there."
        )
    w.beepers[pos] -= 1
    if w.beepers[pos] == 0:
        del w.beepers[pos]
    if w.bag >= 0:
        w.bag += 1


# ── Sensors ───────────────────────────────────────────────────────────────────

def _path_clear(direction: str) -> bool:
    w = _world()
    dc, dr = _DELTA[direction]
    nc, nr = w.robot_col + dc, w.robot_row + dr
    return w.in_bounds(nc, nr) and not w.wall_between(w.robot_col, w.robot_row, direction)


def front_is_clear()    -> bool: return _path_clear(_world().robot_dir)
def front_is_blocked()  -> bool: return not front_is_clear()
def left_is_clear()     -> bool: return _path_clear(_TURN_LEFT[_world().robot_dir])
def left_is_blocked()   -> bool: return not left_is_clear()
def right_is_clear()    -> bool: return _path_clear(_TURN_RIGHT[_world().robot_dir])
def right_is_blocked()  -> bool: return not right_is_clear()

def beepers_present()    -> bool:
    w = _world()
    return w.beepers.get((w.robot_col, w.robot_row), 0) > 0

def no_beepers_present() -> bool: return not beepers_present()
def beepers_in_bag()     -> bool: return _world().bag != 0
def no_beepers_in_bag()  -> bool: return _world().bag == 0

def facing_north()     -> bool: return _world().robot_dir == 'N'
def not_facing_north() -> bool: return not facing_north()
def facing_south()     -> bool: return _world().robot_dir == 'S'
def not_facing_south() -> bool: return not facing_south()
def facing_east()      -> bool: return _world().robot_dir == 'E'
def not_facing_east()  -> bool: return not facing_east()
def facing_west()      -> bool: return _world().robot_dir == 'W'
def not_facing_west()  -> bool: return not facing_west()


# ── ASCII display ─────────────────────────────────────────────────────────────

def display() -> None:
    """Print an ASCII picture of the current Karel world."""
    w = _world()

    # Each cell is 3 characters wide between | separators:
    #   " > "  Karel (no beeper)
    #   " >1"  Karel standing on a beeper
    #   " 1 "  beeper only
    #   "   "  empty

    sep = "   +" + "---+" * w.cols

    print(sep)
    for row in range(w.rows, 0, -1):
        parts = [f"{row:2d} "]
        for col in range(1, w.cols + 1):
            pos = (col, row)
            is_karel = (col == w.robot_col and row == w.robot_row)
            n = w.beepers.get(pos, 0)
            count_char = '+' if n > 9 else (str(n) if n > 0 else ' ')
            if is_karel:
                cell = ' ' + _ARROW[w.robot_dir] + count_char
            else:
                cell = ' ' + count_char + ' '
            parts.append('|' + cell)
        parts.append('|')
        print(''.join(parts))
        print(sep)

    # Column numbers centred under each cell
    # Each column occupies 4 chars in the display (| + 3-char cell),
    # so f" {c:^3}" (1 leading space + number centred in 3) aligns correctly.
    col_labels = "   " + "".join(f" {c:^3}" for c in range(1, w.cols + 1))
    bag_str = "inf" if w.bag < 0 else str(w.bag)
    print(col_labels)
    print(f"   Karel: ({w.robot_col}, {w.robot_row}) facing {w.robot_dir}  bag: {bag_str}")


# ── Public API ────────────────────────────────────────────────────────────────

__all__ = [
    # actions
    'move', 'turn_left', 'turn_right', 'put_beeper', 'pick_beeper',
    # sensors
    'front_is_clear', 'front_is_blocked',
    'left_is_clear',  'left_is_blocked',
    'right_is_clear', 'right_is_blocked',
    'beepers_present', 'no_beepers_present',
    'beepers_in_bag',  'no_beepers_in_bag',
    'facing_north', 'not_facing_north',
    'facing_south', 'not_facing_south',
    'facing_east',  'not_facing_east',
    'facing_west',  'not_facing_west',
    # world setup and display
    'new_world', 'load_world', 'reset', 'display',
    # exception
    'KarelError',
]
