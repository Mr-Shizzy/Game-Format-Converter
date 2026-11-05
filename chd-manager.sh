#!/bin/sh
# CHD Manager for Linux, Steam Deck (desktop mode) and macOS: runs the program from source.
#   ./chd-manager.sh                 opens the window
#   ./chd-manager.sh cli --help      the command line version
# If something it needs is missing, this script says what, why, and offers to install it (it asks first, and shows the
# exact command; your system asks for your password). Nothing is installed without your yes.
here=$(cd "$(dirname "$0")" && pwd)

# Which package manager is this? (CHDM_FORCE_MISSING and CHDM_DRY_RUN are for testing this script.)
pm=""
for t in apt-get dnf pacman brew; do
    if command -v "$t" >/dev/null 2>&1; then pm=$t; break; fi
done
steamos=0
grep -qi steamos "${CHDM_OS_RELEASE:-/etc/os-release}" 2>/dev/null && steamos=1

pkg_for() {
    case "$1:$pm" in
        py:apt-get|py:dnf) echo python3 ;;      py:pacman) echo python ;;      py:brew) echo python ;;
        tk:apt-get) echo python3-tk ;;          tk:dnf) echo python3-tkinter ;; tk:pacman) echo tk ;;  tk:brew) echo python-tk ;;
        chdman:apt-get|chdman:dnf|chdman:pacman) echo mame-tools ;;              chdman:brew) echo rom-tools ;;
    esac
}

cmd_for() {
    pkg=$(pkg_for "$1")
    [ -n "$pkg" ] || return 0
    case "$pm" in
        apt-get) echo "sudo apt install $pkg" ;;
        dnf)     echo "sudo dnf install $pkg" ;;
        pacman)  echo "sudo pacman -S $pkg" ;;
        brew)    echo "brew install $pkg" ;;
    esac
}

missing() {
    case " $CHDM_FORCE_MISSING " in *" $1 "*) return 0 ;; esac
    case "$1" in
        py)     ! command -v python3 >/dev/null 2>&1 ;;
        tk)     ! python3 -c "import tkinter" >/dev/null 2>&1 ;;
        chdman) ! command -v chdman >/dev/null 2>&1 && [ ! -x "$here/chdman" ] ;;
    esac
}

# offer KEY "what it is" "why CHD Manager needs it"
# Returns 0 when it was installed, 1 when not.
offer() {
    echo
    echo "== $2 is missing =="
    echo "$3"
    cmd=$(cmd_for "$1")
    if [ -z "$cmd" ]; then
        echo "I couldn't match your system's package manager to a package for this. Please install it from your distribution's"
        echo "software list or its documentation, then run this again."
        return 1
    fi
    echo
    echo "To install it, this command is run:"
    echo "    $cmd"
    if [ "$steamos" = 1 ]; then
        echo
        echo "This looks like a Steam Deck (SteamOS). Its system files are read-only and a system update removes anything"
        echo "installed into them, so this is not offered automatically. Installing this way is experimental; the README explains."
        return 1
    fi
    if [ -n "$CHDM_DRY_RUN" ]; then
        echo "(dry run: not asking, not running it)"
        return 1
    fi
    if [ ! -t 0 ] && [ -z "$CHDM_ASSUME_TTY" ]; then
        echo "Run that command yourself in a terminal, then start CHD Manager again."
        return 1
    fi
    printf "Run it now? Your system may ask for your password. [y/N] "
    read -r ans
    case "$ans" in
        y|Y|yes|YES) eval "$cmd" ;;
        *) echo "Skipped."; return 1 ;;
    esac
}

if missing py; then
    offer py "Python 3" "CHD Manager is written in Python, so it can't run at all without it." || exit 1
    missing py && exit 1
fi

if missing tk; then
    offer tk "Python's window toolkit (tkinter)" \
        "CHD Manager draws its window with tkinter, so without it nothing can be shown on screen." || exit 1
    missing tk && { echo "tkinter still isn't available, so CHD Manager can't open."; exit 1; }
fi

if missing chdman; then
    offer chdman "chdman" \
        "chdman (part of MAME's tools) is the program that actually makes and reads CHD files; CHD Manager is the friendly front end that asks it to do the work. Without it nothing can be turned into a CHD or back, and most other conversions, which pass through a CHD, won't work either. There is no official chdman download for Linux or macOS, so it has to come from your package manager." \
        || echo "CHD Manager will still open, but it can't make or read CHD files until chdman is installed."
fi

if [ -n "$CHDM_DRY_RUN" ]; then
    exit 0
fi
exec python3 "$here/CHD_Manager.pyw" "$@"
