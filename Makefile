obj-m := src/nitro.o

KVERSION ?= $(shell uname -r)
KERNELDIR ?= /lib/modules/$(KVERSION)/build
ifdef KERNEL_DIR
KERNELDIR := $(KERNEL_DIR)
endif
PWD := $(shell pwd)

all:
	$(MAKE) -C $(KERNELDIR) M=$(PWD) modules

clean:
	rm -rf src/*.o src/*~ src/.*.cmd src/*.ko src/*.mod.c .tmp_versions modules.order Module.symvers

install: all
	sudo insmod src/nitro.ko || sudo modprobe -r nitro; sudo insmod src/nitro.ko

unload:
	sudo rmmod nitro || true
	dmesg | tail -n 30
