// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * nitro - Acer Nitro WMI driver
 *
 * Targets: Acer Nitro family; verified model: AN515-57 (Scala_TLS), BIOS V1.22.
 *
 * Uses WMI GUID 7A4DDFE7-5B5D-40B4-8595-4408E0CC7F56 (object BH -> WMBH).
 * Restores initial fan/RGB/FTBL/CoolBoost on unload. See docs/REVERSE.md.
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/init.h>
#include <linux/types.h>
#include <linux/bits.h>
#include <linux/slab.h>
#include <linux/minmax.h>
#include <linux/dmi.h>
#include <linux/acpi.h>
#include <linux/platform_device.h>
#include <linux/mutex.h>
#include <linux/hwmon.h>
#include <linux/leds.h>
#include <linux/led-class-multicolor.h>
#include <linux/platform_profile.h>
#include <linux/string.h>

#ifndef NITRO_VERSION
#define NITRO_VERSION "unknown"
#endif
MODULE_AUTHOR("SurrealArts");
MODULE_DESCRIPTION("Acer Nitro WMI driver (fan, RGB, hwmon, platform_profile)");
MODULE_LICENSE("GPL");
MODULE_VERSION(NITRO_VERSION);

#define NITRO_WMI_GUID "7A4DDFE7-5B5D-40B4-8595-4408E0CC7F56"
#define NITRO_EC_COOLBOOST 0x10

#define NITRO_BH_SET_LED		0x02
#define NITRO_BH_SYSINFO		0x05
#define NITRO_BH_SET_STATIC		0x06
#define NITRO_BH_GET_STATIC		0x07
#define NITRO_BH_SET_FAN_BEHAVIOR	0x0E
#define NITRO_BH_GET_FAN_BEHAVIOR	0x0F
#define NITRO_BH_SET_FAN		0x10
#define NITRO_BH_GET_FAN		0x11
#define NITRO_BH_SET_FTBL		0x12
#define NITRO_BH_GET_FTBL		0x13
#define NITRO_BH_SET_DYN		0x14
#define NITRO_BH_GET_DYN		0x15
#define NITRO_BH_SET_MISC		0x16
#define NITRO_BH_GET_MISC		0x17

#define NITRO_FAN_CPU			0x01
#define NITRO_FAN_GPU			0x04

#define NITRO_FANBIT_CPU		0x01
#define NITRO_FANBIT_GPU		0x08
#define NITRO_FANBIT_BOTH		0x09

#define NITRO_CAP_FAN			BIT(0)
#define NITRO_CAP_FAN_MODE		BIT(1)
#define NITRO_CAP_HWMON			BIT(2)
#define NITRO_CAP_PLATFORM_PROFILE	BIT(3)
#define NITRO_CAP_RGB			BIT(4)
#define NITRO_CAP_RGB_DYNAMIC		BIT(5)
#define NITRO_CAP_COOLBOOST		BIT(6)
#define NITRO_CAP_LED_CLASS		BIT(7)

#define NITRO_CAP_ALL \
	(NITRO_CAP_FAN | NITRO_CAP_FAN_MODE | NITRO_CAP_HWMON | \
	NITRO_CAP_PLATFORM_PROFILE | NITRO_CAP_RGB | NITRO_CAP_RGB_DYNAMIC | \
	NITRO_CAP_COOLBOOST | NITRO_CAP_LED_CLASS)

#define NITRO_MAX_ZONES			4
#define NITRO_WMI_MAX_PAYLOAD		64
#define NITRO_INPUT_MAX			128

enum nitro_fan_mode {
	NITRO_FAN_MODE_AUTO		= 1,
	NITRO_FAN_MODE_MAX		= 2,
	NITRO_FAN_MODE_CUSTOM		= 3,
};

enum nitro_ftbl {
	NITRO_FTBL_BALANCED		= 0x00,
	NITRO_FTBL_QUIET		= 0x01,
	NITRO_FTBL_PERF			= 0x02,
	NITRO_FTBL_TURBO		= 0x03,
	NITRO_FTBL_ECO			= 0x04,
};

enum nitro_profile_backend {
	NITRO_PROFILE_NONE		= 0,
	NITRO_PROFILE_FTBL		= 1,
};

enum nitro_coolboost_backend {
	NITRO_COOLBOOST_NONE		= 0,
	NITRO_COOLBOOST_EC10		= 1,
};

struct nitro_quirks {
	unsigned long capabilities;
	u8 rgb_zones;
	u8 dynamic_effects;
	u8 profile_backend;
	u8 coolboost_backend;
	u8 fan_count;
	bool has_misc;
	const char *board;
	const char *tested_bios;
};

static const struct nitro_quirks nitro_quirks_an515_57 = {
	.capabilities = NITRO_CAP_FAN | NITRO_CAP_FAN_MODE | NITRO_CAP_HWMON |
			NITRO_CAP_PLATFORM_PROFILE | NITRO_CAP_RGB |
			NITRO_CAP_RGB_DYNAMIC | NITRO_CAP_COOLBOOST |
			NITRO_CAP_LED_CLASS,
	.rgb_zones = 4,
	.dynamic_effects = 5,
	.profile_backend = NITRO_PROFILE_FTBL,
	.coolboost_backend = NITRO_COOLBOOST_EC10,
	.fan_count = 2,
	.has_misc = true,
	.board = "Scala_TLS",
	.tested_bios = "V1.22",
};

static int nitro_validate_quirks(const struct nitro_quirks *q)
{
	if (!q)
		return -EINVAL;

	if (q->capabilities & ~NITRO_CAP_ALL) {
		pr_err(
			"quirks: unknown capability bits 0x%lx\n",
			q->capabilities & ~NITRO_CAP_ALL);
		return -EINVAL;
	}

	if (q->rgb_zones > NITRO_MAX_ZONES) {
		pr_err(
			"quirks: rgb_zones %u > max %u\n",
			q->rgb_zones, NITRO_MAX_ZONES);
		return -EINVAL;
	}

	if (q->dynamic_effects > 5) {
		pr_err(
			"quirks: dynamic_effects %u > 5\n",
			q->dynamic_effects);
		return -EINVAL;
	}

	if (q->fan_count > 2) {
		pr_err(
			"quirks: fan_count %u > 2\n",
			q->fan_count);
		return -EINVAL;
	}

	if ((
		q->capabilities & NITRO_CAP_RGB) && 
		!q->rgb_zones) {
		pr_err("quirks: CAP_RGB set but rgb_zones == 0\n");
		return -EINVAL;
	}
	if (
		!(q->capabilities & NITRO_CAP_RGB) && 
		q->rgb_zones) {
		pr_err("quirks: rgb_zones > 0 but CAP_RGB not set\n");
		return -EINVAL;
	}

	if (
		(q->capabilities & NITRO_CAP_RGB_DYNAMIC) && 
		!q->dynamic_effects) {
		pr_err("quirks: CAP_RGB_DYNAMIC set but dynamic_effects == 0\n");
		return -EINVAL;
	}
	if (
		!(q->capabilities & NITRO_CAP_RGB_DYNAMIC) && 
		q->dynamic_effects) {
		pr_err("quirks: dynamic_effects > 0 but CAP_RGB_DYNAMIC not set\n");
		return -EINVAL;
	}

	if (
		(q->capabilities & NITRO_CAP_LED_CLASS) && 
		!(q->capabilities & NITRO_CAP_RGB)) {
		pr_err("quirks: CAP_LED_CLASS requires CAP_RGB\n");
		return -EINVAL;
	}

	if (
		(q->capabilities & NITRO_CAP_FAN) && 
		!q->fan_count) {
		pr_err("quirks: CAP_FAN set but fan_count == 0\n");
		return -EINVAL;
	}
	if (
		!(q->capabilities & NITRO_CAP_FAN) && 
		q->fan_count) {
		pr_err("quirks: fan_count > 0 but CAP_FAN not set\n");
		return -EINVAL;
	}

	if (
		(q->capabilities & NITRO_CAP_FAN_MODE) && 
		!(q->capabilities & NITRO_CAP_FAN)) {
		pr_err("quirks: CAP_FAN_MODE requires CAP_FAN\n");
		return -EINVAL;
	}

	if (
		(q->capabilities & NITRO_CAP_PLATFORM_PROFILE) &&
		q->profile_backend != NITRO_PROFILE_FTBL) {
		pr_err("quirks: CAP_PLATFORM_PROFILE requires FTBL backend\n");
		return -EINVAL;
	}
	if (
		!(q->capabilities & NITRO_CAP_PLATFORM_PROFILE) &&
		q->profile_backend != NITRO_PROFILE_NONE) {
		pr_err("quirks: profile_backend set but CAP_PLATFORM_PROFILE not set\n");
		return -EINVAL;
	}

	if (
		(q->capabilities & NITRO_CAP_COOLBOOST) &&
		q->coolboost_backend != NITRO_COOLBOOST_EC10) {
		pr_err("quirks: CAP_COOLBOOST requires EC10 backend\n");
		return -EINVAL;
	}
	if (
		!(q->capabilities & NITRO_CAP_COOLBOOST) &&
		q->coolboost_backend != NITRO_COOLBOOST_NONE) {
		pr_err("quirks: coolboost_backend set but CAP_COOLBOOST not set\n");
		return -EINVAL;
	}

	return 0;
}



static struct platform_device *nitro_pdev;

static bool probe_mode;
static bool disable_fan;
static bool disable_hwmon;
static bool disable_profile;
static bool disable_rgb;
static bool disable_coolboost;

module_param_named(probe, probe_mode, bool, 0444);
MODULE_PARM_DESC(probe, "Read-only probe mode for unverified models");
module_param(disable_fan, bool, 0444);
MODULE_PARM_DESC(disable_fan, "Disable fan and fan mode control");
module_param(disable_hwmon, bool, 0444);
MODULE_PARM_DESC(disable_hwmon, "Disable hwmon registration");
module_param(disable_profile, bool, 0444);
MODULE_PARM_DESC(disable_profile, "Disable platform_profile registration");
module_param(disable_rgb, bool, 0444);
MODULE_PARM_DESC(disable_rgb, "Disable RGB and LED registration");
module_param(disable_coolboost, bool, 0444);
MODULE_PARM_DESC(disable_coolboost, "Disable CoolBoost control");

struct nitro_device;

struct nitro_led {
	struct led_classdev_mc mc;
	struct mc_subled sub[3];
	struct nitro_device *nitro;
	u8 zone;
};

struct nitro_device {
	struct platform_device *pdev;
	struct mutex lock;
	const struct nitro_quirks *quirks;
	unsigned long capabilities;
	bool probe_mode;
	struct nitro_led leds[NITRO_MAX_ZONES];
	u8 saved_fan_cpu;
	u8 saved_fan_gpu;
	u8 saved_fan_cpu_mode;
	u8 saved_fan_gpu_mode;
	bool have_fan_cpu;
	bool have_fan_gpu;
	bool have_fan_modes;
	u8 saved_zone[NITRO_MAX_ZONES][3];
	u8 saved_dyn[16];
	u8 saved_ftbl;
	u8 saved_coolboost;
	bool have_rgb;
	bool have_dyn;
	bool have_ftbl;
	bool have_coolboost;
	bool have_saved;
	u8 cur_zone[NITRO_MAX_ZONES][3];
	u8 wmi_raw_out[16];
	size_t wmi_raw_out_len;
	int wmi_raw_status;
};

static const u8 nitro_zone_ids[NITRO_MAX_ZONES] = { 0x01, 0x02, 0x04, 0x08 };
static const char *const nitro_zone_names[NITRO_MAX_ZONES] = {
	"nitro:rgb:zone1", "nitro:rgb:zone2",
	"nitro:rgb:zone3", "nitro:rgb:zone4",
};

static struct nitro_device *nitro_current(void)
{
	return nitro_pdev ? platform_get_drvdata(nitro_pdev) : NULL;
}

static bool nitro_has_cap(struct nitro_device *nitro, unsigned long cap)
{
	return nitro->capabilities & cap;
}

static unsigned int nitro_zone_count(struct nitro_device *nitro)
{
	return min_t(unsigned int, nitro->quirks->rgb_zones, NITRO_MAX_ZONES);
}

static int nitro_zone_index(struct nitro_device *nitro, unsigned int zone)
{
	unsigned int i, nzones = nitro_zone_count(nitro);

	for (i = 0; i < nzones; i++) {
		if (nitro_zone_ids[i] == zone)
			return i;
	}
	return -1;
}

static int nitro_bh_call(
	u32 method,
	const u8 *in,
	size_t in_len,
	u8 *out,
	size_t out_len)
{
	struct acpi_buffer input, output = { ACPI_ALLOCATE_BUFFER, NULL };
	union acpi_object *obj;
	acpi_status status;
	u64 value;
	size_t i;

	if (!in || in_len > NITRO_WMI_MAX_PAYLOAD)
		return -EINVAL;
	if (out_len > NITRO_WMI_MAX_PAYLOAD)
		return -EINVAL;
	if (!out && out_len)
		return -EINVAL;

	input.length = in_len;
	input.pointer = (void *)in;

	status = wmi_evaluate_method(
		NITRO_WMI_GUID, 0,
		method, &input, &output);
	if (ACPI_FAILURE(status)) {
		pr_debug("BH 0x%x evaluate failed: %s\n", method, acpi_format_exception(status));
		return -EIO;
	}

	obj = output.pointer;
	if (!obj)
		return -ENODATA;

	if (obj->type == ACPI_TYPE_INTEGER) {
		value = obj->integer.value;
		for (i = 0; i < out_len; i++)
			out[i] = i < sizeof(value) ? (u8)(value >> (i * 8)) : 0;
		kfree(obj);
		return 0;
	}

	if (obj->type != ACPI_TYPE_BUFFER) {
		pr_debug("BH 0x%x unexpected type %d\n", method, obj->type);
		kfree(obj);
		return -EPROTO;
	}
	if (obj->buffer.length < out_len)
		out_len = obj->buffer.length;
	if (out_len)
		memcpy(out, obj->buffer.pointer, out_len);
	kfree(obj);
	return 0;
}

static bool nitro_read_only_method(u32 method)
{
	switch (method) {
	case NITRO_BH_SYSINFO:
	case NITRO_BH_GET_STATIC:
	case NITRO_BH_GET_FAN_BEHAVIOR:
	case NITRO_BH_GET_FAN:
	case NITRO_BH_GET_FTBL:
	case NITRO_BH_GET_DYN:
	case NITRO_BH_GET_MISC:
		return true;
	default:
		return false;
	}
}

static int nitro_ec_read(u8 reg, u8 *val)
{
	int ret = ec_read(reg, val);

	return ret == -ENODEV ? -ENXIO : ret;
}

static int nitro_ec_write(u8 reg, u8 val)
{
	int ret = ec_write(reg, val);

	return ret == -ENODEV ? -ENXIO : ret;
}

static int nitro_get_fan(u8 which, u8 *duty)
{
	u8 in[4] = { which, 0, 0, 0 };
	u8 out[8] = { 0 };
	int ret = nitro_bh_call(
		NITRO_BH_GET_FAN,
		in, sizeof(in),
		out, sizeof(out));

	if (ret)
		return ret;
	if (out[0] != 0)
		return -EPROTO;
	*duty = out[1];
	return 0;
}

static int nitro_get_ftbl(u8 *ftbl)
{
	u8 in[4] = { 0, 0, 0, 0 };
	u8 out[8] = { 0 };
	int ret = nitro_bh_call(
		NITRO_BH_GET_FTBL,
		in, sizeof(in),
		out, sizeof(out));

	if (ret)
		return ret;
	if (out[0] != 0)
		return -EPROTO;
	*ftbl = out[1];
	return 0;
}

static int nitro_set_ftbl(u8 ftbl)
{
	u8 in[8] = { ftbl, 0, 0, 0, 0, 0, 0, 0 };
	u8 out[4] = { 0 };

	if (ftbl > NITRO_FTBL_ECO)
		return -ERANGE;
	return nitro_bh_call(
		NITRO_BH_SET_FTBL,
		in, sizeof(in),
		out, sizeof(out));
}

static int nitro_get_misc(u8 sel, u8 *val)
{
	u8 in[4] = { sel, 0, 0, 0 };
	u8 out[8] = { 0 };
	int ret;

	if (sel != 0x01 && sel != 0x0B)
		return -EINVAL;
	ret = nitro_bh_call(
		NITRO_BH_GET_MISC,
		in, sizeof(in),
		out, sizeof(out));
	if (ret)
		return ret;
	if (out[0] != 0)
		return -EPROTO;
	*val = out[1];
	return 0;
}

static int nitro_get_static(u8 zone, u8 *r, u8 *g, u8 *b)
{
	u8 in[4] = { zone, 0, 0, 0 };
	u8 out[8] = { 0 };
	int ret = nitro_bh_call(
		NITRO_BH_GET_STATIC,
		in, sizeof(in),
		out, sizeof(out));

	if (ret)
		return ret;
	if (out[0] != 0)
		return -EPROTO;
	*r = out[1];
	*g = out[2];
	*b = out[3];
	return 0;
}

static int nitro_get_sysinfo(u8 a0, u8 a1, u8 *v0, u8 *v1)
{
	u8 in[4] = { a0, a1, 0, 0 };
	u8 out[8] = { 0 };
	int ret = nitro_bh_call(
		NITRO_BH_SYSINFO,
		in, sizeof(in),
		out, sizeof(out));

	if (ret)
		return ret;
	*v0 = out[1];
	*v1 = out[2];
	if (out[0] != 0)
		return -EPROTO;
	return 0;
}

static int nitro_set_fan(u8 which, u8 duty)
{
	u8 in[4] = { which, duty, 0, 0 };
	u8 out[4] = { 0 };

	if (which != NITRO_FAN_CPU && which != NITRO_FAN_GPU)
		return -EINVAL;
	if (duty > 100)
		return -ERANGE;
	if (duty == 0)
		pr_info("fan %s duty 0 -> firmware minimum\n",
			which == NITRO_FAN_CPU ? "cpu" : "gpu");
	else if (duty < 20)
		pr_warn("fan %s duty %u is very low, overheat risk\n",
			which == NITRO_FAN_CPU ? "cpu" : "gpu", duty);
	return nitro_bh_call(
		NITRO_BH_SET_FAN,
		in, sizeof(in),
		out, sizeof(out));
}

static int nitro_set_fan_behavior(u8 bitmap, u8 cpu_mode, u8 gpu_mode)
{
	u8 in[8] = { bitmap, 0, (u8)(cpu_mode | (gpu_mode << 6)), 0, 0, 0, 0, 0 };
	u8 out[4] = { 0 };

	return nitro_bh_call(
		NITRO_BH_SET_FAN_BEHAVIOR,
		in, sizeof(in),
		out, sizeof(out));
}

static int nitro_get_fan_behavior(u8 bitmap, u8 *cpu_mode, u8 *gpu_mode)
{
	u8 in[4] = { bitmap, 0, 0, 0 };
	u8 out[8] = { 0 };
	int ret;

	ret = nitro_bh_call(
		NITRO_BH_GET_FAN_BEHAVIOR,
		in, sizeof(in),
		out, sizeof(out));
	if (ret)
		return ret;
	if (out[0] != 0)
		return -EPROTO;
	*cpu_mode = out[1] & 0x3;
	*gpu_mode = (out[1] >> 6) & 0x3;
	return 0;
}

static int nitro_fans_auto(void)
{
	u8 cpu, gpu;
	int ret;

	ret = nitro_get_fan_behavior(NITRO_FANBIT_BOTH, &cpu, &gpu);
	if (ret)
		return ret;
	return (cpu == NITRO_FAN_MODE_AUTO &&
		gpu == NITRO_FAN_MODE_AUTO) ? 0 : -EPERM;
}

static int nitro_get_coolboost(u8 *value)
{
	u8 v;
	int ret = nitro_ec_read(NITRO_EC_COOLBOOST, &v);

	if (ret)
		return ret;
	*value = v & 1;
	return 0;
}

static int nitro_set_coolboost(u8 value)
{
	u8 check;
	int ret;

	if (value > 1)
		return -ERANGE;
	ret = nitro_ec_write(NITRO_EC_COOLBOOST, value);
	if (ret)
		return ret;
	ret = nitro_ec_read(NITRO_EC_COOLBOOST, &check);
	if (ret)
		return ret;
	if ((check & 1) != value)
		return -EIO;
	return 0;
}

static int nitro_set_static(u8 zone, u8 r, u8 g, u8 b)
{
	u8 in[4] = { zone, r, g, b };
	u8 out[8] = { 0 };

	if (zone != 0x01 && zone != 0x02 && zone != 0x04 && zone != 0x08)
		return -EINVAL;
	return nitro_bh_call(
		NITRO_BH_SET_STATIC,
		in, sizeof(in),
		out, sizeof(out));
}

static int nitro_set_dynamic_raw(const u8 *data16)
{
	u8 out[4] = { 0 };

	if (data16[0] == 0xFF)
		return -EINVAL;
	return nitro_bh_call(
		NITRO_BH_SET_DYN,
		data16, 16,
		out, sizeof(out));
}

static int nitro_get_dynamic_raw(u8 *data16)
{
	u8 in[4] = { 0, 0, 0, 0 };
	u8 out[16] = { 0 };
	int ret = nitro_bh_call(NITRO_BH_GET_DYN, in, sizeof(in), out, sizeof(out));

	if (ret)
		return ret;
	if (out[0] != 0)
		return -EPROTO;
	memcpy(data16, out, sizeof(out));
	return 0;
}

static int nitro_apply_static(
	struct nitro_device *nitro,
	const u8 zones[NITRO_MAX_ZONES][3],
	u8 brightness)
{
	u8 led[8] = { 0x08, 0, 0, 0, 0, 0, 0, 0 };
	u8 out[8] = { 0 };
	u8 bl[16] = { 0 };
	u8 v0, v1;
	unsigned int i, nzones = nitro_zone_count(nitro);
	int ret;

	if (!nzones)
		return -ENODEV;
	ret = nitro_get_sysinfo(0, 0, &v0, &v1);
	if (ret)
		return ret;
	led[5] = (u8)((1u << nzones) - 1);
	ret = nitro_bh_call(
		NITRO_BH_SET_LED,
		led, sizeof(led),
		out, sizeof(out));
	if (ret)
		return ret;
	for (i = 0; i < nzones; i++) {
		ret = nitro_set_static(
			nitro_zone_ids[i],
			zones[i][0], zones[i][1], zones[i][2]);
		if (ret)
			return ret;
	}
	bl[2] = brightness;
	bl[9] = 1;
	return nitro_set_dynamic_raw(bl);
}

static int nitro_apply_brightness(struct nitro_device *nitro, unsigned int bri)
{
	u8 zc[NITRO_MAX_ZONES][3];
	unsigned int i, nzones = nitro_zone_count(nitro);

	for (i = 0; i < nzones; i++) {
		zc[i][0] = (u8)((unsigned int)nitro->cur_zone[i][0] * bri / 100);
		zc[i][1] = (u8)((unsigned int)nitro->cur_zone[i][1] * bri / 100);
		zc[i][2] = (u8)((unsigned int)nitro->cur_zone[i][2] * bri / 100);
	}
	return nitro_apply_static(nitro, zc, (u8)bri);
}

static void nitro_build_effect(
	u8 *dst16,
	unsigned int mode,
	unsigned int speed,
	unsigned int brightness,
	unsigned int dir,
	unsigned int r,
	unsigned int g,
	unsigned int b)
{
	int i;

	for (i = 0; i < 16; i++)
		dst16[i] = 0;
	dst16[0] = (u8)mode;
	dst16[1] = (u8)speed;
	dst16[2] = (u8)brightness;
	dst16[3] = (mode == 3) ? 8 : 0;
	dst16[4] = (u8)dir;
	dst16[5] = (u8)r;
	dst16[6] = (u8)g;
	dst16[7] = (u8)b;
	dst16[8] = 1;
	dst16[9] = 1;
}

static int nitro_get_rpm(bool cpu, unsigned int *rpm)
{
	u8 v0, v1;
	int ret = nitro_get_sysinfo(1, cpu ? 2 : 6, &v0, &v1);

	if (ret)
		return ret;
	*rpm = ((unsigned int)v1 << 8) | v0;
	return 0;
}

static int nitro_get_ctml(unsigned int *temp_c)
{
	u8 v0, v1;
	int ret = nitro_get_sysinfo(1, 1, &v0, &v1);

	if (ret)
		return ret;
	*temp_c = v0;
	return 0;
}

static int nitro_fan_write(struct nitro_device *nitro, u8 which, u8 duty)
{
	u8 prev_cpu = 0, prev_gpu = 0;
	u8 bitmap = nitro->quirks->fan_count >= 2 ?
		NITRO_FANBIT_BOTH : NITRO_FANBIT_CPU;
	bool have_prev = false;
	int ret;

	mutex_lock(&nitro->lock);
	if (!nitro_get_fan_behavior(bitmap, &prev_cpu, &prev_gpu))
		have_prev = prev_cpu >= NITRO_FAN_MODE_AUTO &&
			prev_cpu <= NITRO_FAN_MODE_CUSTOM && (
				nitro->quirks->fan_count < 2 || (
					prev_gpu >= NITRO_FAN_MODE_AUTO &&
					prev_gpu <= NITRO_FAN_MODE_CUSTOM));
	if (which == NITRO_FAN_CPU)
		ret = nitro_set_fan_behavior(
			NITRO_FANBIT_CPU, NITRO_FAN_MODE_CUSTOM, 0);
	else
		ret = nitro_set_fan_behavior(
			NITRO_FANBIT_GPU, 0, NITRO_FAN_MODE_CUSTOM);
	if (!ret)
		ret = nitro_set_fan(which, duty);
	if (ret && have_prev)
		nitro_set_fan_behavior(bitmap, prev_cpu, prev_gpu);
	mutex_unlock(&nitro->lock);
	return ret;
}

static ssize_t fan_cpu_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 duty;
	int ret;

	if (!nitro)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_fan(NITRO_FAN_CPU, &duty);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "%u\n", duty);
}

static ssize_t fan_cpu_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	unsigned long v;
	int ret;

	if (!nitro)
		return -ENODEV;
	if (nitro->probe_mode)
		return -EPERM;
	if (!nitro_has_cap(nitro, NITRO_CAP_FAN))
		return -ENODEV;
	if (count > NITRO_INPUT_MAX)
		return -EINVAL;
	ret = kstrtoul(buf, 10, &v);
	if (ret)
		return ret;
	if (v > 100)
		return -ERANGE;
	ret = nitro_fan_write(nitro, NITRO_FAN_CPU, (u8)v);
	if (ret)
		return ret;
	pr_info("cpu fan set %lu (custom)\n", v);
	return count;
}
static DEVICE_ATTR_RW(fan_cpu);

static ssize_t fan_gpu_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 duty;
	int ret;

	if (!nitro)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_fan(NITRO_FAN_GPU, &duty);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "%u\n", duty);
}

static ssize_t fan_gpu_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	unsigned long v;
	int ret;

	if (!nitro)
		return -ENODEV;
	if (nitro->probe_mode)
		return -EPERM;
	if (!nitro_has_cap(nitro, NITRO_CAP_FAN))
		return -ENODEV;
	if (count > NITRO_INPUT_MAX)
		return -EINVAL;
	ret = kstrtoul(buf, 10, &v);
	if (ret)
		return ret;
	if (v > 100)
		return -ERANGE;
	ret = nitro_fan_write(nitro, NITRO_FAN_GPU, (u8)v);
	if (ret)
		return ret;
	pr_info("gpu fan set %lu (custom)\n", v);
	return count;
}
static DEVICE_ATTR_RW(fan_gpu);

static const char *nitro_fan_mode_name(u8 mode)
{
	switch (mode) {
	case NITRO_FAN_MODE_AUTO:
		return "auto";
	case NITRO_FAN_MODE_MAX:
		return "max";
	case NITRO_FAN_MODE_CUSTOM:
		return "custom";
	default:
		return "unknown";
	}
}

static int nitro_fan_mode_parse(const char *s, u8 *mode)
{
	if (sysfs_streq(s, "auto"))
		*mode = NITRO_FAN_MODE_AUTO;
	else if (sysfs_streq(s, "max"))
		*mode = NITRO_FAN_MODE_MAX;
	else if (sysfs_streq(s, "custom"))
		*mode = NITRO_FAN_MODE_CUSTOM;
	else
		return -EINVAL;
	return 0;
}

static ssize_t fan_mode_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 cpu, gpu;
	int ret;

	if (!nitro)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_fan_behavior(NITRO_FANBIT_BOTH, &cpu, &gpu);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "cpu=%s gpu=%s\n",
		nitro_fan_mode_name(cpu), nitro_fan_mode_name(gpu));
}

static ssize_t fan_mode_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	char work[64], *p, *tok, *key, *val;
	u8 cpu = 0, gpu = 0, c, g;
	u8 bitmap;
	bool have_cpu = false, have_gpu = false;
	int ret;

	if (!nitro)
		return -ENODEV;
	if (nitro->probe_mode)
		return -EPERM;
	if (
		!nitro_has_cap(nitro, NITRO_CAP_FAN) ||
		!nitro_has_cap(nitro, NITRO_CAP_FAN_MODE))
		return -ENODEV;
	if (!count || count >= sizeof(work))
		return -EINVAL;
	memcpy(work, buf, count);
	work[count] = '\0';
	p = work;
	while ((tok = strsep(&p, " \t\n"))) {
		if (!*tok)
			continue;
		key = strsep(&tok, "=");
		val = tok;
		if (!val) {
			if (have_cpu || have_gpu)
				return -EINVAL;
			ret = nitro_fan_mode_parse(key, &cpu);
			if (ret)
				return ret;
			gpu = cpu;
			have_cpu = have_gpu = true;
		} else if (!strcmp(key, "cpu") && !have_cpu) {
			ret = nitro_fan_mode_parse(val, &cpu);
			if (ret)
				return ret;
			have_cpu = true;
		} else if (!strcmp(key, "gpu") && !have_gpu) {
			ret = nitro_fan_mode_parse(val, &gpu);
			if (ret)
				return ret;
			have_gpu = true;
		} else {
			return -EINVAL;
		}
	}
	if (!have_cpu && !have_gpu)
		return -EINVAL;

	bitmap = nitro->quirks->fan_count >= 2 ?
		NITRO_FANBIT_BOTH : NITRO_FANBIT_CPU;
	mutex_lock(&nitro->lock);
	if (!have_cpu || !have_gpu) {
		ret = nitro_get_fan_behavior(bitmap, &c, &g);
		if (ret)
			goto out;
		if (!have_cpu)
			cpu = c;
		if (!have_gpu)
			gpu = g;
	}
	ret = nitro_set_fan_behavior(bitmap, cpu, gpu);
out:
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	pr_info("fan mode cpu=%u gpu=%u\n", cpu, gpu);
	return count;
}
static DEVICE_ATTR_RW(fan_mode);

static ssize_t coolboost_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 value;
	int ret;

	if (!nitro)
		return -ENODEV;
	if (
		!nitro_has_cap(nitro, NITRO_CAP_COOLBOOST) ||
		nitro->quirks->coolboost_backend != NITRO_COOLBOOST_EC10)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_coolboost(&value);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "%u\n", value);
}

static ssize_t coolboost_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	unsigned long v;
	bool on;
	int ret;

	if (!nitro)
		return -ENODEV;
	if (nitro->probe_mode)
		return -EPERM;
	if (
		!nitro_has_cap(nitro, NITRO_CAP_COOLBOOST) ||
		!nitro_has_cap(nitro, NITRO_CAP_FAN_MODE) ||
		nitro->quirks->coolboost_backend != NITRO_COOLBOOST_EC10)
		return -ENODEV;
	if (count > NITRO_INPUT_MAX)
		return -EINVAL;

	if (sysfs_streq(buf, "on")) {
		on = true;
	} else if (sysfs_streq(buf, "off")) {
		on = false;
	} else {
		ret = kstrtoul(buf, 10, &v);
		if (ret)
			return ret;
		if (v > 1)
			return -ERANGE;
		on = v;
	}

	mutex_lock(&nitro->lock);
	if (nitro->quirks->fan_count >= 2) {
		ret = nitro_fans_auto();
	} else {
		u8 cpu, gpu;

		ret = nitro_get_fan_behavior(NITRO_FANBIT_CPU, &cpu, &gpu);
		if (!ret && cpu != NITRO_FAN_MODE_AUTO)
			ret = -EPERM;
	}
	if (!ret)
		ret = nitro_set_coolboost(on ? 1 : 0);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	pr_info("CoolBoost %s\n", on ? "on" : "off");
	return count;
}
static DEVICE_ATTR_RW(coolboost);

static ssize_t ftbl_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 v;
	int ret;

	if (!nitro)
		return -ENODEV;
	if (
		!nitro_has_cap(nitro, NITRO_CAP_PLATFORM_PROFILE) ||
		nitro->quirks->profile_backend != NITRO_PROFILE_FTBL)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_ftbl(&v);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "0x%02x (%u)\n", v, v);
}

static ssize_t ftbl_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	unsigned long v;
	int ret;

	if (!nitro)
		return -ENODEV;
	if (nitro->probe_mode)
		return -EPERM;
	if (
		!nitro_has_cap(nitro, NITRO_CAP_PLATFORM_PROFILE) ||
		nitro->quirks->profile_backend != NITRO_PROFILE_FTBL)
		return -ENODEV;
	if (count > NITRO_INPUT_MAX)
		return -EINVAL;
	ret = kstrtoul(buf, 10, &v);
	if (ret)
		return ret;
	if (v > NITRO_FTBL_ECO)
		return -ERANGE;
	mutex_lock(&nitro->lock);
	ret = nitro_set_ftbl((u8)v);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	pr_info("FTBL set %lu\n", v);
	return count;
}
static DEVICE_ATTR_RW(ftbl);

static ssize_t tkst_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 v;
	int ret;

	if (!nitro)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_misc(0x01, &v);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "0x%02x (%u)\n", v, v);
}

static ssize_t tkst_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 in[4] = { 0x01, 0, 0, 0 };
	u8 out[4] = { 0 };
	unsigned long v;
	int ret;

	if (!nitro)
		return -ENODEV;
	if (nitro->probe_mode)
		return -EPERM;
	if (!nitro->quirks->has_misc)
		return -ENODEV;
	if (count > NITRO_INPUT_MAX)
		return -EINVAL;
	ret = kstrtoul(buf, 10, &v);
	if (ret)
		return ret;
	if (v > 1)
		return -ERANGE;
	in[1] = (u8)v;
	mutex_lock(&nitro->lock);
	ret = nitro_bh_call(
		NITRO_BH_SET_MISC,
		in, sizeof(in),
		out, sizeof(out));
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	pr_info("TKST set %lu (experimental)\n", v);
	return count;
}
static DEVICE_ATTR_RW(tkst);

static ssize_t opms_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 v;
	int ret;

	if (!nitro)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_misc(0x0B, &v);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "0x%02x (%u)\n", v, v);
}
static DEVICE_ATTR_RO(opms);

static ssize_t kbd_zone1_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 r, g, b;
	int ret;

	if (!nitro)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_static(0x01, &r, &g, &b);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "%u %u %u\n", r, g, b);
}
static DEVICE_ATTR_RO(kbd_zone1);

static ssize_t kbd_static_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	unsigned int i, nzones;
	ssize_t n = 0;
	int ret;

	if (!nitro)
		return -ENODEV;
	nzones = nitro_zone_count(nitro);
	mutex_lock(&nitro->lock);
	for (i = 0; i < nzones; i++) {
		u8 r, g, b;

		ret = nitro_get_static(nitro_zone_ids[i], &r, &g, &b);
		if (ret) {
			mutex_unlock(&nitro->lock);
			return ret;
		}
		n += sysfs_emit_at(buf, n, "%s%u:%u %u %u",
			i ? " " : "", nitro_zone_ids[i], r, g, b);
	}
	mutex_unlock(&nitro->lock);
	n += sysfs_emit_at(buf, n, "\n");
	return n;
}

static ssize_t kbd_static_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	unsigned int zone, r, g, b;
	u8 dyn[16];
	u8 brightness = 100;
	int idx, ret;

	if (!nitro)
		return -ENODEV;
	if (nitro->probe_mode)
		return -EPERM;
	if (!nitro_has_cap(nitro, NITRO_CAP_RGB))
		return -ENODEV;
	if (count > NITRO_INPUT_MAX)
		return -EINVAL;
	if (sscanf(buf, "%u %u %u %u", &zone, &r, &g, &b) != 4)
		return -EINVAL;
	if (r > 255 || g > 255 || b > 255)
		return -ERANGE;
	idx = nitro_zone_index(nitro, zone);
	if (idx < 0)
		return -EINVAL;
	mutex_lock(&nitro->lock);
	if (
		nitro_has_cap(nitro, NITRO_CAP_RGB_DYNAMIC) &&
		!nitro_get_dynamic_raw(dyn) &&
		dyn[3] <= 100)
		brightness = dyn[3];
	nitro->cur_zone[idx][0] = (u8)r;
	nitro->cur_zone[idx][1] = (u8)g;
	nitro->cur_zone[idx][2] = (u8)b;
	ret = nitro_apply_brightness(nitro, brightness);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	pr_info("static zone %u set %u %u %u brightness %u\n",
		zone, r, g, b, brightness);
	return count;
}
static DEVICE_ATTR_RW(kbd_static);

static ssize_t wmi_raw_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	ssize_t n = 0;
	size_t i;

	if (!nitro)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	n += sysfs_emit_at(buf, n, "status=%d len=%zu",
		nitro->wmi_raw_status, nitro->wmi_raw_out_len);
	for (i = 0; i < nitro->wmi_raw_out_len && n < PAGE_SIZE - 4; i++)
		n += sysfs_emit_at(buf, n, " %02x", nitro->wmi_raw_out[i]);
	mutex_unlock(&nitro->lock);
	n += sysfs_emit_at(buf, n, "\n");
	return n;
}

static ssize_t wmi_raw_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	int vals[17];
	u8 in[16];
	u8 out[16] = { 0 };
	int n, i, ret;

	if (!nitro)
		return -ENODEV;
	if (count > NITRO_INPUT_MAX)
		return -EINVAL;
	n = sscanf(
		buf,
		"%i %i %i %i %i %i %i %i %i %i %i %i %i %i %i %i %i",
		&vals[0], &vals[1], &vals[2], &vals[3], &vals[4], &vals[5],
		&vals[6], &vals[7], &vals[8], &vals[9], &vals[10], &vals[11],
		&vals[12], &vals[13], &vals[14], &vals[15], &vals[16]);
	if (n < 1 || n > 17)
		return -EINVAL;
	for (i = 0; i < n; i++) {
		if (vals[i] < 0 || vals[i] > 0xFF)
			return -ERANGE;
	}
	if (n - 1 > 16)
		return -EINVAL;
	if (nitro->probe_mode && !nitro_read_only_method(vals[0]))
		return -EPERM;
	for (i = 0; i < n - 1; i++)
		in[i] = (u8)vals[i + 1];
	mutex_lock(&nitro->lock);
	ret = nitro_bh_call(vals[0], in, n - 1, out, sizeof(out));
	nitro->wmi_raw_status = ret;
	if (!ret) {
		memcpy(nitro->wmi_raw_out, out, sizeof(out));
		nitro->wmi_raw_out_len = sizeof(out);
	}
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	pr_info("wmi_raw method 0x%x inlen %d\n", vals[0], n - 1);
	return count;
}
static DEVICE_ATTR_RW(wmi_raw);

static ssize_t kbd_dynamic_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 d[16] = { 0 };
	int ret;

	if (!nitro)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_dynamic_raw(d);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "%u %u %u %u %u %u %u (%02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x)\n",
		d[1], d[2], d[3], d[5], d[6], d[7], d[8],
		d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7],
		d[8], d[9], d[10], d[11], d[12], d[13], d[14], d[15]);
}

static ssize_t kbd_dynamic_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	unsigned int mode, speed, brightness, dir, r, g, b;
	u8 payload[16];
	int ret;

	if (!nitro)
		return -ENODEV;
	if (nitro->probe_mode)
		return -EPERM;
	if (
		!nitro_has_cap(nitro, NITRO_CAP_RGB) ||
		!nitro_has_cap(nitro, NITRO_CAP_RGB_DYNAMIC))
		return -ENODEV;
	if (count > NITRO_INPUT_MAX)
		return -EINVAL;
	if (sscanf(
		buf, "%u %u %u %u %u %u %u",
		&mode, &speed, &brightness,
		&dir, &r, &g, &b) != 7)
		return -EINVAL;
	if (
		mode < 1 ||
		mode > nitro->quirks->dynamic_effects ||
		speed > 255 ||
		brightness > 100 ||
		(dir != 1 && dir != 2) ||
		r > 255 || g > 255 || b > 255)
		return -EINVAL;
	nitro_build_effect(
		payload,
		mode,
		speed,
		brightness,
		dir, r, g, b);
	mutex_lock(&nitro->lock);
	ret = nitro_set_dynamic_raw(payload);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	pr_info("dynamic mode %u speed %u bright %u dir %u rgb %u %u %u\n",
		mode, speed, brightness,
		dir, r, g, b);
	return count;
}
static DEVICE_ATTR_RW(kbd_dynamic);

static ssize_t kbd_bright_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 d[16] = { 0 };
	int ret;

	if (!nitro)
		return -ENODEV;
	if (!nitro_has_cap(nitro, NITRO_CAP_RGB_DYNAMIC))
		return sysfs_emit(buf, "100\n");
	mutex_lock(&nitro->lock);
	ret = nitro_get_dynamic_raw(d);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "%u\n", d[3] <= 100 ? d[3] : 100);
}

static ssize_t kbd_bright_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	unsigned int bri;
	u8 d[16] = { 0 };
	u8 eff[16];
	int ret;

	if (!nitro)
		return -ENODEV;
	if (nitro->probe_mode)
		return -EPERM;
	if (!nitro_has_cap(nitro, NITRO_CAP_RGB))
		return -ENODEV;
	if (count > NITRO_INPUT_MAX)
		return -EINVAL;
	ret = kstrtouint(buf, 10, &bri);
	if (ret)
		return ret;
	if (bri > 100)
		return -ERANGE;
	mutex_lock(&nitro->lock);
	if (nitro_has_cap(nitro, NITRO_CAP_RGB_DYNAMIC)) {
		ret = nitro_get_dynamic_raw(d);
		if (ret) {
			mutex_unlock(&nitro->lock);
			return ret;
		}
		if (d[1] >= 1 && d[1] <= nitro->quirks->dynamic_effects) {
			nitro_build_effect(eff, d[1], d[2], bri, d[5], d[6], d[7], d[8]);
			ret = nitro_set_dynamic_raw(eff);
		} else {
			ret = nitro_apply_brightness(nitro, bri);
		}
	} else {
		ret = nitro_apply_brightness(nitro, bri);
	}
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	pr_info("kbd brightness %u\n", bri);
	return count;
}
static DEVICE_ATTR_RW(kbd_bright);

static ssize_t ec_cpu_temp_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	u8 v0, v1;
	int ret;

	if (!nitro)
		return -ENODEV;
	if (!nitro_has_cap(nitro, NITRO_CAP_HWMON))
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_sysinfo(1, 1, &v0, &v1);
	mutex_unlock(&nitro->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "0x%02x%02x (%u %u)\n", v1, v0, v1, v0);
}
static DEVICE_ATTR_RO(ec_cpu_temp);

static umode_t nitro_hwmon_is_visible(
	const void *drvdata,
	enum hwmon_sensor_types type,
	u32 attr,
	int channel)
{
	const struct nitro_device *nitro = drvdata;

	if (!nitro || !(nitro->capabilities & NITRO_CAP_HWMON))
		return 0;

	switch (type) {
	case hwmon_fan:
		if (attr != hwmon_fan_input)
			break;
		if (channel == 0)
			return 0444;
		if (channel == 1 && nitro->quirks->fan_count >= 2)
			return 0444;
		break;
	case hwmon_temp:
		if (channel == 0 && attr == hwmon_temp_input)
			return 0444;
		break;
	default:
		break;
	}
	return 0;
}

static int nitro_hwmon_read(
	struct device *dev,
	enum hwmon_sensor_types type,
	u32 attr,
	int channel,
	long *val)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	unsigned int v;
	int ret;

	if (!nitro)
		return -ENODEV;
	switch (type) {
	case hwmon_fan:
		if (attr != hwmon_fan_input)
			return -EOPNOTSUPP;
		if (channel != 0 && channel != 1)
			return -EOPNOTSUPP;
		mutex_lock(&nitro->lock);
		ret = nitro_get_rpm(channel == 0, &v);
		mutex_unlock(&nitro->lock);
		if (ret)
			return ret;
		*val = v;
		return 0;
	case hwmon_temp:
		if (attr != hwmon_temp_input || channel != 0)
			return -EOPNOTSUPP;
		mutex_lock(&nitro->lock);
		ret = nitro_get_ctml(&v);
		mutex_unlock(&nitro->lock);
		if (ret)
			return ret;
		*val = (long)v * 1000;
		return 0;
	default:
		return -EOPNOTSUPP;
	}
}

static const struct hwmon_channel_info *const nitro_hwmon_info[] = {
	HWMON_CHANNEL_INFO(fan, HWMON_F_INPUT, HWMON_F_INPUT),
	HWMON_CHANNEL_INFO(temp, HWMON_T_INPUT),
	NULL
};

static const struct hwmon_ops nitro_hwmon_ops = {
	.is_visible = nitro_hwmon_is_visible,
	.read = nitro_hwmon_read,
};

static const struct hwmon_chip_info nitro_hwmon_chip = {
	.ops = &nitro_hwmon_ops,
	.info = nitro_hwmon_info,
};

static int nitro_led_set(struct led_classdev *cdev, enum led_brightness brightness)
{
	struct led_classdev_mc *mc = lcdev_to_mccdev(cdev);
	struct nitro_led *nl = container_of(mc, struct nitro_led, mc);
	struct nitro_device *nitro = nl->nitro;
	u8 dyn[16];
	u8 r, g, b, bri = 100;
	int idx, ret;

	led_mc_calc_color_components(mc, brightness);
	r = (u8)mc->subled_info[0].intensity;
	g = (u8)mc->subled_info[1].intensity;
	b = (u8)mc->subled_info[2].intensity;

	mutex_lock(&nitro->lock);
	if (
		nitro_has_cap(nitro, NITRO_CAP_RGB_DYNAMIC) &&
		!nitro_get_dynamic_raw(dyn) && dyn[3] <= 100)
		bri = dyn[3];
	idx = nitro_zone_index(nitro, nl->zone);
	if (idx >= 0) {
		nitro->cur_zone[idx][0] = r;
		nitro->cur_zone[idx][1] = g;
		nitro->cur_zone[idx][2] = b;
	}
	ret = nitro_apply_static(nitro, nitro->cur_zone, bri);
	mutex_unlock(&nitro->lock);
	return ret;
}

static int nitro_leds_register(struct device *parent, struct nitro_device *nitro)
{
	unsigned int i, j, nzones = nitro_zone_count(nitro);
	int ret;

	if (!nitro_has_cap(nitro, NITRO_CAP_LED_CLASS) || !nzones)
		return 0;

	for (i = 0; i < nzones; i++) {
		struct nitro_led *nl = &nitro->leds[i];

		nl->nitro = nitro;
		nl->zone = nitro_zone_ids[i];
		nl->sub[0].color_index = LED_COLOR_ID_RED;
		nl->sub[0].channel = 0;
		nl->sub[1].color_index = LED_COLOR_ID_GREEN;
		nl->sub[1].channel = 1;
		nl->sub[2].color_index = LED_COLOR_ID_BLUE;
		nl->sub[2].channel = 2;
		if (nitro->have_saved) {
			nl->sub[0].intensity = nitro->saved_zone[i][0];
			nl->sub[1].intensity = nitro->saved_zone[i][1];
			nl->sub[2].intensity = nitro->saved_zone[i][2];
		}
		nl->mc.num_colors = 3;
		nl->mc.subled_info = nl->sub;
		nl->mc.led_cdev.name = nitro_zone_names[i];
		nl->mc.led_cdev.max_brightness = 255;
		nl->mc.led_cdev.brightness_set_blocking = nitro_led_set;
		nl->mc.led_cdev.brightness = (u8)max3(
			(unsigned int)nl->sub[0].intensity,
			(unsigned int)nl->sub[1].intensity,
			(unsigned int)nl->sub[2].intensity
		);

		ret = devm_led_classdev_multicolor_register(parent, &nl->mc);
		if (ret) {
			pr_err("led zone %u register failed %d\n", nl->zone, ret);
			return ret;
		}
		for (j = 0; j < 3; j++)
			nl->sub[j].brightness = nl->mc.led_cdev.brightness;
	}
	return 0;
}

static int nitro_ftbl_to_profile(u8 ftbl, enum platform_profile_option *profile)
{
	switch (ftbl) {
	case NITRO_FTBL_BALANCED:
		*profile = PLATFORM_PROFILE_BALANCED;
		return 0;
	case NITRO_FTBL_QUIET:
		*profile = PLATFORM_PROFILE_QUIET;
		return 0;
	case NITRO_FTBL_PERF:
		*profile = PLATFORM_PROFILE_BALANCED_PERFORMANCE;
		return 0;
	case NITRO_FTBL_TURBO:
		*profile = PLATFORM_PROFILE_PERFORMANCE;
		return 0;
	case NITRO_FTBL_ECO:
		*profile = PLATFORM_PROFILE_LOW_POWER;
		return 0;
	default:
		return -EOPNOTSUPP;
	}
}

static int nitro_profile_to_ftbl(enum platform_profile_option profile, u8 *ftbl)
{
	switch (profile) {
	case PLATFORM_PROFILE_BALANCED:
		*ftbl = NITRO_FTBL_BALANCED;
		return 0;
	case PLATFORM_PROFILE_QUIET:
		*ftbl = NITRO_FTBL_QUIET;
		return 0;
	case PLATFORM_PROFILE_BALANCED_PERFORMANCE:
		*ftbl = NITRO_FTBL_PERF;
		return 0;
	case PLATFORM_PROFILE_PERFORMANCE:
		*ftbl = NITRO_FTBL_TURBO;
		return 0;
	case PLATFORM_PROFILE_LOW_POWER:
		*ftbl = NITRO_FTBL_ECO;
		return 0;
	default:
		return -EOPNOTSUPP;
	}
}

static int nitro_profile_get(struct device *dev, enum platform_profile_option *profile)
{
	struct nitro_device *nitro = nitro_current();
	u8 ftbl;
	int ret;

	if (!nitro)
		return -ENODEV;
	mutex_lock(&nitro->lock);
	ret = nitro_get_ftbl(&ftbl);
	if (!ret)
		ret = nitro_ftbl_to_profile(ftbl, profile);
	mutex_unlock(&nitro->lock);
	return ret;
}

static int nitro_profile_set(struct device *dev, enum platform_profile_option profile)
{
	struct nitro_device *nitro = nitro_current();
	u8 ftbl;
	int ret;

	if (!nitro)
		return -ENODEV;
	ret = nitro_profile_to_ftbl(profile, &ftbl);
	if (ret)
		return ret;
	mutex_lock(&nitro->lock);
	ret = nitro_set_ftbl(ftbl);
	mutex_unlock(&nitro->lock);
	if (!ret)
		pr_info("platform profile %d -> FTBL 0x%02x\n", profile, ftbl);
	return ret;
}

static int nitro_profile_probe(void *drvdata, unsigned long *choices)
{
	struct nitro_device *nitro = drvdata;

	if (!nitro)
		nitro = nitro_current();
	if (!nitro)
		return -ENODEV;
	if (
		!nitro_has_cap(nitro, NITRO_CAP_PLATFORM_PROFILE) ||
		nitro->quirks->profile_backend != NITRO_PROFILE_FTBL)
		return -ENODEV;
	set_bit(PLATFORM_PROFILE_LOW_POWER, choices);
	set_bit(PLATFORM_PROFILE_QUIET, choices);
	set_bit(PLATFORM_PROFILE_BALANCED, choices);
	set_bit(PLATFORM_PROFILE_BALANCED_PERFORMANCE, choices);
	set_bit(PLATFORM_PROFILE_PERFORMANCE, choices);
	return 0;
}

static const struct platform_profile_ops nitro_profile_ops = {
	.probe = nitro_profile_probe,
	.profile_get = nitro_profile_get,
	.profile_set = nitro_profile_set,
};

static ssize_t capabilities_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	struct nitro_device *nitro = dev_get_drvdata(dev);
	const struct nitro_quirks *q;
	ssize_t n = 0;

	if (!nitro || !nitro->quirks)
		return -ENODEV;
	q = nitro->quirks;

	n += sysfs_emit_at(buf, n, "probe=%u", nitro->probe_mode ? 1u : 0u);
	if (nitro->capabilities & NITRO_CAP_FAN)
		n += sysfs_emit_at(buf, n, " fan=1 fan_count=%u", q->fan_count);
	if (nitro->capabilities & NITRO_CAP_FAN_MODE)
		n += sysfs_emit_at(buf, n, " fan_mode=1");
	if (nitro->capabilities & NITRO_CAP_HWMON)
		n += sysfs_emit_at(buf, n, " hwmon=1");
	if (nitro->capabilities & NITRO_CAP_PLATFORM_PROFILE)
		n += sysfs_emit_at(buf, n, " profile=ftbl");
	if (nitro->capabilities & NITRO_CAP_RGB)
		n += sysfs_emit_at(buf, n, " rgb=%u", q->rgb_zones);
	if (nitro->capabilities & NITRO_CAP_RGB_DYNAMIC)
		n += sysfs_emit_at(buf, n, " dyn=%u", q->dynamic_effects);
	if (nitro->capabilities & NITRO_CAP_COOLBOOST)
		n += sysfs_emit_at(buf, n, " coolboost=ec10");
	if (nitro->capabilities & NITRO_CAP_LED_CLASS)
		n += sysfs_emit_at(buf, n, " led=1");
	n += sysfs_emit_at(buf, n, "\n");
	return n;
}
static DEVICE_ATTR_RO(capabilities);

static umode_t nitro_attr_is_visible(
	struct kobject *kobj,
	struct attribute *attr,
	int n)
{
	struct device *dev = kobj_to_dev(kobj);
	struct nitro_device *nitro = dev_get_drvdata(dev);
	const struct nitro_quirks *q;
	umode_t mode = attr->mode;

	if (!nitro || !nitro->quirks)
		return 0;
	q = nitro->quirks;

	if (!strcmp(attr->name, "capabilities"))
		return mode;
	if (!strcmp(attr->name, "wmi_raw"))
		return mode;

	if (!strcmp(attr->name, "fan_cpu")) {
		if (!(nitro->capabilities & NITRO_CAP_FAN))
			return 0;
	} else if (!strcmp(attr->name, "fan_gpu")) {
		if (!(nitro->capabilities & NITRO_CAP_FAN) || q->fan_count < 2)
			return 0;
	} else if (!strcmp(attr->name, "fan_mode")) {
		if (
			(nitro->capabilities & (NITRO_CAP_FAN | NITRO_CAP_FAN_MODE)) !=
			(NITRO_CAP_FAN | NITRO_CAP_FAN_MODE))
			return 0;
	} else if (!strcmp(attr->name, "coolboost")) {
		if (
			!(nitro->capabilities & NITRO_CAP_COOLBOOST) ||
			q->coolboost_backend != NITRO_COOLBOOST_EC10)
			return 0;
	} else if (!strcmp(attr->name, "ftbl")) {
		if (
			!(nitro->capabilities & NITRO_CAP_PLATFORM_PROFILE) ||
			q->profile_backend != NITRO_PROFILE_FTBL)
			return 0;
	} else if (
		!strcmp(attr->name, "tkst") ||
		!strcmp(attr->name, "opms")) {
		if (!q->has_misc)
			return 0;
	} else if (
		!strcmp(attr->name, "kbd_zone1") ||
		!strcmp(attr->name, "kbd_static") ||
		!strcmp(attr->name, "kbd_bright")) {
		if (!(nitro->capabilities & NITRO_CAP_RGB))
			return 0;
	} else if (!strcmp(attr->name, "kbd_dynamic")) {
		if (
			(nitro->capabilities & (NITRO_CAP_RGB | NITRO_CAP_RGB_DYNAMIC)) !=
			(NITRO_CAP_RGB | NITRO_CAP_RGB_DYNAMIC))
			return 0;
	} else if (!strcmp(attr->name, "ec_cpu_temp")) {
		if (!(nitro->capabilities & NITRO_CAP_HWMON))
			return 0;
	} else {
		return 0;
	}

	if (nitro->probe_mode)
		mode &= ~0222;
	return mode;
}

static struct attribute *nitro_attrs[] = {
	&dev_attr_capabilities.attr,
	&dev_attr_fan_cpu.attr,
	&dev_attr_fan_gpu.attr,
	&dev_attr_fan_mode.attr,
	&dev_attr_coolboost.attr,
	&dev_attr_ftbl.attr,
	&dev_attr_tkst.attr,
	&dev_attr_opms.attr,
	&dev_attr_kbd_zone1.attr,
	&dev_attr_kbd_static.attr,
	&dev_attr_kbd_dynamic.attr,
	&dev_attr_kbd_bright.attr,
	&dev_attr_ec_cpu_temp.attr,
	&dev_attr_wmi_raw.attr,
	NULL,
};

static const struct attribute_group nitro_group = {
	.attrs = nitro_attrs,
	.is_visible = nitro_attr_is_visible,
};

static const struct attribute_group *nitro_groups[] = {
	&nitro_group,
	NULL,
};

static const struct dmi_system_id nitro_dmi[] __initconst = {
	{
		.matches = {
			DMI_MATCH(DMI_SYS_VENDOR, "Acer"),
			DMI_MATCH(DMI_PRODUCT_NAME, "Nitro AN515-57"),
			DMI_MATCH(DMI_BOARD_NAME, "Scala_TLS"),
		},
		.driver_data = (void *)&nitro_quirks_an515_57,
	},
	{}
};
MODULE_DEVICE_TABLE(dmi, nitro_dmi);

// RELEVANT: PROBE

static const struct dmi_system_id nitro_probe_dmi[] __initconst = {
	{}
};

static void nitro_save_initial(struct nitro_device *nitro)
{
	unsigned int i, nzones = nitro_zone_count(nitro);
	u8 d;

	if (nitro_has_cap(nitro, NITRO_CAP_FAN)) {
		if (!nitro_get_fan(NITRO_FAN_CPU, &d)) {
			nitro->saved_fan_cpu = d;
			nitro->have_fan_cpu = true;
		}
		if (
			nitro->quirks->fan_count >= 2 &&
			!nitro_get_fan(NITRO_FAN_GPU, &d)) {
			nitro->saved_fan_gpu = d;
			nitro->have_fan_gpu = true;
		}
	}
	if (
		nitro_has_cap(nitro, NITRO_CAP_FAN_MODE) &&
		!nitro_get_fan_behavior(
		nitro->quirks->fan_count >= 2 ?
			NITRO_FANBIT_BOTH : NITRO_FANBIT_CPU,
		&nitro->saved_fan_cpu_mode,
		&nitro->saved_fan_gpu_mode))
		nitro->have_fan_modes = true;
	if (
		nitro_has_cap(nitro, NITRO_CAP_COOLBOOST) &&
		!nitro_get_coolboost(&d)) {
		nitro->saved_coolboost = d;
		nitro->have_coolboost = true;
	}
	if (nitro_has_cap(nitro, NITRO_CAP_RGB)) {
		nitro->have_rgb = true;
		for (i = 0; i < nzones; i++) {
			if (nitro_get_static(
				nitro_zone_ids[i],
				&nitro->saved_zone[i][0],
				&nitro->saved_zone[i][1],
				&nitro->saved_zone[i][2])) {
				nitro->saved_zone[i][0] =
				nitro->saved_zone[i][1] =
				nitro->saved_zone[i][2] = 0;
				nitro->have_rgb = false;
			}
		}
	}
	memcpy(nitro->cur_zone, nitro->saved_zone, sizeof(nitro->cur_zone));
	if (nitro_has_cap(nitro, NITRO_CAP_RGB_DYNAMIC)) {
		if (!nitro_get_dynamic_raw(nitro->saved_dyn))
			nitro->have_dyn = true;
		else
			memset(nitro->saved_dyn, 0, sizeof(nitro->saved_dyn));
	}
	if (
		nitro_has_cap(nitro, NITRO_CAP_PLATFORM_PROFILE) &&
		nitro->quirks->profile_backend == NITRO_PROFILE_FTBL &&
		!nitro_get_ftbl(&d)) {
		nitro->saved_ftbl = d;
		nitro->have_ftbl = true;
	}
	nitro->have_saved = true;
}

static void nitro_log_state(struct nitro_device *nitro)
{
	u8 duty, ftbl, r, g, b, v0, v1, tkst, opms;
	u8 dyn[16] = { 0 };
	unsigned int nzones = nitro_zone_count(nitro);

	if (
		nitro_has_cap(nitro, NITRO_CAP_FAN) &&
		!nitro_get_fan(NITRO_FAN_CPU, &duty))
		pr_info("EC CPU fan duty=%u\n", duty);
	if (
		nitro_has_cap(nitro, NITRO_CAP_FAN) &&
		nitro->quirks->fan_count >= 2 &&
		!nitro_get_fan(NITRO_FAN_GPU, &duty))
		pr_info("EC GPU fan duty=%u\n", duty);
	if (
		nitro_has_cap(nitro, NITRO_CAP_PLATFORM_PROFILE) &&
		nitro->quirks->profile_backend == NITRO_PROFILE_FTBL &&
		!nitro_get_ftbl(&ftbl))
		pr_info("FTBL=0x%02x\n", ftbl);
	if (
		nitro_has_cap(nitro, NITRO_CAP_RGB) && nzones &&
		!nitro_get_static(nitro_zone_ids[0], &r, &g, &b))
		pr_info("zone1 RGB=%u %u %u\n", r, g, b);
	if (
		nitro_has_cap(nitro, NITRO_CAP_HWMON) &&
		!nitro_get_sysinfo(1, 1, &v0, &v1))
		pr_info("CTML raw=0x%02x%02x\n", v1, v0);
	if (
		nitro_has_cap(nitro, NITRO_CAP_RGB_DYNAMIC) &&
		!nitro_get_dynamic_raw(dyn))
		pr_info("dyn raw %02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x\n",
			dyn[0], dyn[1], dyn[2], dyn[3], dyn[4], dyn[5], dyn[6], dyn[7],
			dyn[8], dyn[9], dyn[10], dyn[11], dyn[12], dyn[13], dyn[14], dyn[15]);
	if (nitro->quirks->has_misc && !nitro_get_misc(0x01, &tkst))
		pr_info("TKST=0x%02x\n", tkst);
	if (nitro->quirks->has_misc && !nitro_get_misc(0x0B, &opms))
		pr_info("OPMS=0x%02x\n", opms);
}

static int __init nitro_init(void)
{
	const struct dmi_system_id *match;
	struct nitro_device *nitro;
	struct device *hwmon;
	struct device *pp;
	int ret;

	pr_info("v%s loading (probe=%d)\n", NITRO_VERSION, probe_mode ? 1 : 0);

	if (probe_mode) {
		match = dmi_first_match(nitro_probe_dmi);
		if (!match)
			match = dmi_first_match(nitro_dmi);
	} else {
		match = dmi_first_match(nitro_dmi);
	}
	if (!match) {
		pr_err("DMI match failed for %s\n",
			probe_mode ? "probe model" : "supported model");
		return -ENODEV;
	}

	if (!match->driver_data) {
		pr_err("DMI match has no quirks data\n");
		return -EINVAL;
	}

	ret = nitro_validate_quirks(match->driver_data);
	if (ret) {
		pr_err("Quirks validation failed: %d\n", ret);
		return ret;
	}

	if (!wmi_has_guid(NITRO_WMI_GUID)) {
		pr_err("WMI GUID %s not present\n", NITRO_WMI_GUID);
		return -ENODEV;
	}

	nitro = kzalloc(sizeof(*nitro), GFP_KERNEL);
	if (!nitro)
		return -ENOMEM;
	mutex_init(&nitro->lock);
	nitro->saved_fan_cpu = 50;
	nitro->saved_fan_gpu = 50;
	nitro->quirks = match->driver_data;
	nitro->capabilities = nitro->quirks->capabilities;
	nitro->probe_mode = probe_mode;

	if (disable_fan)
		nitro->capabilities &= ~(NITRO_CAP_FAN | NITRO_CAP_FAN_MODE);
	if (disable_hwmon)
		nitro->capabilities &= ~NITRO_CAP_HWMON;
	if (disable_profile)
		nitro->capabilities &= ~NITRO_CAP_PLATFORM_PROFILE;
	if (disable_rgb)
		nitro->capabilities &= ~(NITRO_CAP_RGB |
			NITRO_CAP_RGB_DYNAMIC | NITRO_CAP_LED_CLASS);
	if (disable_coolboost)
		nitro->capabilities &= ~NITRO_CAP_COOLBOOST;

	nitro_pdev = platform_device_register_simple("nitro", -1, NULL, 0);
	if (IS_ERR(nitro_pdev)) {
		ret = PTR_ERR(nitro_pdev);
		nitro_pdev = NULL;
		kfree(nitro);
		return ret;
	}
	nitro->pdev = nitro_pdev;
	platform_set_drvdata(nitro_pdev, nitro);

	mutex_lock(&nitro->lock);
	if (!nitro->probe_mode)
		nitro_save_initial(nitro);
	nitro_log_state(nitro);
	mutex_unlock(&nitro->lock);

	if (!nitro->probe_mode) {
		if (nitro_has_cap(nitro, NITRO_CAP_HWMON)) {
			hwmon = devm_hwmon_device_register_with_info(
				&nitro_pdev->dev,
				"nitro", nitro,
				&nitro_hwmon_chip, NULL);
			if (IS_ERR(hwmon)) {
				ret = PTR_ERR(hwmon);
				pr_warn("hwmon register failed %d; continuing without hwmon\n", ret);
				nitro->capabilities &= ~NITRO_CAP_HWMON;
			}
		}

		ret = nitro_leds_register(&nitro_pdev->dev, nitro);
		if (ret) {
			pr_warn("led register failed %d; continuing without led class\n", ret);
			nitro->capabilities &= ~NITRO_CAP_LED_CLASS;
		}

		if (
			nitro_has_cap(nitro, NITRO_CAP_PLATFORM_PROFILE) &&
			nitro->quirks->profile_backend == NITRO_PROFILE_FTBL) {
			pp = devm_platform_profile_register(
				&nitro_pdev->dev,
				"nitro", nitro,
				&nitro_profile_ops);
			if (IS_ERR(pp)) {
				ret = PTR_ERR(pp);
				pr_warn("platform_profile register failed %d; continuing without it\n", ret);
				nitro->capabilities &= ~NITRO_CAP_PLATFORM_PROFILE;
			}
		}
	}

	ret = sysfs_create_groups(&nitro_pdev->dev.kobj, nitro_groups);
	if (ret)
		goto err_pdev;

	if (nitro->probe_mode) {
		pr_info("probe mode: writes disabled, wmi_raw limited to read-only methods\n");
		return 0;
	}

	pr_info("capabilities: fan=%u fan_mode=%u hwmon=%u profile=%u rgb=%u dyn=%u coolboost=%u led=%u\n",
		!!nitro_has_cap(nitro, NITRO_CAP_FAN),
		!!nitro_has_cap(nitro, NITRO_CAP_FAN_MODE),
		!!nitro_has_cap(nitro, NITRO_CAP_HWMON),
		!!nitro_has_cap(nitro, NITRO_CAP_PLATFORM_PROFILE),
		!!nitro_has_cap(nitro, NITRO_CAP_RGB),
		!!nitro_has_cap(nitro, NITRO_CAP_RGB_DYNAMIC),
		!!nitro_has_cap(nitro, NITRO_CAP_COOLBOOST),
		!!nitro_has_cap(nitro, NITRO_CAP_LED_CLASS));
	return 0;

err_pdev:
	platform_set_drvdata(nitro_pdev, NULL);
	platform_device_unregister(nitro_pdev);
	nitro_pdev = NULL;
	kfree(nitro);
	return ret;
}

static void __exit nitro_exit(void)
{
	struct nitro_device *nitro = nitro_current();

	if (nitro_pdev) {
		sysfs_remove_groups(&nitro_pdev->dev.kobj, nitro_groups);
		platform_set_drvdata(nitro_pdev, NULL);
		platform_device_unregister(nitro_pdev);
		nitro_pdev = NULL;
	}

	if (nitro && nitro->have_saved && !nitro->probe_mode) {
		unsigned int i, nzones = nitro_zone_count(nitro);
		u8 bri = 100;
		u8 eff[16];
		u8 zc[NITRO_MAX_ZONES][3];

		if (nitro->have_dyn && nitro->saved_dyn[3] <= 100)
			bri = nitro->saved_dyn[3];

		mutex_lock(&nitro->lock);
		/* best-effort restore, ignore errors during unload */
		if (
			nitro->have_fan_modes &&
			nitro_has_cap(nitro, NITRO_CAP_FAN_MODE) &&
			nitro->saved_fan_cpu_mode >= NITRO_FAN_MODE_AUTO &&
			(nitro->quirks->fan_count < 2 ||
			nitro->saved_fan_gpu_mode >= NITRO_FAN_MODE_AUTO))
			nitro_set_fan_behavior(
				nitro->quirks->fan_count >= 2 ?
					NITRO_FANBIT_BOTH : NITRO_FANBIT_CPU,
				nitro->saved_fan_cpu_mode,
				nitro->saved_fan_gpu_mode);
		if (nitro_has_cap(nitro, NITRO_CAP_FAN)) {
			if (nitro->have_fan_cpu)
				nitro_set_fan(NITRO_FAN_CPU, nitro->saved_fan_cpu);
			if (nitro->quirks->fan_count >= 2 && nitro->have_fan_gpu)
				nitro_set_fan(NITRO_FAN_GPU, nitro->saved_fan_gpu);
		}
		if (nitro_has_cap(nitro, NITRO_CAP_RGB) && nitro->have_rgb) {
			for (i = 0; i < nzones; i++) {
				zc[i][0] = (u8)((unsigned int)nitro->saved_zone[i][0] * bri / 100);
				zc[i][1] = (u8)((unsigned int)nitro->saved_zone[i][1] * bri / 100);
				zc[i][2] = (u8)((unsigned int)nitro->saved_zone[i][2] * bri / 100);
			}
			nitro_apply_static(nitro, zc, bri);
		}
		if (
			nitro_has_cap(nitro, NITRO_CAP_RGB_DYNAMIC) &&
			nitro->have_dyn &&
			nitro->saved_dyn[1] >= 1 &&
			nitro->saved_dyn[1] <= nitro->quirks->dynamic_effects) {
			nitro_build_effect(eff,
				nitro->saved_dyn[1], nitro->saved_dyn[2], bri,
				nitro->saved_dyn[5],
				nitro->saved_dyn[6], nitro->saved_dyn[7],
				nitro->saved_dyn[8]);
			nitro_set_dynamic_raw(eff);
		}
		if (
			nitro->have_ftbl &&
			nitro_has_cap(nitro, NITRO_CAP_PLATFORM_PROFILE) &&
			nitro->quirks->profile_backend == NITRO_PROFILE_FTBL)
			nitro_set_ftbl(nitro->saved_ftbl);
		if (
			nitro->have_coolboost &&
			nitro_has_cap(nitro, NITRO_CAP_COOLBOOST) &&
			nitro_set_coolboost(nitro->saved_coolboost))
			pr_warn("CoolBoost restore failed\n");
		pr_info("restored fan %u/%u ftbl 0x%02x coolboost %u\n",
			nitro->saved_fan_cpu, nitro->saved_fan_gpu,
			nitro->saved_ftbl,
			nitro->have_coolboost ? nitro->saved_coolboost : 0);
		mutex_unlock(&nitro->lock);
	}

	kfree(nitro);
	pr_info("unloaded\n");
}

module_init(nitro_init);
module_exit(nitro_exit);
