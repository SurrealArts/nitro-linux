obj-m := src/nitro.o

CC ?= cc
KVERSION ?= $(shell uname -r)
KERNELDIR ?= /lib/modules/$(KVERSION)/build
ifdef KERNEL_DIR
KERNELDIR := $(KERNEL_DIR)
endif
PWD := $(shell pwd)

NITRO_VERSION := $(shell cat $(src)/VERSION 2>/dev/null || echo "unknown")
ccflags-y += -DNITRO_VERSION=\"$(NITRO_VERSION)\"

all:
	$(MAKE) -C $(KERNELDIR) M=$(PWD) modules

clean:
	rm -rf src/*.o src/*~ src/.*.cmd src/*.ko src/*.mod src/*.mod.c \
		.module-common.o ..module-common.o.cmd .modules.order.cmd .Module.symvers.cmd \
		.tmp_versions modules.order Module.symvers

install: all
	sudo insmod src/nitro.ko || { sudo modprobe -r nitro; sudo insmod src/nitro.ko; }

unload:
	sudo rmmod nitro || true
	dmesg | tail -n 30

test:
	$(CC) -std=c99 -Wall -Wextra -o /tmp/nitro-writeback-guards \
		tools/test_writeback_guards.c
	/tmp/nitro-writeback-guards

bump:
	@if [ -z "$(VERSION)" ]; then echo "usage: make bump VERSION=x.y.z"; exit 1; fi
	python3 tools/bump_version.py "$(VERSION)"
