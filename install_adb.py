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
import re
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
LOGO_TOP_MARGIN = 1  # blank lines above the logo — bump this to push it down further


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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _vlen(s):
    """Visible length of a string, ignoring ANSI escape codes — needed so
    centering/padding math is based on what actually appears on screen."""
    return len(_ANSI_RE.sub("", s))


def _ctr(s, width):
    """Center a (possibly ANSI-colored) string within `width` columns,
    padding based on *visible* length so escape codes never throw the
    alignment off."""
    pad = max(width - _vlen(s), 0)
    left = pad // 2
    return " " * left + s + " " * (pad - left)


# ----------------------------------------------------------------------
# Rounded "card" panel — the one modern building block the full-screen
# TUI is built from. The menu screen and the action/progress screen are
# both just a card with different content, always centered on screen.
# ----------------------------------------------------------------------
def _card_width(cols):
    return max(min(64, cols - 6), min(24, cols - 2))


def _card_top(w):
    return _c(f"╭{'─' * (w - 2)}╮", C.DROID_DIM)


def _card_bottom(w):
    return _c(f"╰{'─' * (w - 2)}╯", C.DROID_DIM)


def _card_divider(w):
    return _c(f"├{'─' * (w - 2)}┤", C.DROID_DIM)


def _card_row(content, w, center=False):
    """One row of card content, padded/truncated to fit inside the
    border. `content` may already contain ANSI color codes."""
    inner_w = max(w - 4, 1)
    if _vlen(content) > inner_w:
        content = _ANSI_RE.sub("", content)[:max(inner_w - 1, 1)] + "…"
    if center:
        content = _ctr(content, inner_w)
    else:
        content = content + " " * max(inner_w - _vlen(content), 0)
    border = _c("│", C.DROID_DIM)
    return f"{border} {content} {border}"


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


# ----------------------------------------------------------------------
# In-TUI activity log + progress state.
#
# When an action screen (install/update/uninstall) is running inside the
# full-screen alt-buffer, log()/success()/warn()/error() calls made deep
# inside do_install() etc. get redirected here instead of printed
# directly — so the same code path drives both the plain CLI output and
# the live in-TUI card, without do_install() needing to know which mode
# it's running in.
# ----------------------------------------------------------------------
_tui_active = False
_tui_log_lines = []   # list of (text, kind)
_tui_progress = None  # (done, total) or None
_tui_redraw = None    # callable, set while an action screen is live

_KIND_STYLE = {
    "info": (C.DROID, "›"),
    "success": (C.DROID, GLYPH_SUCCESS),
    "warn": (C.SOFT_YELLOW, GLYPH_WARN),
    "error": (C.SOFT_RED, GLYPH_ERROR),
    "detail": (C.DIM, " "),
}


def _tui_push(text, kind="info"):
    _tui_log_lines.append((text, kind))
    del _tui_log_lines[:-6]  # keep the card compact — most recent lines only
    if _tui_redraw:
        _tui_redraw()


def log(msg):
    if _tui_active:
        _tui_push(msg, "info")
        return
    print(f"{_c('›', C.DROID)} {msg}")


def success(msg):
    if _tui_active:
        _tui_push(msg, "success")
        return
    print(f"{_c(GLYPH_SUCCESS, C.DROID)} {msg}")


def warn(msg):
    if _tui_active:
        _tui_push(msg, "warn")
        return
    print(f"{_c(GLYPH_WARN, C.SOFT_YELLOW)} {msg}")


def error(msg):
    if _tui_active:
        _tui_push(msg, "error")
        return
    print(f"{_c(GLYPH_ERROR, C.SOFT_RED)} {msg}")


def detail(msg):
    """Secondary/dim output, e.g. the verbatim adb version string."""
    if _tui_active:
        _tui_push(msg, "detail")
        return
    print(_c(f"    {msg}", C.DIM))


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
def _progress_bar_str(done, total, width=32):
    """Build just the colored bar+percentage text (no cursor control) —
    shared by the plain \\r-updating CLI output and the in-TUI card."""
    if total > 0:
        frac = min(done / total, 1.0)
        filled = int(width * frac)
        bar = "█" * filled + "░" * (width - filled)
        pct = f"{frac * 100:5.1f}%"
        mb_done = done / (1024 * 1024)
        mb_total = total / (1024 * 1024)
        return f"{_c(bar, C.DROID)} {pct}  ({mb_done:.1f}/{mb_total:.1f} MB)"
    mb_done = done / (1024 * 1024)
    return f"downloading… {mb_done:.1f} MB"


def _print_progress_bar(done, total, width=32):
    global _tui_progress
    if _tui_active:
        _tui_progress = (done, total)
        if _tui_redraw:
            _tui_redraw()
        return
    line = "  " + _progress_bar_str(done, total, width=width)
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
    if not _tui_active:
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
        detail(out.stdout.strip())
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


_raw_mode = False            # True while a screen holds the terminal in raw mode
_raw_old_settings = None


def _read_raw_char():
    """Read one keypress from a terminal that is already in raw mode.
    Distinguishes an escape sequence (arrow keys) from a lone ESC with a
    short peek timeout, so pressing ESC alone never blocks on a second
    read."""
    fd = sys.stdin.fileno()
    ch = os.read(fd, 1)
    if ch == b"\x1b":
        import select as _select
        ready, _, _ = _select.select([fd], [], [], 0.05)
        if ready:
            ch2 = os.read(fd, 1)
            if ch2 == b"[":
                ch3 = os.read(fd, 1)
                if ch3:
                    return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(ch3.decode(errors="ignore"), "ESC")
                return "ESC"
            return "ESC"
        return "ESC"
    if ch in (b"\r", b"\n"):
        return "ENTER"
    if ch == b"\x03":
        raise KeyboardInterrupt
    return ch.decode(errors="ignore")


def _raw_enter():
    """Hold the terminal in raw mode for the whole lifetime of a screen,
    so fast typing and pasted input aren't mangled by per-keystroke
    mode toggling. Idempotent; no-op on Windows (msvcrt handles it)."""
    global _raw_mode, _raw_old_settings
    if _raw_mode or platform.system() == "Windows":
        return
    import termios, tty
    fd = sys.stdin.fileno()
    _raw_old_settings = termios.tcgetattr(fd)
    tty.setraw(fd)
    _raw_mode = True


def _raw_leave():
    """Restore the terminal from raw mode (paired with _raw_enter)."""
    global _raw_mode, _raw_old_settings
    if not _raw_mode or platform.system() == "Windows":
        return
    import termios
    fd = sys.stdin.fileno()
    termios.tcsetattr(fd, termios.TCSADRAIN, _raw_old_settings)
    _raw_old_settings = None
    _raw_mode = False


def _getch():
    """Read one keypress, returning 'UP', 'DOWN', 'LEFT', 'RIGHT',
    'ENTER', 'ESC', a single printable character, or None for anything
    unrecognized. Uses an active raw-mode session if one is held by the
    current screen, otherwise manages raw mode for this single read."""
    if platform.system() == "Windows":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):  # arrow / function key prefix
            ch2 = msvcrt.getch()
            return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(ch2.decode(errors="ignore"))
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
    if _raw_mode:
        return _read_raw_char()
    import termios, tty
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return _read_raw_char()
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


def status_card_lines(system, state):
    """Status content for the modern centered card inside the TUI (as
    opposed to status_lines(), which renders the plain left-aligned
    --status panel)."""
    lines = []
    if state["installed"]:
        badge = f"{GLYPH_OK} INSTALLED"
        lines.append(f"{C.DROID}{C.BOLD}{badge}{C.RESET}" if USE_COLOR else badge)
        managed_note = "managed by Quick ADB" if state["managed"] else "found on PATH — not managed by Quick ADB"
        lines.append(_c(managed_note, C.DIM))
        lines.append(f"{_c('Location', C.DROID)}  {state['dir']}")
        lines.append(f"{_c('Version', C.DROID)}   {state['version'] or 'unknown'}")
        lines.append(f"{_c('Platform', C.DROID)}  {system}")
    else:
        badge = f"{GLYPH_EMPTY} NOT INSTALLED"
        lines.append(f"{C.SOFT_RED}{C.BOLD}{badge}{C.RESET}" if USE_COLOR else badge)
        lines.append("")
        lines.append(f"{_c('Platform', C.DROID)}  {system}")
    return lines


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


def dispatch_choice(system, choice, state, tui=False):
    if choice == "update":
        run_action_screen(system, "Updating platform-tools", lambda: do_update(system), persistent=tui)
    elif choice in ("reinstall_default", "install_default"):
        run_action_screen(system, "Installing platform-tools",
                           lambda: do_install(system, DEFAULT_DIR[system]), persistent=tui)
    elif choice in ("reinstall_custom", "install_custom"):
        if tui:
            custom = run_input_screen(
                system,
                "Custom install location",
                f"Full path to install into (default: {DEFAULT_DIR[system]}):",
                initial=DEFAULT_DIR[system],
            )
            if custom is None or not custom.strip():
                return
            run_action_screen(system, "Installing platform-tools",
                              lambda: do_install(system, custom.strip()), persistent=tui)
        else:
            custom = input(_c("  Enter full install path: ", C.BOLD)).strip()
            if custom:
                run_action_screen(system, "Installing platform-tools",
                                  lambda: do_install(system, custom))
    elif choice == "uninstall":
        if tui:
            confirmed = run_confirm_screen(system, f"Uninstall adb from {state['dir']}?")
        else:
            confirmed = confirm_select(prompt=f"Uninstall adb from {state['dir']}?")
        if confirmed:
            run_action_screen(system, "Uninstalling platform-tools",
                              lambda: do_uninstall(system), persistent=tui)
        elif not tui:
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


def _logo_header_lines(art, cols, margin=LOGO_TOP_MARGIN):
    """Build the ascii-art header block for one art tier, centered on
    `cols` columns: the art rows, tagline, byline, and a divider."""
    lines = [""] * margin
    plain = [_center(l, cols) for l in art]
    plain.append(_center(LOGO_TAGLINE, cols))
    plain.append(_center(LOGO_BYLINE, cols))
    lines.extend(colorize_logo(plain, len(art)))
    lines.append(_c("─" * len(art[0]), C.DROID_DIM))
    return lines


def _logo_header_height(art, margin=LOGO_TOP_MARGIN):
    return margin + len(art) + 3  # art + tagline + byline + divider


def _pick_header(cols, rows, content_h):
    """Pick the largest header that fits without clipping: prefer the big
    ascii-art logo, then the compact logo, then the slim title bar. The
    top margin is dropped if that's what keeps the chosen tier on screen.
    `content_h` is the number of card lines that share the screen below
    the header (plus the pinned footer bar)."""
    footer = 1
    space = rows - content_h - footer
    for art in (LOGO_LARGE, LOGO_MEDIUM):
        if cols < len(art[0]) + 4:
            continue
        base_h = _logo_header_height(art, margin=0)
        if space >= base_h + LOGO_TOP_MARGIN:
            return _logo_header_lines(art, cols, margin=LOGO_TOP_MARGIN)
        if space >= base_h:
            return _logo_header_lines(art, cols, margin=0)
    return [_title_bar()]


def _menu_frame(system, state, options, idx):
    cols, rows = screen_width(), screen_height()
    w = _card_width(cols)

    body = [_card_top(w)]
    for line in status_card_lines(system, state):
        body.append(_card_row(line, w, center=True))
    body.append(_card_divider(w))
    body.append(_card_row(_c("SELECT AN ACTION", C.BOLD), w, center=True))
    for i, (_, label) in enumerate(options):
        if i == idx:
            row = f"{_c(GLYPH_POINTER, C.DROID)} {C.BOLD}{C.DROID}{label}{C.RESET}"
        else:
            row = _c(label, C.DIM)
        body.append(_card_row(row, w, center=True))
    body.append(_card_bottom(w))

    content = [_ctr(l, cols) for l in body]
    header = _pick_header(cols, rows, len(content))

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


def _action_frame(system, title, done):
    """The live card shown while install/update/uninstall runs: a title,
    a progress bar (while a download is in flight), and a small scrolling
    activity log — all centered, all inside the same rounded card style
    as the menu screen."""
    cols, rows = screen_width(), screen_height()
    w = _card_width(cols)
    inner_w = max(w - 4, 1)

    body = [_card_top(w)]
    body.append(_card_row(f"{C.BOLD}{title}{C.RESET}" if USE_COLOR else title, w, center=True))
    body.append(_card_row("", w))

    if _tui_progress is not None:
        bar_width = max(min(32, inner_w - 22), 10)
        bar = _progress_bar_str(*_tui_progress, width=bar_width)
        body.append(_card_row(bar, w, center=True))
        body.append(_card_row("", w))

    if not _tui_log_lines:
        body.append(_card_row(_c(GLYPH_BUSY + " working...", C.DIM), w, center=True))
    else:
        for text, kind in _tui_log_lines:
            color, glyph = _KIND_STYLE.get(kind, (C.DROID, "›"))
            row = _c(text, color) if kind == "detail" else f"{_c(glyph, color)} {text}"
            body.append(_card_row(row, w, center=True))

    body.append(_card_row("", w))
    if done:
        body.append(_card_row(f"{_c(GLYPH_SUCCESS, C.DROID)} done", w, center=True))
    body.append(_card_bottom(w))

    content = [_ctr(l, cols) for l in body]
    header = _pick_header(cols, rows, len(content))

    available = max(rows - len(header) - 1, 1)
    extra = max(available - len(content), 0)
    top_pad = extra // 2
    bottom_pad = extra - top_pad

    lines = list(header)
    lines.extend([""] * top_pad)
    lines.extend(content)
    lines.extend([""] * bottom_pad)
    hint = "any key to continue" if done else "please wait..."
    lines.append(_hint_bar(hint))
    return lines


def run_action_screen(system, title, action_fn, persistent=False):
    """Runs `action_fn` (do_install / do_update / do_uninstall) inside its
    own full-screen alt-buffer session with a live-updating card: a
    progress bar while downloading, and a scrolling activity log — so
    install/update/uninstall never dumps back to plain scrolling text.
    Waits for a keypress once finished, then restores the normal screen.
    When `persistent` is True the caller already owns the alt buffer, so
    this screen only draws into it and never enters/leaves it."""
    global _tui_active, _tui_log_lines, _tui_progress, _tui_redraw, _active_frame_renderer

    _tui_log_lines = []
    _tui_progress = None
    _tui_active = True

    def draw(done=False):
        frame = _action_frame(system, title, done)
        sys.stdout.write("\033[H\033[J")
        sys.stdout.write("\n".join(frame) + "\n")
        sys.stdout.flush()

    if not persistent:
        _buffer_enter()
    _tui_redraw = draw
    _active_frame_renderer = draw
    _install_resize_handler()
    _install_suspend_handler()
    _raw_enter()
    try:
        draw(False)
        action_fn()
    except PrivilegeError as e:
        error(str(e))
    except Exception as e:
        error(f"Unexpected error: {e}")
    finally:
        _tui_progress = None
        draw(True)
        try:
            _getch()
        except KeyboardInterrupt:
            pass
        _raw_leave()
        _tui_active = False
        _tui_redraw = None
        _active_frame_renderer = None
        _restore_default_signals()
        if not persistent:
            _buffer_leave()


_active_frame_renderer = None  # set while a full-screen frame is on screen,
                                # so SIGWINCH can trigger an immediate redraw

_tui_in_buffer = False  # True while the alternate screen buffer is active


def _buffer_enter():
    """Enter the alternate screen buffer (idempotent — safe to call when
    it's already active)."""
    global _tui_in_buffer
    if _tui_in_buffer:
        return
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()
    _tui_in_buffer = True


def _buffer_leave():
    """Leave the alternate screen buffer and restore the cursor
    (idempotent)."""
    global _tui_in_buffer
    if not _tui_in_buffer:
        return
    sys.stdout.write("\033[?25h\033[?1049l")
    sys.stdout.flush()
    _tui_in_buffer = False


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


def run_menu_screen(system, state, options, persistent=False):
    """Full-screen (alt-buffer) picker. Returns the chosen option key.
    If `persistent`, assumes the alt buffer is already active (managed by
    the caller) and only draws — it never enters/leaves the buffer."""
    global _active_frame_renderer
    n = len(options)
    idx = 0

    def draw():
        frame = _menu_frame(system, state, options, idx)
        sys.stdout.write("\033[H\033[J")
        sys.stdout.write("\n".join(frame) + "\n")
        sys.stdout.flush()

    if not persistent:
        _buffer_enter()
    _active_frame_renderer = draw
    _install_resize_handler()
    _install_suspend_handler()
    _raw_enter()
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
        _raw_leave()
        _active_frame_renderer = None
        _restore_default_signals()
        if not persistent:
            _buffer_leave()

    return options[idx][0]


# ----------------------------------------------------------------------
# In-TUI input / confirm / notice screens — everything a selection can
# lead to happens *inside* the alt-buffer TUI, so the shell prompt never
# reappears until Exit is chosen.
# ----------------------------------------------------------------------
def _input_field_row(value, cursor, w):
    """The editable field row: the value with a block cursor ('▌') at the
    cursor position, left-aligned and windowed so the cursor always stays
    visible even for very long paths."""
    inner_w = max(w - 4, 1)
    marker = value[:cursor] + "▌" + value[cursor:]
    if len(marker) > inner_w:
        start = max(0, cursor + 1 - inner_w)
        marker = marker[start:start + inner_w]
    border = _c("│", C.DROID_DIM)
    return f"{border} {_c(marker, C.BOLD)}{' ' * max(inner_w - len(marker), 0)} {border}"


def _input_frame(system, title, prompt, value, cursor):
    cols, rows = screen_width(), screen_height()
    w = _card_width(cols)

    body = [_card_top(w)]
    body.append(_card_row(_c(title, C.BOLD), w, center=True))
    body.append(_card_row("", w))
    body.append(_card_row(_c(prompt, C.DIM), w, center=True))
    body.append(_card_row("", w))
    body.append(_input_field_row(value, cursor, w))
    body.append(_card_row("", w))
    body.append(_card_row(_c("↵ confirm   ⎋ cancel", C.DIM), w, center=True))
    body.append(_card_bottom(w))

    content = [_ctr(l, cols) for l in body]
    header = _pick_header(cols, rows, len(content))
    available = max(rows - len(header) - 1, 1)
    extra = max(available - len(content), 0)
    top_pad = extra // 2
    bottom_pad = extra - top_pad

    lines = list(header)
    lines.extend([""] * top_pad)
    lines.extend(content)
    lines.extend([""] * bottom_pad)
    lines.append(_hint_bar("type to edit   ←/→ move   ↵ confirm   ⎋ cancel"))
    return lines


def run_input_screen(system, title, prompt, initial=""):
    """Full-screen card with an editable text field. Returns the entered
    text, or None if the user cancelled with ESC."""
    global _active_frame_renderer
    buf = list(initial)
    cursor = len(buf)

    def draw():
        frame = _input_frame(system, title, prompt, "".join(buf), cursor)
        sys.stdout.write("\033[H\033[J")
        sys.stdout.write("\n".join(frame) + "\n")
        sys.stdout.flush()

    _active_frame_renderer = draw
    _install_resize_handler()
    _install_suspend_handler()
    _raw_enter()
    try:
        while True:
            draw()
            key = _getch()
            if key == "ENTER":
                break
            if key == "ESC":
                return None
            if key in ("\x7f", "\b"):
                if cursor > 0:
                    del buf[cursor - 1]
                    cursor -= 1
            elif key == "LEFT":
                cursor = max(0, cursor - 1)
            elif key == "RIGHT":
                cursor = min(len(buf), cursor + 1)
            elif key == "UP":
                cursor = 0
            elif key == "DOWN":
                cursor = len(buf)
            elif key and key.isprintable() and len(key) == 1:
                buf.insert(cursor, key)
                cursor += 1
    finally:
        _raw_leave()
        _active_frame_renderer = None
        _restore_default_signals()
    return "".join(buf)


def _confirm_frame(system, prompt, idx):
    cols, rows = screen_width(), screen_height()
    w = _card_width(cols)
    options = [("no", "No, cancel"), ("yes", "Yes, continue")]

    body = [_card_top(w)]
    body.append(_card_row(_c(prompt, C.BOLD), w, center=True))
    body.append(_card_row("", w))
    for i, (_, label) in enumerate(options):
        if i == idx:
            row = f"{_c(GLYPH_POINTER, C.DROID)} {C.BOLD}{C.DROID}{label}{C.RESET}"
        else:
            row = _c(label, C.DIM)
        body.append(_card_row(row, w, center=True))
    body.append(_card_bottom(w))

    content = [_ctr(l, cols) for l in body]
    header = _pick_header(cols, rows, len(content))
    available = max(rows - len(header) - 1, 1)
    extra = max(available - len(content), 0)
    top_pad = extra // 2
    bottom_pad = extra - top_pad

    lines = list(header)
    lines.extend([""] * top_pad)
    lines.extend(content)
    lines.extend([""] * bottom_pad)
    lines.append(_hint_bar("↑/k ↓/j move   ↵ select   ⎋ cancel"))
    return lines


def run_confirm_screen(system, prompt):
    """Full-screen yes/no confirmation inside the TUI. Returns True only
    if the user explicitly picks 'Yes, continue'."""
    global _active_frame_renderer
    options = [("no", "No, cancel"), ("yes", "Yes, continue")]
    n = len(options)
    idx = 0

    def draw():
        frame = _confirm_frame(system, prompt, idx)
        sys.stdout.write("\033[H\033[J")
        sys.stdout.write("\n".join(frame) + "\n")
        sys.stdout.flush()

    _active_frame_renderer = draw
    _install_resize_handler()
    _install_suspend_handler()
    _raw_enter()
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
            elif key == "ESC":
                return False
    finally:
        _raw_leave()
        _active_frame_renderer = None
        _restore_default_signals()
    return options[idx][0] == "yes"


def _notice_frame(system, title, lines):
    cols, rows = screen_width(), screen_height()
    w = _card_width(cols)

    body = [_card_top(w)]
    body.append(_card_row(_c(title, C.BOLD), w, center=True))
    body.append(_card_row("", w))
    for line in lines:
        body.append(_card_row(line, w, center=True))
    body.append(_card_row("", w))
    body.append(_card_row(_c("any key to continue", C.DIM), w, center=True))
    body.append(_card_bottom(w))

    content = [_ctr(l, cols) for l in body]
    header = _pick_header(cols, rows, len(content))
    available = max(rows - len(header) - 1, 1)
    extra = max(available - len(content), 0)
    top_pad = extra // 2
    bottom_pad = extra - top_pad

    frame = list(header)
    frame.extend([""] * top_pad)
    frame.extend(content)
    frame.extend([""] * bottom_pad)
    frame.append(_hint_bar("any key to continue"))
    return frame


def run_notice_screen(system, title, lines):
    """Show a message card in the TUI and wait for a keypress — used for
    errors raised outside an action screen (e.g. privilege checks)."""
    global _active_frame_renderer

    def draw():
        frame = _notice_frame(system, title, lines)
        sys.stdout.write("\033[H\033[J")
        sys.stdout.write("\n".join(frame) + "\n")
        sys.stdout.flush()

    _active_frame_renderer = draw
    _install_resize_handler()
    _install_suspend_handler()
    _raw_enter()
    draw()
    try:
        _getch()
    except KeyboardInterrupt:
        pass
    finally:
        _raw_leave()
        _active_frame_renderer = None
        _restore_default_signals()


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

    # Full-screen TUI: one continuous alt-buffer session. Menu, custom
    # path input, uninstall confirmation, and action screens all render
    # inside the TUI — the shell prompt only returns after Exit.
    _buffer_enter()
    try:
        while True:
            state = detect_existing_install(system)
            options = build_options(state)
            choice = run_menu_screen(system, state, options, persistent=True)

            if choice == "exit":
                break

            try:
                dispatch_choice(system, choice, state, tui=True)
            except PrivilegeError as e:
                run_notice_screen(system, "Permission required", [str(e)])
            except Exception as e:
                run_notice_screen(system, "Unexpected error", [str(e)])
    finally:
        _buffer_leave()
    log("Bye 👋")


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
