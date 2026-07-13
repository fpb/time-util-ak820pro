// cc set-clock.c -o set-clock $(pkg-config --cflags --libs hidapi)
// brew install hidapi pkg-config
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <hidapi.h>

#define VID 0x0C45
#define PID 0x8009
#define RAW_USAGE_PAGE 0xFF60
#define RAW_USAGE      0x61

// VIA firmware (the `via` keymap) owns the raw-HID endpoint, so the clock-set
// command rides VIA's custom-value channel instead of a bespoke command:
//   [id_custom_set_value, RTC_CHANNEL, RTC_SET_TIME, yy, mm, dd, wday, hh, mi, ss]
// VIA echoes the buffer back with data[0] == id_custom_set_value when handled, or
// id_unhandled (0xFF) if the channel/value was rejected.
#define ID_CUSTOM_SET_VALUE 0x07
#define ID_UNHANDLED        0xFF
#define RTC_CHANNEL         0x10
#define RTC_SET_TIME        0x01

// The default (non-VIA) keymap keeps the original bespoke command:
//   [0x01, yy, mm, dd, wday, hh, mi, ss]; reply byte [1] is a status code.
#define LEGACY_CMD_SET_TIME 0x01

static void usage(const char *argv0) {
    fprintf(stderr,
        "usage: %s [--legacy] [--list] [YYYY-MM-DDTHH:MM:SS]\n"
        "  (no time)  set the clock to the host's current local time\n"
        "  --legacy   talk to the default (non-VIA) firmware protocol\n"
        "  --list     list HID interfaces and exit\n",
        argv0);
}

int main(int argc, char **argv) {
    int legacy = 0, list = 0;
    const char *timearg = NULL;
    for (int i = 1; i < argc; i++) {
        if      (strcmp(argv[i], "--legacy") == 0) legacy = 1;
        else if (strcmp(argv[i], "--list")   == 0) list = 1;
        else if (strcmp(argv[i], "--help")   == 0) { usage(argv[0]); return 0; }
        else if (argv[i][0] == '-')                { usage(argv[0]); return 1; }
        else                                       timearg = argv[i];
    }

    if (hid_init()) { fprintf(stderr, "hid_init failed\n"); return 1; }

    // Find the Raw HID interface by usage page/usage (NOT interface number).
    struct hid_device_info *devs = hid_enumerate(VID, PID), *cur = devs;
    char path[512] = {0};
    for (; cur; cur = cur->next) {
        if (list)
            printf("usage_page=0x%04hX usage=0x%02hX iface=%d path=%s\n",
                   cur->usage_page, cur->usage, cur->interface_number, cur->path);
        if (cur->usage_page == RAW_USAGE_PAGE && cur->usage == RAW_USAGE)
            strncpy(path, cur->path, sizeof(path) - 1);
    }
    hid_free_enumeration(devs);
    if (list) { hid_exit(); return 0; }
    if (!path[0]) { fprintf(stderr, "Raw HID interface (0xFF60) not found — flashed with raw enabled?\n"); return 1; }

    // Time: now, or argv = "YYYY-MM-DDTHH:MM:SS".
    // strptime/localtime_r are POSIX-only; parse with sscanf and normalize with
    // mktime so this stays portable (glibc, macOS, MinGW) and sets tm_wday.
    struct tm tmv;
    time_t t = time(NULL);
    tmv = *localtime(&t);
    if (timearg) {
        int Y, M, D, h, m, s;
        if (sscanf(timearg, "%d-%d-%dT%d:%d:%d", &Y, &M, &D, &h, &m, &s) != 6) {
            fprintf(stderr, "bad time, use YYYY-MM-DDTHH:MM:SS\n"); return 1;
        }
        tmv.tm_year = Y - 1900; tmv.tm_mon = M - 1; tmv.tm_mday = D;
        tmv.tm_hour = h; tmv.tm_min = m; tmv.tm_sec = s;
        tmv.tm_isdst = -1;
        if (mktime(&tmv) == (time_t)-1) {
            fprintf(stderr, "invalid date/time\n"); return 1;
        }
    }

    // 7-byte time payload shared by both protocols.
    unsigned char yy = (unsigned char)(tmv.tm_year + 1900 - 2000);
    unsigned char mm = (unsigned char)(tmv.tm_mon + 1);
    unsigned char dd = (unsigned char)tmv.tm_mday;
    unsigned char wd = (unsigned char)tmv.tm_wday;   // 0=Sun
    unsigned char hh = (unsigned char)tmv.tm_hour;
    unsigned char mi = (unsigned char)tmv.tm_min;
    unsigned char ss = (unsigned char)tmv.tm_sec;

    // buf[0] is the HID report id (0, stripped on the wire); payload starts at [1].
    unsigned char buf[33] = {0};
    if (legacy) {
        buf[1] = LEGACY_CMD_SET_TIME;
        buf[2] = yy; buf[3] = mm; buf[4] = dd; buf[5] = wd;
        buf[6] = hh; buf[7] = mi; buf[8] = ss;
    } else {
        buf[1] = ID_CUSTOM_SET_VALUE;
        buf[2] = RTC_CHANNEL;
        buf[3] = RTC_SET_TIME;
        buf[4] = yy; buf[5] = mm; buf[6] = dd; buf[7] = wd;
        buf[8] = hh; buf[9] = mi; buf[10] = ss;
    }

    hid_device *h = hid_open_path(path);
    if (!h) { fprintf(stderr, "open failed: %ls\n", hid_error(NULL)); return 1; }

    if (hid_write(h, buf, sizeof buf) < 0) {
        fprintf(stderr, "write failed: %ls\n", hid_error(h)); return 1;
    }

    unsigned char rep[32] = {0};
    int n = hid_read_timeout(h, rep, sizeof rep, 1000);
    int ok = 0;
    if (n > 0) {
        // Legacy: status byte in rep[1] (0x00 = OK). VIA: rep[0] echoes the
        // command id when handled, or id_unhandled (0xFF) on rejection.
        ok = legacy ? (rep[1] == 0x00) : (rep[0] == ID_CUSTOM_SET_VALUE);
        printf("reply: %s\n", ok ? "OK" : "error");
    } else {
        printf("no reply\n");
    }

    hid_close(h);
    hid_exit();
    return (n > 0 && ok) ? 0 : 1;
}
