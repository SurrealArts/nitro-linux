// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef NITRO_WRITEBACK_H
#define NITRO_WRITEBACK_H

enum nitro_fan_mode {
	NITRO_FAN_MODE_AUTO = 1,
	NITRO_FAN_MODE_MAX = 2,
	NITRO_FAN_MODE_CUSTOM = 3,
};

static inline int nitro_wb_fan_allowed(
	unsigned int fan_count,
	int gpu)
{
	if (!fan_count)
		return 0;
	if (gpu && fan_count < 2)
		return 0;
	return 1;
}

static inline int nitro_wb_modes_ok(
	unsigned int fan_count,
	unsigned int cpu,
	unsigned int gpu)
{
	if (cpu < NITRO_FAN_MODE_AUTO || cpu > NITRO_FAN_MODE_CUSTOM)
		return 0;
	if (fan_count < 2)
		return 1;
	return gpu >= NITRO_FAN_MODE_AUTO && gpu <= NITRO_FAN_MODE_CUSTOM;
}

static inline int nitro_wb_fans_auto(
	unsigned int fan_count,
	unsigned int cpu,
	unsigned int gpu)
{
	if (cpu != NITRO_FAN_MODE_AUTO)
		return 0;
	if (fan_count < 2)
		return 1;
	return gpu == NITRO_FAN_MODE_AUTO;
}

#endif
