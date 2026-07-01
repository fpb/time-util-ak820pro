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

int main(int argc, char **argv) {
    if (hid_init()) { fprintf(stderr, "hid_init failed\n"); return 1; }

    // Find the Raw HID interface by usage page/usage (NOT interface number).
    struct hid_device_info *devs = hid_enumerate(VID, PID), *cur = devs;
    char path[512] = {0};
    int list = (argc > 1 && strcmp(argv[1], "--list") == 0);
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

    // Time: now, or argv[1] = "YYYY-MM-DDTHH:MM:SS".
    struct tm tmv;
    if (argc > 1) {
        memset(&tmv, 0, sizeof tmv);
        if (!strptime(argv[1], "%Y-%m-%dT%H:%M:%S", &tmv)) {
            fprintf(stderr, "bad time, use YYYY-MM-DDTHH:MM:SS\n"); return 1;
        }
    } else {
        time_t t = time(NULL); localtime_r(&t, &tmv);
    }

    // 33 bytes: [0]=report id (0), [1]=cmd 0x01, then payload.
    unsigned char buf[33] = {0};
    buf[1] = 0x01;
    buf[2] = (unsigned char)(tmv.tm_year + 1900 - 2000);
    buf[3] = (unsigned char)(tmv.tm_mon + 1);
    buf[4] = (unsigned char)tmv.tm_mday;
    buf[5] = (unsigned char)(tmv.tm_wday);          // 0=Sun
    buf[6] = (unsigned char)tmv.tm_hour;
    buf[7] = (unsigned char)tmv.tm_min;
    buf[8] = (unsigned char)tmv.tm_sec;

    hid_device *h = hid_open_path(path);
    if (!h) { fprintf(stderr, "open failed: %ls\n", hid_error(NULL)); return 1; }

    if (hid_write(h, buf, sizeof buf) < 0) {
        fprintf(stderr, "write failed: %ls\n", hid_error(h)); return 1;
    }

    unsigned char rep[32] = {0};
    int n = hid_read_timeout(h, rep, sizeof rep, 1000);
    if (n > 1) printf("status: 0x%02X (%s)\n", rep[1], rep[1] == 0 ? "OK" : "error");
    else       printf("no reply\n");

    hid_close(h);
    hid_exit();
    return 0;
}
