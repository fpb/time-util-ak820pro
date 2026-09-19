#!/usr/bin/env bash
# nowplaying-macos.sh -- push macOS now-playing to the AK820 Pro LCD.
#
# Watches Music.app and Spotify via AppleScript (stable, app-specific -- unlike
# MediaRemote, which Apple restricts on recent macOS) and calls ak820ctl on
# change. The firmware self-advances elapsed, so we only push when the track /
# state / duration changes, when playback is seeked (>2 s drift), or every
# KEEPALIVE seconds as a resync/anti-timeout heartbeat.
#
#   INTERVAL=2   poll seconds           AK820CTL=/path/to/ak820ctl
#   KEEPALIVE=15 unconditional re-push seconds
#
# Run in the foreground to watch it, or install as a LaunchAgent (see README).
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
CTL="${AK820CTL:-$HERE/ak820ctl}"
INTERVAL="${INTERVAL:-2}"
KEEPALIVE="${KEEPALIVE:-15}"
# Panel geometry. The firmware is a dumb 3-line renderer: this host owns ALL
# layout, so the character budget per line lives here (change it without a
# reflash). The AK820 Pro's now-playing font fits ~12 chars per 128px line.
CHARS_PER_LINE="${CHARS_PER_LINE:-12}"; export CHARS_PER_LINE

[ -x "$CTL" ] || { echo "ak820ctl not found/executable at: $CTL" >&2; exit 1; }

# Fold accents/UTF-8 to printable ASCII (the LCD font only has glyphs 0x20-0x7E).
# macOS's BSD `iconv -t ASCII//TRANSLIT` is unusable here: it renders "Beyoncé"
# as "Beyonc'e", "Motörhead" as "Mot?rhead", emits '?' and stray marks, and
# corrupts adjacent letters. python3 (shipped with macOS) folds deterministically:
# NFKD-decompose + drop combining marks (e->e, n~->n, o..->o), an explicit table
# for letters that don't decompose (ss, o, ae, oe...) and common punctuation
# (smart quotes/dashes/ellipsis), then keep only printable ASCII. iconv stays as a
# fallback for the unlikely case python3 is missing.
_ASCII_FOLD_PY=$(cat <<'PY'
import sys, unicodedata
MAP = {
    "ß":"ss","ø":"o","Ø":"O","æ":"ae","Æ":"AE",
    "œ":"oe","Œ":"OE","đ":"d","Đ":"D","ł":"l","Ł":"L",
    "þ":"th","Þ":"Th","ð":"d","Ð":"D","ı":"i",
    "“":"\"","”":"\"","„":"\"","‘":"'","’":"'","‚":"'",
    "–":"-","—":"-","―":"-","−":"-","‐":"-","‑":"-",
    "·":".","•":"*","…":"..."," ":" ","​":"","﻿":"",
    "™":"(TM)","©":"(C)","®":"(R)","№":"No",
}
def fold(s):
    out=[]
    for ch in s:
        if ch in MAP: out.append(MAP[ch]); continue
        if ord(ch) < 0x80: out.append(ch); continue
        d=unicodedata.normalize("NFKD", ch)
        d="".join(c for c in d if not unicodedata.combining(c) and ord(c)<0x80)
        out.append(d if d else "?")
    return "".join(c for c in "".join(out) if 0x20 <= ord(c) <= 0x7e)
sys.stdout.write(fold(sys.stdin.read()))
PY
)
if command -v python3 >/dev/null 2>&1; then
    to_ascii() { python3 -c "$_ASCII_FOLD_PY"; }
else
    to_ascii() { iconv -f UTF-8 -t ASCII//TRANSLIT 2>/dev/null | tr -d '\r'; }
fi

# Lay a title + artist out into the three display lines the firmware renders.
# Args: <title> <artist>; prints exactly 3 lines (line0, line1, line2). Owns all
# layout so the firmware stays dumb: ASCII-fold, then wrap the title over lines
# 0-1 keeping spaces when it fits and CamelCase-packing only when it wouldn't,
# and fit the artist on line 2; '>' marks truncation. CHARS_PER_LINE cells/line.
_LAYOUT_PY=$(cat <<'PY'
import sys, os, unicodedata
MAP = {
    "ß":"ss","ø":"o","Ø":"O","æ":"ae","Æ":"AE",
    "œ":"oe","Œ":"OE","đ":"d","Đ":"D","ł":"l","Ł":"L",
    "þ":"th","Þ":"Th","ð":"d","Ð":"D","ı":"i",
    "“":'"',"”":'"',"„":'"',"‘":"'","’":"'","‚":"'",
    "–":"-","—":"-","―":"-","−":"-","‐":"-","‑":"-",
    "·":".","•":"*","…":"..."," ":" ","​":"","﻿":"",
    "™":"(TM)","©":"(C)","®":"(R)","№":"No",
}
def fold(s):
    out=[]
    for ch in s:
        if ch in MAP: out.append(MAP[ch]); continue
        if ord(ch) < 0x80: out.append(ch); continue
        d=unicodedata.normalize("NFKD", ch)
        d="".join(c for c in d if not unicodedata.combining(c) and ord(c)<0x80)
        out.append(d if d else "?")
    return "".join(c for c in "".join(out) if 0x20 <= ord(c) <= 0x7e)

CH  = max(1, int(os.environ.get("CHARS_PER_LINE", "12")))
ELL = ">"
def cap(w): return (w[:1].upper() + w[1:]) if w else w

def greedy2(words, sep):
    # Pack words onto 2 lines of <=CH; sep=True keeps a space between words.
    lines = ["", ""]; li = 0
    for w in words:
        cur = lines[li]
        need = len(cur) + (1 if (sep and cur) else 0) + len(w)
        if need <= CH:
            lines[li] = cur + ((" " if (sep and cur) else "") + w)
        elif li == 0 and len(w) <= CH:
            li = 1; lines[1] = w
        else:
            return None            # overflowed 2 lines
    return lines

def wrap2(s):
    words = s.split()
    r = greedy2(words, True)                       # spaced, readable
    if r is not None: return r[0], r[1]
    r = greedy2([cap(w) for w in words], False)    # CamelCase-packed, word boundaries
    if r is not None: return r[0], r[1]
    packed = "".join(cap(w) for w in words)        # last resort: hard split + marker
    l1, rest = packed[:CH], packed[CH:]
    if len(rest) <= CH: return l1, rest
    return l1, rest[:CH-1] + ELL

def fit1(s):
    if len(s) <= CH: return s                      # as-is
    cw = "".join(cap(w) for w in s.split())        # CamelCase-collapse
    if len(cw) <= CH: return cw
    return cw[:CH-1] + ELL

title  = fold(sys.argv[1] if len(sys.argv) > 1 else "")
artist = fold(sys.argv[2] if len(sys.argv) > 2 else "")
l0, l1 = wrap2(title)
sys.stdout.write(l0 + "\n" + l1 + "\n" + fit1(artist) + "\n")
PY
)
if command -v python3 >/dev/null 2>&1; then
    layout() { python3 -c "$_LAYOUT_PY" "$1" "$2"; }
else
    # Degraded (no python3): fold via iconv, hard-truncate, no wrap/CamelCase.
    layout() {
        printf '%s\n\n%s\n' \
            "$(printf '%s' "$1" | to_ascii | cut -c1-"$CHARS_PER_LINE")" \
            "$(printf '%s' "$2" | to_ascii | cut -c1-"$CHARS_PER_LINE")"
    }
fi

# Query one player without launching it. Echoes "state|title|artist|pos|dur"
# (pos in s, dur in the app's native unit) or "" when the app isn't running.
query() {
    osascript 2>/dev/null <<OSA
if application "$1" is running then
    tell application "$1"
        set pstate to (player state as string)
        if pstate is "playing" or pstate is "paused" then
            try
                set nm to name of current track
            on error
                set nm to ""
            end try
            try
                set ar to artist of current track
            on error
                set ar to ""
            end try
            return pstate & "|" & nm & "|" & ar & "|" & ((player position) as integer) & "|" & ((duration of current track) as integer)
        else
            return "stopped||||"
        end if
    end tell
else
    return ""
end if
OSA
}

last_sig=""      # state|title|artist|dur  (excludes pos: firmware self-advances)
last_pos=0
last_push=0

echo "ak820 now-playing agent: poll ${INTERVAL}s, keepalive ${KEEPALIVE}s, ctl=$CTL" >&2

while true; do
    active=""    # chosen "state|title|artist|pos_s|dur_s"
    # Music duration is seconds; Spotify duration is milliseconds.
    for spec in "Music:1" "Spotify:1000"; do
        app="${spec%%:*}"; div="${spec##*:}"
        line="$(query "$app")"
        [ -z "$line" ] && continue
        IFS='|' read -r st nm ar ps du <<<"$line"
        [ "$st" = "stopped" ] && continue
        [ "$nm" = "missing value" ] && nm=""
        [ "$ar" = "missing value" ] && ar=""
        # digits only. AppleScript now coerces to integer, but be robust to a
        # locale decimal separator ("." or ",") and "missing value" just in case.
        pnum=${ps%%[.,]*}; pnum=${pnum//[!0-9]/}; pnum=${pnum:-0}
        dnum=${du%%[.,]*}; dnum=${dnum//[!0-9]/}; dnum=${dnum:-0}
        cand="$st|$nm|$ar|$pnum|$(( dnum / div ))"
        if [ "$st" = "playing" ]; then active="$cand"; break; fi
        [ -z "$active" ] && active="$cand"      # paused fallback if nothing playing
    done

    now=$(date +%s)

    if [ -z "$active" ]; then
        if [ "$last_sig" != "CLEAR" ]; then
            [ -n "${DEBUG:-}" ] && echo "[$(date +%T)] CLEAR (no player)" >&2
            "$CTL" media clear >/dev/null 2>&1
            last_sig="CLEAR"
        fi
        sleep "$INTERVAL"; continue
    fi

    IFS='|' read -r st nm ar pos_s dur_s <<<"$active"
    # Lay out the three display lines here (host owns layout; firmware just draws).
    # Portable 3-line read (no mapfile: macOS ships bash 3.2).
    l0=""; l1=""; l2=""
    { IFS= read -r l0; IFS= read -r l1; IFS= read -r l2; } < <(layout "$nm" "$ar")
    sig="$st|$l0|$l1|$l2|$dur_s"

    # Progress drift vs. what the firmware would have self-advanced to.
    drift=999
    if [ "$sig" = "$last_sig" ] && [ "$st" = "playing" ]; then
        expected=$(( last_pos + (now - last_push) ))
        drift=$(( pos_s - expected )); drift=${drift#-}
    fi

    if [ "$sig" != "$last_sig" ] || [ "$drift" -gt 2 ] || [ $(( now - last_push )) -ge "$KEEPALIVE" ]; then
        pflag=--playing; [ "$st" = "paused" ] && pflag=--paused
        if [ -n "${DEBUG:-}" ]; then
            reason="sig"; [ "$sig" = "$last_sig" ] && reason="drift=$drift"
            [ "$sig" = "$last_sig" ] && [ "$drift" -le 2 ] && reason="keepalive"
            echo "[$(date +%T)] push $pflag elapsed=$pos_s dur=$dur_s ($reason) lines=[$l0|$l1|$l2]" >&2
        fi
        "$CTL" media --line0 "$l0" --line1 "$l1" --line2 "$l2" \
            --elapsed "${pos_s:-0}" --duration "${dur_s:-0}" "$pflag" >/dev/null 2>&1
        last_sig="$sig"; last_pos="$pos_s"; last_push="$now"
    fi

    sleep "$INTERVAL"
done
