DEVKITARM ?= /opt/devkitpro/devkitARM
CC      := $(DEVKITARM)/bin/arm-none-eabi-gcc
OBJCOPY := $(DEVKITARM)/bin/arm-none-eabi-objcopy
GBAFIX  := $(shell which gbafix 2>/dev/null || echo /opt/devkitpro/tools/bin/gbafix)

TARGET := video
CFLAGS := -O2 -mthumb -mthumb-interwork -Wall

all: $(TARGET).gba

$(TARGET).elf: main.c frames1.bin frames2.bin frames_idx.bin palette.bin audio.bin audio_state.bin
	$(CC) $(CFLAGS) -specs=gba.specs main.c -o $@

$(TARGET).gba: $(TARGET).elf
	$(OBJCOPY) -O binary $< $@
	$(GBAFIX) $@ -t"VIDEO"

clean:
	rm -f $(TARGET).elf $(TARGET).gba
