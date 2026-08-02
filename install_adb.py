#!/usr/bin/env python3
"""
Quick ADB  —  by HeX
---------------------
Full-screen terminal UI to install, update, or uninstall Google's
"platform-tools" package (adb + fastboot) system-wide.

Works on:
  - Windows  (default: C:\\platform-tools, updates the machine-wide PATH)
  - Linux    (default: /opt/android-platform-tools, symlinked into /usr/local/bin)

Must be run elevated:
  - Windows: Administrator PowerShell / Command Prompt
  - Linux:   sudo

Run with no arguments for the interactive TUI:
  python3 install_adb.py

Or non-interactively:
  python3 install_adb.py --install [--dir CUSTOM_PATH]
  python3 install_adb.py --update
  python3 install_adb.py --uninstall
  python3 install_adb.py --status
  python3 install_adb.py --no-color   (disable ANSI colors/TUI)
"""

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

DOWNLOAD_URLS = {
    "Windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "Linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    "Darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
}

DEFAULT_DIR = {
    "Windows": r"C:\platform-tools",
    "Linux": "/opt/android-platform-tools",
}

LINUX_SYMLINK_DIR = "/usr/local/bin"


# ----------------------------------------------------------------------
# UI: colors, screen control, and pretty printing
#
# Design references applied here:
#  - opencode.md (terminal-native): near-black chrome, single accent color
#    reserved for action/active-state, hard square edges, monospace-honest,
#    ANSI-echo colors kept out of chrome.
#  - tui-design-skill: reverse video for header/footer hierarchy, muted
#    (not saturated) semantic color, one consistent glyph vocabulary,
#    density over whitespace, NO_COLOR respected, floor tested at 80x24.
# ----------------------------------------------------------------------
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    WHITE = "\033[97m"
    BLACK = "\033[30m"
    # Android-ish green (droid green, ~#A4C639) in 24-bit truecolor —
    # the single accent color: action, active state, selection only.
    DROID = "\033[38;2;164;198;57m"
    DROID_DIM = "\033[38;2;108;131;38m"
    DROID_BG = "\033[48;2;164;198;57m"
    # Muted semantic colors (soft, not saturated) — communicate meaning
    # without visual aggression.
    SOFT_RED = "\033[38;2;224;108;117m"
    SOFT_RED_BG = "\033[48;2;224;108;117m"
    SOFT_YELLOW = "\033[38;2;229;192;123m"
    # Chrome surfaces for header/footer bars (reverse-video style hierarchy)
    SURFACE = "\033[48;2;31;31;31m"
    SURFACE_TEXT = "\033[38;2;210;210;210m"


# Consistent glyph vocabulary used everywhere (never mix alternates in):
GLYPH_OK = "●"        # installed / good
GLYPH_EMPTY = "○"      # not installed
GLYPH_SUCCESS = "✔"
GLYPH_WARN = "⚠"
GLYPH_ERROR = "✖"
GLYPH_POINTER = "❯"
GLYPH_BUSY = "⋯"

USE_COLOR = True


# ----------------------------------------------------------------------
# ASCII logo — responsive across screen sizes.
#
# Three tiers, chosen by live terminal width every time it's drawn (so a
# resized window / SIGWINCH redraw always picks the right one):
#   - LOGO_LARGE  (61 cols)  full block-art wordmark
#   - LOGO_MEDIUM (43 cols)  compact figlet-style wordmark
#   - none                   narrow fallback — just the slim title bar
# This means the banner never wraps, truncates, or looks broken on a
# tmux pane, an SSH session, or a tiny cmd.exe window.
# ----------------------------------------------------------------------
LOGO_LARGE = [
    " ██████╗ ██╗   ██╗██╗ ██████╗██╗  ██╗ █████╗ ██████╗ ██████╗ ",
    "██╔═══██╗██║   ██║██║██╔════╝██║ ██╔╝██╔══██╗██╔══██╗██╔══██╗",
    "██║   ██║██║   ██║██║██║     █████╔╝ ███████║██║  ██║██████╔╝",
    "██║▄▄ ██║██║   ██║██║██║     ██╔═██╗ ██╔══██║██║  ██║██╔══██╗",
    "╚██████╔╝╚██████╔╝██║╚██████╗██║  ██╗██║  ██║██████╔╝██████╔╝",
    " ╚══▀▀═╝  ╚═════╝ ╚═╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═════╝ ",
]
LOGO_LARGE_W = max(len(l) for l in LOGO_LARGE)

LOGO_MEDIUM = [
    "  ___  _   _ ___ ___ _  __    _   ___  ___ ",
    " / _ \\| | | |_ _/ __| |/ /   /_\\ |   \\| _ )",
    "| (_) | |_| || | (__| ' <   / _ \\| |) | _ \\",
    " \\__\\_\\\\___/|___\\___|_|\\_\\ /_/ \\_\\___/|___/",
]
LOGO_MEDIUM_W = max(len(l) for l in LOGO_MEDIUM)

LOGO_TAGLINE = "system-wide adb / fastboot installer"
LOGO_BYLINE = "by HeX"
LOGO_TOP_MARGIN = 4  # blank lines above the logo — bump this to push it down further


def _center(line, width):
    pad = max(width - len(line), 0)
    left = pad // 2
    return " " * left + line + " " * (pad - left)


def logo_block(width):
    """Pick the biggest logo tier that fits `width` and return
    (plain_centered_lines, art_line_count), or None if the terminal is
    too narrow for any ascii-art tier (caller should fall back to a
    plain text banner in that case)."""
    if width >= LOGO_LARGE_W + 4:
        art = LOGO_LARGE
    elif width >= LOGO_MEDIUM_W + 4:
        art = LOGO_MEDIUM
    else:
        return None
    lines = [_center(l, width) for l in art]
    lines.append(_center(LOGO_TAGLINE, width))
    lines.append(_center(LOGO_BYLINE, width))
    return lines, len(art)


def colorize_logo(lines, art_line_count):
    """Style the already-centered logo lines: droid-green + bold for the
    ascii-art rows, dim for the tagline, dim droid-green for the byline."""
    if not USE_COLOR:
        return lines
    out = []
    for i, l in enumerate(lines):
        if i < art_line_count:
            out.append(f"{C.DROID}{C.BOLD}{l}{C.RESET}")
        elif i == art_line_count:
            out.append(f"{C.DIM}{l}{C.RESET}")
        else:
            out.append(f"{C.DROID_DIM}{l}{C.RESET}")
    return out


def _terminal_width(fallback=80):
    try:
        return shutil.get_terminal_size(fallback=(fallback, 24)).columns
    except Exception:
        return fallback


def _terminal_size(fallback=(80, 24)):
    try:
        sz = shutil.get_terminal_size(fallback=fallback)
        return sz.columns, sz.lines
    except Exception:
        return fallback


def box_width():
    """Responsive inner content width for standalone (non-full-screen)
    panels like --status output — capped so a single line of scrolling
    output stays readable even on an ultra-wide terminal."""
    cols = _terminal_width()
    return max(36, min(64, cols - 6))


def screen_width():
    """Full, uncapped terminal width — used by the live full-screen TUI so
    its chrome bars and content genuinely fill the terminal edge to edge."""
    cols, _ = _terminal_size()
    return max(cols, 20)


def screen_height():
    _, rows = _terminal_size()
    return max(rows, 8)


def _enable_ansi_on_windows():
    """Modern Windows terminals support ANSI escapes once VT processing is
    turned on for the console handle. Older cmd.exe windows may not."""
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:
        return False


def _c(text, color):
    return f"{color}{text}{C.RESET}" if USE_COLOR else text


def _pad_row(plain_text, colored_text=None, width=None):
    """Pads a row based on plain_text length, but renders colored_text
    (falls back to plain_text) so ANSI codes never throw off alignment."""
    width = width if width is not None else box_width()
    content = colored_text if (colored_text is not None and USE_COLOR) else plain_text
    pad = max(width - 1 - len(plain_text), 0)
    if USE_COLOR:
        return f"{C.DIM}│{C.RESET}{content}{' ' * pad}{C.DIM}│{C.RESET}"
    return f"|{plain_text}{' ' * pad}|"


def box_top():
    w = box_width()
    return f"{C.DIM}┌{'─' * w}┐{C.RESET}" if USE_COLOR else "+" + "-" * w + "+"


def box_bottom():
    w = box_width()
    return f"{C.DIM}└{'─' * w}┘{C.RESET}" if USE_COLOR else "+" + "-" * w + "+"


def box_divider():
    w = box_width()
    return f"{C.DIM}├{'─' * w}┤{C.RESET}" if USE_COLOR else "+" + "-" * w + "+"


def _compact_banner_lines():
    """Slim boxed banner for terminals too narrow for any ascii-art tier."""
    lines = [box_top()]
    lines.append(_pad_row(" 🤖 QUICK ADB", f" {C.DROID}{C.BOLD}🤖 QUICK ADB{C.RESET}"))
    lines.append(_pad_row(" system-wide adb / fastboot installer",
                           f" {C.DIM}system-wide adb / fastboot installer{C.RESET}"))
    lines.append(_pad_row(" by HeX", f" {C.DROID}by HeX{C.RESET}"))
    lines.append(box_bottom())
    return lines


def banner_lines():
    w = box_width() + 2  # matches hr()/status width so everything lines up
    logo = logo_block(min(w, screen_width()))
    if not logo:
        return _compact_banner_lines()
    art_lines, art_count = logo
    lines = [""] * LOGO_TOP_MARGIN
    lines.extend(colorize_logo(art_lines, art_count))
    lines.append(_c("─" * len(art_lines[0]), C.DROID_DIM))
    return lines


def print_banner():
    for line in banner_lines():
        print(line)
    print()


def hr(char="─", width=None, color=C.DIM):
    width = width if width is not None else box_width() + 2
    print(_c(char * width, color))


def log(msg):
    print(f"{_c('›', C.DROID)} {msg}")


def success(msg):
    print(f"{_c(GLYPH_SUCCESS, C.DROID)} {msg}")


def warn(msg):
    print(f"{_c(GLYPH_WARN, C.SOFT_YELLOW)} {msg}")


def error(msg):
    print(f"{_c(GLYPH_ERROR, C.SOFT_RED)} {msg}")


# ----------------------------------------------------------------------
# Privilege checks
# ----------------------------------------------------------------------
class PrivilegeError(Exception):
    """Raised instead of exiting the process directly, so the TUI loop can
    show the error and return to the menu instead of dying outright."""


def is_admin_windows():
    import ctypes
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def is_root_linux():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def require_privileges(system):
    if system == "Windows" and not is_admin_windows():
        raise PrivilegeError("Run this from an elevated (Administrator) PowerShell / Command Prompt.")
    if system == "Linux" and not is_root_linux():
        raise PrivilegeError("Run this with root privileges:  sudo python3 install_adb.py")


# ----------------------------------------------------------------------
# Config file (remembers where WE installed adb, so update/uninstall work
# even if it was put in a custom location)
# ----------------------------------------------------------------------
def config_path(system):
    if system == "Windows":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return os.path.join(base, "adb-installer", "config.json")
    return "/etc/adb-installer/config.json"


def read_config(system):
    path = config_path(system)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def write_config(system, data):
    path = config_path(system)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def remove_config(system):
    path = config_path(system)
    if os.path.isfile(path):
        os.remove(path)


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------
def get_adb_version(adb_path):
    try:
        out = subprocess.run([adb_path, "version"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip().splitlines()[0] if out.stdout else None
    except Exception:
        return None


def detect_existing_install(system):
    """
    Returns dict describing current state:
      installed: bool
      dir: install directory (or None)
      managed: True if this tool created it (tracked in config)
      adb_path: full path to adb binary (or None)
      version: adb version string (or None)
    """
    cfg = read_config(system)
    managed_dir = cfg.get("install_dir")
    if managed_dir and os.path.isdir(managed_dir):
        adb_name = "adb.exe" if system == "Windows" else "adb"
        adb_path = os.path.join(managed_dir, adb_name)
        if os.path.isfile(adb_path):
            return {
                "installed": True, "dir": managed_dir, "managed": True,
                "adb_path": adb_path, "version": get_adb_version(adb_path),
            }

    which_path = shutil.which("adb")
    if which_path:
        return {
            "installed": True, "dir": os.path.dirname(which_path), "managed": False,
            "adb_path": which_path, "version": get_adb_version(which_path),
        }

    return {"installed": False, "dir": None, "managed": False, "adb_path": None, "version": None}


# ----------------------------------------------------------------------
# Download / extract
# ----------------------------------------------------------------------
def _print_progress_bar(done, total, width=32):
    if total > 0:
        frac = min(done / total, 1.0)
        filled = int(width * frac)
        bar = "█" * filled + "░" * (width - filled)
        pct = f"{frac * 100:5.1f}%"
        mb_done = done / (1024 * 1024)
        mb_total = total / (1024 * 1024)
        line = f"  {_c(bar, C.DROID)} {pct}  ({mb_done:.1f}/{mb_total:.1f} MB)"
    else:
        mb_done = done / (1024 * 1024)
        line = f"  downloading... {mb_done:.1f} MB"
    sys.stdout.write("\r" + line + " " * 8)
    sys.stdout.flush()


def download_platform_tools(system, dest_zip):
    url = DOWNLOAD_URLS[system]
    log(f"Downloading platform-tools from {url}")
    with urllib.request.urlopen(url) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 256 * 1024
        with open(dest_zip, "wb") as out:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                _print_progress_bar(downloaded, total)
    print()
    success(f"Downloaded to {dest_zip}")


def extract_zip(zip_path, extract_to):
    log(f"Extracting to {extract_to}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_to)
    return os.path.join(extract_to, "platform-tools")


# ----------------------------------------------------------------------
# Windows PATH management
# ----------------------------------------------------------------------
def add_to_system_path_windows(new_dir):
    import winreg
    key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                         winreg.KEY_READ | winreg.KEY_WRITE) as key:
        current_path, value_type = winreg.QueryValueEx(key, "Path")
        entries = [p for p in current_path.split(";") if p.strip()]
        if any(os.path.normcase(p) == os.path.normcase(new_dir) for p in entries):
            log("System PATH already contains this directory.")
        else:
            updated_path = current_path.rstrip(";") + ";" + new_dir
            winreg.SetValueEx(key, "Path", 0, value_type, updated_path)
            log("Added directory to the system PATH.")
    _broadcast_env_change_windows()


def remove_from_system_path_windows(old_dir):
    import winreg
    key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                         winreg.KEY_READ | winreg.KEY_WRITE) as key:
        current_path, value_type = winreg.QueryValueEx(key, "Path")
        entries = [p for p in current_path.split(";") if p.strip()]
        new_entries = [p for p in entries if os.path.normcase(p) != os.path.normcase(old_dir)]
        if len(new_entries) != len(entries):
            winreg.SetValueEx(key, "Path", 0, value_type, ";".join(new_entries))
            log("Removed directory from the system PATH.")
    _broadcast_env_change_windows()


def _broadcast_env_change_windows():
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result)
        )
    except Exception:
        pass
    log("Open a new terminal window for the PATH change to take effect.")


# ----------------------------------------------------------------------
# Linux symlink / profile.d management
# ----------------------------------------------------------------------
PROFILE_SNIPPET = "/etc/profile.d/android-platform-tools.sh"


def link_linux(install_dir):
    for name in ("adb", "fastboot"):
        src = os.path.join(install_dir, name)
        link = os.path.join(LINUX_SYMLINK_DIR, name)
        if os.path.isfile(src):
            os.chmod(src, 0o755)
            if os.path.islink(link) or os.path.exists(link):
                os.remove(link)
            os.symlink(src, link)
            log(f"Linked {link} -> {src}")

    with open(PROFILE_SNIPPET, "w") as f:
        f.write(f'export PATH="$PATH:{install_dir}"\n')
    os.chmod(PROFILE_SNIPPET, 0o644)
    log(f"Wrote {PROFILE_SNIPPET} for system-wide PATH.")


def unlink_linux():
    for name in ("adb", "fastboot"):
        link = os.path.join(LINUX_SYMLINK_DIR, name)
        if os.path.islink(link):
            os.remove(link)
            log(f"Removed symlink {link}")
    if os.path.isfile(PROFILE_SNIPPET):
        os.remove(PROFILE_SNIPPET)
        log(f"Removed {PROFILE_SNIPPET}")


# ----------------------------------------------------------------------
# Core actions
# ----------------------------------------------------------------------
def do_install(system, target_dir):
    require_privileges(system)

    if os.path.isdir(target_dir):
        log(f"{target_dir} already exists — it will be replaced.")
        shutil.rmtree(target_dir)

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "platform-tools.zip")
        download_platform_tools(system, zip_path)
        extracted = extract_zip(zip_path, tmp)
        parent = os.path.dirname(target_dir) or "."
        os.makedirs(parent, exist_ok=True)
        shutil.move(extracted, target_dir)

    success(f"Installed platform-tools to {target_dir}")

    if system == "Windows":
        add_to_system_path_windows(target_dir)
        adb_path = os.path.join(target_dir, "adb.exe")
    else:
        link_linux(target_dir)
        adb_path = os.path.join(target_dir, "adb")

    write_config(system, {"install_dir": target_dir, "version": get_adb_version(adb_path)})
    verify_adb(adb_path)


def do_update(system):
    require_privileges(system)
    state = detect_existing_install(system)
    if not state["installed"]:
        warn("No existing installation found — nothing to update. Choose install instead.")
        return
    if not state["managed"]:
        warn(f"Found adb at {state['adb_path']}, but it wasn't installed by this tool, "
             "so it can't be safely auto-updated.")
        log("Use the install option to place a fresh, tool-managed copy instead.")
        return
    log(f"Updating existing install at {state['dir']}...")
    do_install(system, state["dir"])


def do_uninstall(system):
    require_privileges(system)
    state = detect_existing_install(system)
    if not state["installed"]:
        warn("No installation found — nothing to uninstall.")
        return
    if not state["managed"]:
        warn(f"Found adb at {state['adb_path']}, but it wasn't installed by this tool.")
        log("Please remove it manually (this tool won't touch installs it didn't create).")
        return

    target_dir = state["dir"]
    if system == "Windows":
        remove_from_system_path_windows(target_dir)
    else:
        unlink_linux()

    if os.path.isdir(target_dir):
        shutil.rmtree(target_dir)
        log(f"Removed {target_dir}")

    remove_config(system)
    success("Uninstall complete.")


def verify_adb(adb_path):
    if not os.path.isfile(adb_path):
        warn(f"Expected adb binary not found at {adb_path}")
        return
    try:
        out = subprocess.run([adb_path, "version"], capture_output=True, text=True, timeout=10)
        success("adb is working:")
        print(_c(f"    {out.stdout.strip()}", C.DIM))
    except Exception as e:
        warn(f"Could not run adb to verify install: {e}")


# ----------------------------------------------------------------------
# Raw keyboard input (no external deps: msvcrt on Windows, termios on POSIX)
# ----------------------------------------------------------------------
def _supports_raw_input():
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if platform.system() == "Windows":
        try:
            import msvcrt  # noqa: F401
            return True
        except ImportError:
            return False
    try:
        import termios, tty  # noqa: F401
        return True
    except ImportError:
        return False


def _getch():
    """Read one keypress, returning 'UP', 'DOWN', 'ENTER', 'ESC', a single
    printable character, or None for anything unrecognized."""
    if platform.system() == "Windows":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):  # arrow / function key prefix
            ch2 = msvcrt.getch()
            return {"H": "UP", "P": "DOWN"}.get(ch2.decode(errors="ignore"))
        if ch in (b"\r", b"\n"):
            return "ENTER"
        if ch == b"\x03":
            raise KeyboardInterrupt
        if ch == b"\x1b":
            return "ESC"
        try:
            return ch.decode()
        except Exception:
            return None
    else:
        import termios, tty
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    return {"A": "UP", "B": "DOWN"}.get(ch3, "ESC")
                return "ESC"
            if ch in ("\r", "\n"):
                return "ENTER"
            if ch == "\x03":
                raise KeyboardInterrupt
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# ----------------------------------------------------------------------
# Inline (non-full-screen) arrow picker — used for small confirmations
# ----------------------------------------------------------------------
def prompt_choice(prompt, valid):
    while True:
        choice = input(_c(prompt, C.BOLD)).strip()
        if choice in valid:
            return choice
        warn(f"Please enter one of: {', '.join(valid)}")


def interactive_select(options, prompt="Choose an option"):
    """options: list of (key, label). Returns the chosen key."""
    if not _supports_raw_input():
        print(_c(f"  {prompt}", C.BOLD))
        for i, (_, label) in enumerate(options, start=1):
            print(f"   {_c(f'[{i}]', C.DROID)} {label}")
        valid = [str(i) for i in range(1, len(options) + 1)]
        choice = prompt_choice("  > ", valid)
        return options[int(choice) - 1][0]

    n = len(options)
    idx = 0

    def render(first):
        lines = []
        for i, (_, label) in enumerate(options):
            if i == idx:
                lines.append(f"  {_c(GLYPH_POINTER, C.DROID)} {C.BOLD}{C.DROID}{label}{C.RESET}")
            else:
                lines.append(f"    {_c(label, C.DIM)}")
        block = "\n".join(lines)
        if not first:
            sys.stdout.write(f"\033[{n}A\033[J")
        sys.stdout.write(block + "\n")
        sys.stdout.flush()

    print(_c(f"  {prompt}", C.BOLD))
    sys.stdout.write("\033[?25l")
    try:
        render(first=True)
        while True:
            key = _getch()
            if key in ("UP", "k"):
                idx = (idx - 1) % n
                render(first=False)
            elif key in ("DOWN", "j"):
                idx = (idx + 1) % n
                render(first=False)
            elif key == "ENTER":
                break
            elif key == "ESC":
                idx = n - 1
                break
            elif key and key.isdigit() and 1 <= int(key) <= n:
                idx = int(key) - 1
                render(first=False)
                break
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
    print()
    return options[idx][0]


def confirm_select(prompt="Are you sure?"):
    choice = interactive_select([("no", "No, cancel"), ("yes", "Yes, continue")], prompt=prompt)
    return choice == "yes"


# ----------------------------------------------------------------------
# Status panel (used both standalone and inside the full-screen TUI)
# ----------------------------------------------------------------------
def _field_row(label, value, width=12):
    plain = f"  {label:<{width}} {value}"
    colored = f"  {C.DROID}{label:<{width}}{C.RESET} {value}"
    return plain, colored


def status_lines(system, state):
    lines = []
    if state["installed"]:
        badge_plain = f"{GLYPH_OK} INSTALLED"
        badge_colored = f"{C.DROID}{C.BOLD}{GLYPH_OK} INSTALLED{C.RESET}"
        managed_note = "managed by Quick ADB" if state["managed"] else "found on PATH — not managed by Quick ADB"
        lines.append((f"  {badge_plain}   {managed_note}",
                       f"  {badge_colored}   {_c(managed_note, C.DIM)}"))
        lines.append(_field_row("Location", state["dir"]))
        lines.append(_field_row("Version", state["version"] or "unknown"))
        lines.append(_field_row("Platform", system))
    else:
        badge_plain = f"{GLYPH_EMPTY} NOT INSTALLED"
        badge_colored = f"{C.SOFT_RED}{C.BOLD}{GLYPH_EMPTY} NOT INSTALLED{C.RESET}"
        lines.append((f"  {badge_plain}", f"  {badge_colored}"))
        lines.append(_field_row("Platform", system))
    return [colored if USE_COLOR else plain for plain, colored in lines]


def print_status(system, state):
    hr()
    for line in status_lines(system, state):
        print(line)
    hr()
    print()


# ----------------------------------------------------------------------
# Full-screen TUI
# ----------------------------------------------------------------------
def build_options(state):
    if state["installed"]:
        return [
            ("update", "Update existing installation"),
            ("reinstall_default", "Reinstall to default location"),
            ("reinstall_custom", "Reinstall to a custom location"),
            ("uninstall", "Uninstall"),
            ("exit", "Exit"),
        ]
    return [
        ("install_default", "Install to default location"),
        ("install_custom", "Install to a custom location"),
        ("exit", "Exit"),
    ]


def dispatch_choice(system, choice, state):
    if choice == "update":
        do_update(system)
    elif choice in ("reinstall_default", "install_default"):
        do_install(system, DEFAULT_DIR[system])
    elif choice in ("reinstall_custom", "install_custom"):
        custom = input(_c("  Enter full install path: ", C.BOLD)).strip()
        if custom:
            do_install(system, custom)
    elif choice == "uninstall":
        if confirm_select(prompt=f"Uninstall adb from {state['dir']}?"):
            do_uninstall(system)
        else:
            log("Cancelled.")


def _title_bar():
    """Reverse-video header strip, full terminal width edge-to-edge —
    chrome distinct from content via a solid color block."""
    w = screen_width()
    left = " 🤖 QUICK ADB"
    right = "by HeX "
    fill = max(w - len(left) - len(right), 1)
    if USE_COLOR:
        return f"{C.DROID_BG}{C.BLACK}{C.BOLD}{left}{C.RESET}{C.DROID_BG}{' ' * fill}{right}{C.RESET}"
    return left + " " * fill + right


def _hint_bar(hint):
    """Reverse-video footer strip, full terminal width edge-to-edge —
    keybinding hints (status-bar convention shared by vim/tmux/lazygit)."""
    w = screen_width()
    text = f" {hint}"
    pad = max(w - len(text), 0)
    if USE_COLOR:
        return f"{C.SURFACE}{C.SURFACE_TEXT}{text}{' ' * pad}{C.RESET}"
    return text + " " * pad


def _menu_header(cols, rows):
    """Ascii-art logo when there's enough real estate for it to breathe,
    otherwise the slim reverse-video title bar. Checking both `rows` and
    `cols` on every draw is what makes this react correctly to live
    terminal resizes (SIGWINCH) as well as small vs. large windows."""
    logo = logo_block(cols) if rows >= 22 else None
    if not logo:
        return [_title_bar()]
    art_lines, art_count = logo
    header = [""] * LOGO_TOP_MARGIN
    header.extend(colorize_logo(art_lines, art_count))
    header.append(_c("─" * len(art_lines[0]), C.DROID_DIM))
    return header


def _menu_frame(system, state, options, idx):
    cols, rows = screen_width(), screen_height()
    w = max(cols - 4, 20)  # content divider/text width, small side margin

    header = _menu_header(cols, rows)

    content = []
    content.extend("  " + l if not l.startswith(" ") else l for l in status_lines(system, state))
    content.append(_c("  " + "─" * w, C.DIM))
    content.append("")
    content.append(_c("  Select an action", C.BOLD))
    content.append("")
    for i, (_, label) in enumerate(options):
        if i == idx:
            content.append(f"  {_c(GLYPH_POINTER, C.DROID)} {C.BOLD}{C.DROID}{label}{C.RESET}")
        else:
            content.append(f"    {_c(label, C.DIM)}")

    # Fill the full terminal height: header + content, vertically
    # centered in whatever's left + footer (1), pinned to the very bottom.
    available = max(rows - len(header) - 1, 1)
    extra = max(available - len(content), 0)
    top_pad = extra // 2
    bottom_pad = extra - top_pad

    lines = list(header)
    lines.extend([""] * top_pad)
    lines.extend(content)
    lines.extend([""] * bottom_pad)
    lines.append(_hint_bar("↑/k ↓/j move   ↵ select   1-9 jump   q/⎋ quit"))
    return lines


_active_frame_renderer = None  # set while a full-screen frame is on screen,
                                # so SIGWINCH can trigger an immediate redraw


def _install_resize_handler():
    if not hasattr(signal, "SIGWINCH"):
        return
    def _on_resize(signum, frame):
        if _active_frame_renderer:
            try:
                _active_frame_renderer()
            except Exception:
                pass
    try:
        signal.signal(signal.SIGWINCH, _on_resize)
    except (ValueError, AttributeError):
        pass  # not the main thread, or unsupported — skip gracefully


def _on_suspend(signum, frame):
    """Ctrl+Z: leave the alt screen / show the cursor *before* actually
    suspending, so the shell prompt doesn't reappear inside a hidden
    alt-screen buffer. Re-enter on SIGCONT (fg)."""
    sys.stdout.write("\033[?25h\033[?1049l")
    sys.stdout.flush()
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTSTP)
    # ---- execution resumes here after `fg` sends SIGCONT ----
    signal.signal(signal.SIGTSTP, _on_suspend)
    sys.stdout.write("\033[?1049h\033[?25l")
    if _active_frame_renderer:
        _active_frame_renderer()
    sys.stdout.flush()


def _install_suspend_handler():
    if not hasattr(signal, "SIGTSTP"):
        return
    try:
        signal.signal(signal.SIGTSTP, _on_suspend)
    except (ValueError, AttributeError):
        pass


def _restore_default_signals():
    for sig_name in ("SIGWINCH", "SIGTSTP"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (ValueError, AttributeError):
                pass


def run_menu_screen(system, state, options):
    """Full-screen (alt-buffer) picker. Returns the chosen option key.
    Restores the normal screen buffer before returning so subsequent
    log/success/etc. output scrolls normally."""
    global _active_frame_renderer
    n = len(options)
    idx = 0

    def draw():
        frame = _menu_frame(system, state, options, idx)
        sys.stdout.write("\033[H\033[J")
        sys.stdout.write("\n".join(frame) + "\n")
        sys.stdout.flush()

    sys.stdout.write("\033[?1049h")  # enter alternate screen buffer
    sys.stdout.write("\033[?25l")    # hide cursor
    _active_frame_renderer = draw
    _install_resize_handler()
    _install_suspend_handler()
    try:
        while True:
            draw()
            key = _getch()
            if key in ("UP", "k"):
                idx = (idx - 1) % n
            elif key in ("DOWN", "j"):
                idx = (idx + 1) % n
            elif key == "ENTER":
                break
            elif key in ("ESC",):
                idx = n - 1  # last option is always Exit
                break
            elif key and key.lower() == "q":
                idx = n - 1
                break
            elif key and key.isdigit() and 1 <= int(key) <= n:
                idx = int(key) - 1
                break
    finally:
        _active_frame_renderer = None
        _restore_default_signals()
        sys.stdout.write("\033[?25h")    # show cursor
        sys.stdout.write("\033[?1049l")  # leave alternate screen buffer
        sys.stdout.flush()

    return options[idx][0]


def interactive_menu():
    system = platform.system()
    if system not in DOWNLOAD_URLS:
        error(f"Unsupported platform: {system}")
        sys.exit(1)
    if system == "Darwin":
        warn("macOS isn't automated by this script yet. Download URL:")
        log(DOWNLOAD_URLS["Darwin"])
        sys.exit(1)

    use_tui = _supports_raw_input()

    if not use_tui:
        # Plain fallback: no full-screen redraw, no raw keys.
        print_banner()
        while True:
            state = detect_existing_install(system)
            print_status(system, state)
            options = build_options(state)
            choice = interactive_select(options, prompt="What would you like to do?")
            if choice == "exit":
                log("Bye 👋")
                break
            try:
                dispatch_choice(system, choice, state)
            except PrivilegeError as e:
                error(str(e))
            except Exception as e:
                error(f"Unexpected error: {e}")
            input(_c("\n  Press Enter to continue...", C.DIM))
        return

    while True:
        state = detect_existing_install(system)
        options = build_options(state)
        choice = run_menu_screen(system, state, options)

        if choice == "exit":
            log("Bye 👋")
            break

        try:
            dispatch_choice(system, choice, state)
        except PrivilegeError as e:
            error(str(e))
        except Exception as e:
            error(f"Unexpected error: {e}")
        input(_c("\n  Press Enter to return to the menu...", C.DIM))


# ----------------------------------------------------------------------
def main():
    global USE_COLOR

    parser = argparse.ArgumentParser(description="Quick ADB — install/update/uninstall adb system-wide.")
    parser.add_argument("--install", action="store_true", help="Install adb (non-interactive)")
    parser.add_argument("--update", action="store_true", help="Update existing installation")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall adb")
    parser.add_argument("--status", action="store_true", help="Show current install status and exit")
    parser.add_argument("--dir", default=None, help="Custom install directory (used with --install)")
    parser.add_argument("--no-color", action="store_true", help="Disable colored/ANSI output and the full-screen UI")
    args = parser.parse_args()

    if args.no_color or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        USE_COLOR = False
    else:
        USE_COLOR = _enable_ansi_on_windows()

    system = platform.system()
    if system not in DOWNLOAD_URLS:
        error(f"Unsupported platform: {system}")
        sys.exit(1)
    if system == "Darwin" and (args.install or args.update):
        warn("macOS isn't automated by this script yet. Download URL:")
        log(DOWNLOAD_URLS["Darwin"])
        sys.exit(1)

    if args.status:
        print_banner()
        print_status(system, detect_existing_install(system))
        return

    try:
        if args.install:
            do_install(system, args.dir or DEFAULT_DIR[system])
            return
        if args.update:
            do_update(system)
            return
        if args.uninstall:
            do_uninstall(system)
            return
    except PrivilegeError as e:
        error(str(e))
        sys.exit(1)

    interactive_menu()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Make sure we never leave the terminal in alt-screen/hidden-cursor
        # state if Ctrl+C interrupts mid-render.
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        print()
        warn("Interrupted.")
        sys.exit(130)
