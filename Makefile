# Cross-platform Makefile for set-clock (macOS, Linux, Windows)
#
#   Prerequisites (hidapi + pkg-config):
#     macOS:    brew install hidapi pkg-config
#     Linux:    sudo apt install libhidapi-dev pkg-config   (Debian/Ubuntu)
#               sudo dnf install hidapi-devel pkgconf-pkg-config  (Fedora)
#     Windows:  MSYS2/MinGW-w64:  pacman -S mingw-w64-x86_64-hidapi \
#                                            mingw-w64-x86_64-pkg-config
#
#   Usage:  make        # build
#           make clean

CC      ?= cc
CFLAGS  ?= -O2 -Wall
TARGET   = set-clock
SRC      = set-clock.c

# Detect the platform.
ifeq ($(OS),Windows_NT)
    PLATFORM = Windows
else
    PLATFORM = $(shell uname -s)
endif

# hidapi flags. On macOS/Windows the package is "hidapi". On Linux prefer the
# hidraw backend ("hidapi-hidraw"): unlike the libusb backend it reports HID
# usage_page/usage for devices whose interfaces are claimed by the kernel HID
# driver (e.g. keyboards), which this program needs to find the raw interface.
ifeq ($(PLATFORM),Linux)
    HID_PKG := $(shell pkg-config --exists hidapi-hidraw && echo hidapi-hidraw || echo hidapi)
else
    HID_PKG := hidapi
endif

HID_CFLAGS := $(shell pkg-config --cflags $(HID_PKG))
HID_LIBS   := $(shell pkg-config --libs   $(HID_PKG))

ifeq ($(PLATFORM),Windows)
    TARGET := $(TARGET).exe
endif

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(HID_CFLAGS) -o $@ $< $(HID_LIBS)

clean:
	rm -f $(TARGET)
