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
    nm_a="$(printf '%s' "$nm" | to_ascii)"
    ar_a="$(printf '%s' "$ar" | to_ascii)"
    sig="$st|$nm_a|$ar_a|$dur_s"

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
            echo "[$(date +%T)] push $pflag elapsed=$pos_s dur=$dur_s ($reason) title=[$nm_a]" >&2
        fi
        "$CTL" media --title "$nm_a" --artist "$ar_a" \
            --elapsed "${pos_s:-0}" --duration "${dur_s:-0}" "$pflag" >/dev/null 2>&1
        last_sig="$sig"; last_pos="$pos_s"; last_push="$now"
    fi

    sleep "$INTERVAL"
done
