// ak820ctl -- host utility for the AK820 Pro QMK port.
//
//   ak820ctl clock [YYYY-MM-DDTHH:MM:SS]   set the RTC (default: host time)
//   ak820ctl info                          flash id + writable base
//   ak820ctl flash write <addr> <file>     erase + program + verify
//   ak820ctl flash erase <addr> [n]        erase n 4K sectors
//   ak820ctl flash crc   <addr> <len>      CRC32 a range
//   ak820ctl list                          list HID interfaces
//
// Everything speaks VIA's custom-value layout, so one protocol works against
// both the VIA and non-VIA builds:
//   [ID_CUSTOM_SET_VALUE, channel, cmd, payload...]
// Channel 0x10 is the clock, 0x11 the flash. The reply echoes the buffer;
// data[0] stays ID_CUSTOM_SET_VALUE when handled, and for flash commands
// data[3] carries a status byte.
//
// cc ak820ctl.c -o ak820ctl $(pkg-config --cflags --libs hidapi)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <hidapi.h>

#define VID 0x0C45
#define PID 0x8009            // the QMK build's PID
#define RAW_USAGE_PAGE 0xFF60
#define RAW_USAGE      0x61

#define ID_CUSTOM_SET_VALUE 0x07
#define ID_UNHANDLED        0xFF

#define RTC_CHANNEL   0x10
#define RTC_SET_TIME  0x01

#define FLASH_CHANNEL  0x11
#define FC_INFO        0x01
#define FC_ERASE       0x02
#define FC_WRITE_BEGIN 0x03
#define FC_WRITE_DATA  0x04
#define FC_WRITE_END   0x05
#define FC_CRC32       0x06
#define FC_STATUS      0x07
#define FC_UNLOCK      0x08

#define FS_OK      0x00
#define FS_BUSY    0x01
#define FS_REFUSED 0x02
#define FS_BADARG  0x03

#define SECTOR 4096u
#define PAGE    256u
// 32-byte reports minus the report id, the 3-byte VIA header and our length
// byte. This is what caps throughput at roughly 27 KB/s.
#define CHUNK   27

static hid_device *dev;

static const char *fs_str(unsigned char s) {
    switch (s) {
        case FS_OK:      return "ok";
        case FS_BUSY:    return "busy";
        case FS_REFUSED: return "refused (write floor / locked / animation running)";
        case FS_BADARG:  return "bad argument";
        default:         return "unknown status";
    }
}

// One command/reply round trip. rep[] receives the 32-byte reply payload.
static int xfer(const unsigned char *payload, int len, unsigned char *rep) {
    unsigned char buf[33] = {0};      // buf[0] = report id (stripped on the wire)
    memcpy(&buf[1], payload, len);
    if (hid_write(dev, buf, sizeof buf) < 0) {
        fprintf(stderr, "write failed: %ls\n", hid_error(dev));
        return -1;
    }
    int n = hid_read_timeout(dev, rep, 32, 2000);
    if (n <= 0) { fprintf(stderr, "no reply\n"); return -1; }
    if (rep[0] == ID_UNHANDLED) { fprintf(stderr, "command not handled by firmware\n"); return -1; }
    return 0;
}

// Flash commands share a header; retries absorb FS_BUSY, which the firmware
// returns instead of blocking (a synchronous erase would stall the matrix scan).
static int fcmd(unsigned char cmd, const unsigned char *args, int nargs, unsigned char *rep) {
    unsigned char p[32] = {ID_CUSTOM_SET_VALUE, FLASH_CHANNEL, cmd};
    if (nargs) memcpy(&p[3], args, nargs);
    for (int tries = 0; tries < 2000; tries++) {
        if (xfer(p, 3 + nargs, rep) < 0) return -1;
        if (rep[3] != FS_BUSY) break;
    }
    if (rep[3] != FS_OK) { fprintf(stderr, "flash: %s\n", fs_str(rep[3])); return -1; }
    return 0;
}

static int cmd_clock(const char *timearg) {
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
        if (mktime(&tmv) == (time_t)-1) { fprintf(stderr, "invalid date/time\n"); return 1; }
    }
    unsigned char p[10] = {
        ID_CUSTOM_SET_VALUE, RTC_CHANNEL, RTC_SET_TIME,
        (unsigned char)(tmv.tm_year + 1900 - 2000), (unsigned char)(tmv.tm_mon + 1),
        (unsigned char)tmv.tm_mday, (unsigned char)tmv.tm_wday,
        (unsigned char)tmv.tm_hour, (unsigned char)tmv.tm_min, (unsigned char)tmv.tm_sec,
    };
    unsigned char rep[32];
    if (xfer(p, sizeof p, rep) < 0) return 1;
    printf("clock set to %04d-%02d-%02d %02d:%02d:%02d\n",
           tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
           tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
    return 0;
}

static int cmd_info(void) {
    unsigned char rep[32];
    if (fcmd(FC_INFO, NULL, 0, rep) < 0) return 1;
    unsigned long id   = ((unsigned long)rep[4] << 16) | (rep[5] << 8) | rep[6];
    unsigned long base = ((unsigned long)rep[7] << 16) | (rep[8] << 8) | rep[9];
    printf("flash jedec id : 0x%06lX\n", id);
    printf("writable from  : 0x%06lX (below this needs unlock, stock assets never)\n", base);
    return 0;
}

static int unlock(int on) {
    unsigned char a[1] = {(unsigned char)(on ? 1 : 0)}, rep[32];
    return fcmd(FC_UNLOCK, a, 1, rep);
}

static int erase_sectors(unsigned long addr, unsigned n) {
    unsigned char rep[32];
    for (unsigned i = 0; i < n; i++) {
        unsigned long a = addr + (unsigned long)i * SECTOR;
        unsigned char args[3] = {(unsigned char)(a >> 16), (unsigned char)(a >> 8), (unsigned char)a};
        if (fcmd(FC_ERASE, args, 3, rep) < 0) return -1;
        printf("\rerasing %u/%u", i + 1, n); fflush(stdout);
    }
    printf("\n");
    return 0;
}

static unsigned long crc32_of(const unsigned char *p, size_t n) {
    unsigned long c = 0xFFFFFFFFul;
    for (size_t i = 0; i < n; i++) {
        c ^= p[i];
        for (int b = 0; b < 8; b++) c = (c >> 1) ^ (0xEDB88320ul & (-(long)(c & 1)));
    }
    return ~c & 0xFFFFFFFFul;
}

static int cmd_write(unsigned long addr, const char *file, int do_unlock) {
    if (addr % PAGE) { fprintf(stderr, "address must be 256-byte aligned\n"); return 1; }
    FILE *f = fopen(file, "rb");
    if (!f) { perror(file); return 1; }
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    if (sz <= 0) { fprintf(stderr, "empty file\n"); fclose(f); return 1; }
    unsigned char *buf = malloc((size_t)sz);
    if (!buf || fread(buf, 1, (size_t)sz, f) != (size_t)sz) {
        fprintf(stderr, "read failed\n"); fclose(f); free(buf); return 1;
    }
    fclose(f);

    if (do_unlock && unlock(1) < 0) { free(buf); return 1; }

    unsigned nsec = (unsigned)((sz + SECTOR - 1) / SECTOR);
    printf("writing %ld bytes to 0x%06lX (%u sectors)\n", sz, addr, nsec);
    if (erase_sectors(addr, nsec) < 0) { free(buf); return 1; }

    unsigned char rep[32];
    unsigned char args[3] = {(unsigned char)(addr >> 16), (unsigned char)(addr >> 8), (unsigned char)addr};
    if (fcmd(FC_WRITE_BEGIN, args, 3, rep) < 0) { free(buf); return 1; }

    for (long off = 0; off < sz; ) {
        unsigned char a[1 + CHUNK];
        long n = sz - off; if (n > CHUNK) n = CHUNK;
        a[0] = (unsigned char)n;
        memcpy(&a[1], buf + off, (size_t)n);
        if (fcmd(FC_WRITE_DATA, a, 1 + (int)n, rep) < 0) { free(buf); return 1; }
        off += n;
        if ((off & 0x3FFF) < CHUNK || off == sz) {
            printf("\r%ld/%ld bytes (%.0f%%)", off, sz, 100.0 * off / sz); fflush(stdout);
        }
    }
    printf("\n");
    if (fcmd(FC_WRITE_END, NULL, 0, rep) < 0) { free(buf); return 1; }

    // Verify on the device: CRC the range there and compare, rather than
    // reading megabytes back over 27-byte packets.
    unsigned char c[6] = {(unsigned char)(addr >> 16), (unsigned char)(addr >> 8), (unsigned char)addr,
                          (unsigned char)(sz >> 16), (unsigned char)(sz >> 8), (unsigned char)sz};
    if (fcmd(FC_CRC32, c, 6, rep) < 0) { free(buf); return 1; }
    unsigned long got  = ((unsigned long)rep[4] << 24) | ((unsigned long)rep[5] << 16) |
                         ((unsigned long)rep[6] << 8)  | rep[7];
    unsigned long want = crc32_of(buf, (size_t)sz);
    free(buf);
    if (do_unlock) unlock(0);

    if (got != want) {
        fprintf(stderr, "VERIFY FAILED: device 0x%08lX, file 0x%08lX\n", got, want);
        return 1;
    }
    printf("verified crc32=0x%08lX\n", got);
    return 0;
}

static int cmd_crc(unsigned long addr, unsigned long len) {
    unsigned char rep[32];
    unsigned char c[6] = {(unsigned char)(addr >> 16), (unsigned char)(addr >> 8), (unsigned char)addr,
                          (unsigned char)(len >> 16), (unsigned char)(len >> 8), (unsigned char)len};
    if (fcmd(FC_CRC32, c, 6, rep) < 0) return 1;
    printf("crc32(0x%06lX, %lu) = 0x%02X%02X%02X%02X\n", addr, len, rep[4], rep[5], rep[6], rep[7]);
    return 0;
}

static void usage(const char *a0) {
    fprintf(stderr,
        "usage:\n"
        "  %s clock [YYYY-MM-DDTHH:MM:SS]   set the RTC (default: host time)\n"
        "  %s info                          flash id + writable base\n"
        "  %s flash write <addr> <file> [--unlock]\n"
        "  %s flash erase <addr> [sectors]  4K sectors (default 1)\n"
        "  %s flash crc   <addr> <len>\n"
        "  %s list                          list HID interfaces\n"
        "\n"
        "addresses are hex (0x...) or decimal. --unlock permits writing the stock\n"
        "animation slots; the stock LCD assets are never writable.\n",
        a0, a0, a0, a0, a0, a0);
}

static int open_dev(int list) {
    if (hid_init()) { fprintf(stderr, "hid_init failed\n"); return -1; }
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
    if (list) return 1;   // caller exits
    if (!path[0]) {
        fprintf(stderr, "Raw HID interface (0xFF60) not found -- flashed with raw enabled?\n");
        return -1;
    }
    dev = hid_open_path(path);
    if (!dev) { fprintf(stderr, "open failed: %ls\n", hid_error(NULL)); return -1; }
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(argv[0]); return 1; }
    if (!strcmp(argv[1], "--help") || !strcmp(argv[1], "help")) { usage(argv[0]); return 0; }
    if (!strcmp(argv[1], "list")) { int r = open_dev(1); hid_exit(); return r < 0; }

    if (open_dev(0) < 0) return 1;
    int rc = 1;

    if (!strcmp(argv[1], "clock")) {
        rc = cmd_clock(argc > 2 ? argv[2] : NULL);
    } else if (!strcmp(argv[1], "info")) {
        rc = cmd_info();
    } else if (!strcmp(argv[1], "flash") && argc >= 3) {
        int do_unlock = 0;
        for (int i = 3; i < argc; i++) if (!strcmp(argv[i], "--unlock")) do_unlock = 1;
        if (!strcmp(argv[2], "write") && argc >= 5) {
            rc = cmd_write(strtoul(argv[3], NULL, 0), argv[4], do_unlock);
        } else if (!strcmp(argv[2], "erase") && argc >= 4) {
            unsigned n = argc >= 5 && argv[4][0] != '-' ? (unsigned)strtoul(argv[4], NULL, 0) : 1;
            if (do_unlock && unlock(1) < 0) rc = 1;
            else { rc = erase_sectors(strtoul(argv[3], NULL, 0), n) < 0; if (do_unlock) unlock(0); }
        } else if (!strcmp(argv[2], "crc") && argc >= 5) {
            rc = cmd_crc(strtoul(argv[3], NULL, 0), strtoul(argv[4], NULL, 0));
        } else {
            usage(argv[0]);
        }
    } else {
        usage(argv[0]);
    }

    hid_close(dev);
    hid_exit();
    return rc;
}
