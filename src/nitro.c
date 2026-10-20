// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * nitro - Acer Nitro AN515-57 EC/WMI driver v2.0.0
 * (support for other models are to be added in later updates)
 *
 * Targets (current): Acer Nitro AN515-57 (Scala_TLS), BIOS V1.22.
 * (entry to remove soon)
 * 
 * Uses WMI GUID 7A4DDFE7-5B5D-40B4-8595-4408E0CC7F56 (object BH -> WMBH).
 * Restores initial fan/RGB/FTBL on unload. See docs/REVERSE.md.
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/init.h>
#include <linux/types.h>
#include <linux/dmi.h>
#include <linux/acpi.h>
#include <linux/platform_device.h>
#include <linux/mutex.h>
#include <linux/hwmon.h>
#include <linux/leds.h>
#include <linux/led-class-multicolor.h>
#include <linux/platform_profile.h>
#include <linux/string.h>

MODULE_AUTHOR("SurrealArts");
MODULE_DESCRIPTION("Acer Nitro AN515-57 WMI driver (hwmon + leds + platform_profile)");
MODULE_LICENSE("GPL");
MODULE_VERSION("2.0.0");

#define NITRO_WMI_GUID "7A4DDFE7-5B5D-40B4-8595-4408E0CC7F56"

#define NITRO_BH_SET_LED			0x02
#define NITRO_BH_SYSINFO			0x05
#define NITRO_BH_SET_STATIC			0x06
#define NITRO_BH_GET_STATIC			0x07
#define NITRO_BH_SET_FAN_BEHAVIOR	0x0E
#define NITRO_BH_GET_FAN_BEHAVIOR	0x0F
#define NITRO_BH_SET_FAN			0x10
#define NITRO_BH_GET_FAN			0x11
#define NITRO_BH_SET_FTBL			0x12
#define NITRO_BH_GET_FTBL			0x13
#define NITRO_BH_SET_DYN			0x14
#define NITRO_BH_GET_DYN			0x15
#define NITRO_BH_SET_MISC			0x16
#define NITRO_BH_GET_MISC			0x17

#define NITRO_FAN_CPU				0x01
#define NITRO_FAN_GPU				0x04

#define NITRO_FANBIT_CPU			0x01
#define NITRO_FANBIT_GPU			0x08
#define NITRO_FANBIT_BOTH			0x09

enum nitro_fan_mode {
	NITRO_FAN_MODE_AUTO		= 1,
	NITRO_FAN_MODE_MAX		= 2,
	NITRO_FAN_MODE_CUSTOM	= 3,
};

enum nitro_ftbl {
	NITRO_FTBL_BALANCED		= 0x00,
	NITRO_FTBL_QUIET		= 0x01,
	NITRO_FTBL_PERF			= 0x02,
	NITRO_FTBL_TURBO		= 0x03,
	NITRO_FTBL_ECO			= 0x04,
};

static struct platform_device *nitro_pdev;
static DEFINE_MUTEX(nitro_lock);

static u8 saved_fan_cpu = 50, saved_fan_gpu = 50;
static u8 saved_fan_cpu_mode, saved_fan_gpu_mode;
static bool have_fan_modes;
static u8 saved_zone[4][3];
static u8 saved_dyn[16];
static u8 saved_ftbl = NITRO_FTBL_BALANCED;
static bool have_saved;
static u8 cur_zone[4][3];

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
	int ret = -EIO;

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
	if (!obj) {
		ret = -ENODATA;
		goto out;
	}
	if (obj->type != ACPI_TYPE_BUFFER) {
		pr_debug("BH 0x%x unexpected type %d\n", method, obj->type);
		ret = -EBADMSG;
		goto out;
	}
	if (obj->buffer.length < out_len)
		out_len = obj->buffer.length;
	memcpy(out, obj->buffer.pointer, out_len);
	ret = 0;
out:
	kfree(obj);
	return ret;
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
		return -ENODATA;
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
		return -ENODATA;
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
		return -EPERM;
	ret = nitro_bh_call(
		NITRO_BH_GET_MISC,
		in, sizeof(in),
		out, sizeof(out));
	if (ret)
		return ret;
	if (out[0] != 0)
		return -ENODATA;
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
		return -ENODATA;
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
		return -ENODATA;
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
	if (duty < 20)
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
		return -EIO;
	*cpu_mode = out[1] & 0x3;
	*gpu_mode = (out[1] >> 6) & 0x3;
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

	return nitro_bh_call(
		NITRO_BH_GET_DYN,
		in, sizeof(in),
		data16, 16);
}

static int nitro_apply_static(const u8 zones[4][3], u8 brightness)
{
	const u8 zone_ids[4] = { 0x01, 0x02, 0x04, 0x08 };
	u8 led[8] = { 0x08, 0, 0, 0, 0, 0x0F, 0, 0 };
	u8 out[8] = { 0 };
	u8 bl[16] = { 0 };
	u8 v0, v1;
	int i, ret;

	ret = nitro_get_sysinfo(0, 0, &v0, &v1);
	if (ret)
		return ret;
	ret = nitro_bh_call(
		NITRO_BH_SET_LED,
		led, sizeof(led),
		out, sizeof(out));
	if (ret)
		return ret;
	for (i = 0; i < 4; i++) {
		ret = nitro_set_static(
			zone_ids[i],
			zones[i][0], zones[i][1], zones[i][2]);
		if (ret)
			return ret;
	}
	bl[2] = brightness;
	bl[9] = 1;
	return nitro_set_dynamic_raw(bl);
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

static ssize_t fan_cpu_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	u8 duty;
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_fan(NITRO_FAN_CPU, &duty);
	mutex_unlock(&nitro_lock);
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
	unsigned long v;
	int ret;

	ret = kstrtoul(buf, 10, &v);
	if (ret)
		return ret;
	if (v > 100)
		return -ERANGE;
	mutex_lock(&nitro_lock);
	ret = nitro_set_fan_behavior(NITRO_FANBIT_CPU, NITRO_FAN_MODE_CUSTOM, 0);
	if (!ret)
		ret = nitro_set_fan(NITRO_FAN_CPU, (u8)v);
	mutex_unlock(&nitro_lock);
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
	u8 duty;
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_fan(NITRO_FAN_GPU, &duty);
	mutex_unlock(&nitro_lock);
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
	unsigned long v;
	int ret;

	ret = kstrtoul(buf, 10, &v);
	if (ret)
		return ret;
	if (v > 100)
		return -ERANGE;
	mutex_lock(&nitro_lock);
	ret = nitro_set_fan_behavior(NITRO_FANBIT_GPU, 0, NITRO_FAN_MODE_CUSTOM);
	if (!ret)
		ret = nitro_set_fan(NITRO_FAN_GPU, (u8)v);
	mutex_unlock(&nitro_lock);
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
	u8 cpu, gpu;
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_fan_behavior(NITRO_FANBIT_BOTH, &cpu, &gpu);
	mutex_unlock(&nitro_lock);
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
	char work[64], *p, *tok, *key, *val;
	u8 cpu = 0, gpu = 0, c, g;
	bool have_cpu = false, have_gpu = false;
	int ret;

	if (count >= sizeof(work))
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

	mutex_lock(&nitro_lock);
	if (!have_cpu || !have_gpu) {
		ret = nitro_get_fan_behavior(NITRO_FANBIT_BOTH, &c, &g);
		if (ret)
			goto out;
		if (!have_cpu)
			cpu = c;
		if (!have_gpu)
			gpu = g;
	}
	ret = nitro_set_fan_behavior(NITRO_FANBIT_BOTH, cpu, gpu);
out:
	mutex_unlock(&nitro_lock);
	if (ret)
		return ret;
	pr_info("fan mode cpu=%u gpu=%u\n", cpu, gpu);
	return count;
}
static DEVICE_ATTR_RW(fan_mode);

static ssize_t ftbl_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	u8 v;
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_ftbl(&v);
	mutex_unlock(&nitro_lock);
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
	unsigned long v;
	int ret;

	ret = kstrtoul(buf, 10, &v);
	if (ret)
		return ret;
	if (v > NITRO_FTBL_ECO)
		return -ERANGE;
	mutex_lock(&nitro_lock);
	ret = nitro_set_ftbl((u8)v);
	mutex_unlock(&nitro_lock);
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
	u8 v;
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_misc(0x01, &v);
	mutex_unlock(&nitro_lock);
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
	u8 in[4] = { 0x01, 0, 0, 0 };
	u8 out[4] = { 0 };
	unsigned long v;
	int ret;

	ret = kstrtoul(buf, 10, &v);
	if (ret)
		return ret;
	if (v > 1)
		return -ERANGE;
	in[1] = (u8)v;
	mutex_lock(&nitro_lock);
	ret = nitro_bh_call(
		NITRO_BH_SET_MISC,
		in, sizeof(in),
		out, sizeof(out));
	mutex_unlock(&nitro_lock);
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
	u8 v;
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_misc(0x0B, &v);
	mutex_unlock(&nitro_lock);
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
	u8 r, g, b;
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_static(0x01, &r, &g, &b);
	mutex_unlock(&nitro_lock);
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
	u8 r[4], g[4], b[4];
	const u8 zones[4] = { 0x01, 0x02, 0x04, 0x08 };
	int i, ret = 0;

	mutex_lock(&nitro_lock);
	for (i = 0; i < 4; i++) {
		if (nitro_get_static(zones[i], &r[i], &g[i], &b[i])) {
			r[i] = g[i] = b[i] = 0;
			ret = -EIO;
		}
	}
	mutex_unlock(&nitro_lock);
	return sysfs_emit(buf, "1:%u %u %u 2:%u %u %u 4:%u %u %u 8:%u %u %u\n",
		r[0], g[0], b[0], r[1], g[1], b[1],
		r[2], g[2], b[2], r[3], g[3], b[3]);
}

static ssize_t kbd_static_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	const u8 zone_ids[4] = { 0x01, 0x02, 0x04, 0x08 };
	unsigned int zone, r, g, b;
	u8 dyn[16], scaled[4][3];
	u8 brightness = 100;
	int i, ret;

	if (sscanf(buf, "%u %u %u %u", &zone, &r, &g, &b) != 4)
		return -EINVAL;
	if ((zone != 1 && zone != 2 && zone != 4 && zone != 8) ||
		r > 255 || g > 255 || b > 255)
		return -EINVAL;
	mutex_lock(&nitro_lock);
	if (!nitro_get_dynamic_raw(dyn) && dyn[3] <= 100)
		brightness = dyn[3];
	for (i = 0; i < 4; i++) {
		if (zone_ids[i] == zone) {
			cur_zone[i][0] = (u8)r;
			cur_zone[i][1] = (u8)g;
			cur_zone[i][2] = (u8)b;
		}
		scaled[i][0] = (u8)((unsigned int)cur_zone[i][0] * brightness / 100);
		scaled[i][1] = (u8)((unsigned int)cur_zone[i][1] * brightness / 100);
		scaled[i][2] = (u8)((unsigned int)cur_zone[i][2] * brightness / 100);
	}
	ret = nitro_apply_static(scaled, brightness);
	mutex_unlock(&nitro_lock);
	if (ret)
		return ret;
	pr_info("static zone %u set %u %u %u brightness %u\n",
		zone, r, g, b, brightness);
	return count;
}
static DEVICE_ATTR_RW(kbd_static);

static u8 wmi_raw_out[16];
static size_t wmi_raw_out_len;
static int wmi_raw_status;

static ssize_t wmi_raw_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	ssize_t n = 0;
	size_t i;

	mutex_lock(&nitro_lock);
	n += sysfs_emit_at(buf, n, "status=%d len=%zu", wmi_raw_status, wmi_raw_out_len);
	for (i = 0; i < wmi_raw_out_len && n < PAGE_SIZE - 4; i++)
		n += sysfs_emit_at(buf, n, " %02x", wmi_raw_out[i]);
	mutex_unlock(&nitro_lock);
	n += sysfs_emit_at(buf, n, "\n");
	return n;
}

static ssize_t wmi_raw_store(
	struct device *dev,
	struct device_attribute *attr,
	const char *buf,
	size_t count)
{
	unsigned int vals[17];
	u8 in[16];
	u8 out[16] = { 0 };
	int n, i, ret;

	n = sscanf(
		buf,
		"%u %u %u %u %u %u %u %u %u %u %u %u %u %u %u %u %u",
		&vals[0], &vals[1], &vals[2], &vals[3], &vals[4], &vals[5],
		&vals[6], &vals[7], &vals[8], &vals[9], &vals[10], &vals[11],
		&vals[12], &vals[13], &vals[14], &vals[15], &vals[16]);
	if (n < 1 || n > 17)
		return -EINVAL;
	for (i = 0; i < n; i++) {
		if (vals[i] > 0xFF)
			return -ERANGE;
	}
	if (n - 1 > 16)
		return -EINVAL;
	for (i = 0; i < n - 1; i++)
		in[i] = (u8)vals[i + 1];
	mutex_lock(&nitro_lock);
	ret = nitro_bh_call(vals[0], in, n - 1, out, sizeof(out));
	wmi_raw_status = ret;
	if (!ret) {
		memcpy(wmi_raw_out, out, sizeof(out));
		wmi_raw_out_len = sizeof(out);
	}
	mutex_unlock(&nitro_lock);
	if (ret)
		return ret;
	pr_info("wmi_raw method 0x%x inlen %d\n", vals[0], n - 1);
	return count;
}
static DEVICE_ATTR(wmi_raw, 0600, wmi_raw_show, wmi_raw_store);

static ssize_t kbd_dynamic_show(
	struct device *dev,
	struct device_attribute *attr,
	char *buf)
{
	u8 d[16] = { 0 };
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_dynamic_raw(d);
	mutex_unlock(&nitro_lock);
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
	unsigned int mode, speed, brightness, dir, r, g, b;
	u8 payload[16];
	int ret;

	if (sscanf(
		buf, "%u %u %u %u %u %u %u",
		&mode, &speed, &brightness,
		&dir, &r, &g, &b) != 7)
		return -EINVAL;
	if (mode < 1 || mode > 5 || speed > 255 || brightness > 100 ||
		(dir != 1 && dir != 2) || r > 255 || g > 255 || b > 255)
		return -EINVAL;
	nitro_build_effect(
		payload,
		mode,
		speed,
		brightness,
		dir, r, g, b);
	mutex_lock(&nitro_lock);
	ret = nitro_set_dynamic_raw(payload);
	mutex_unlock(&nitro_lock);
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
	u8 d[16] = { 0 };
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_dynamic_raw(d);
	mutex_unlock(&nitro_lock);
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
	unsigned int bri;
	u8 d[16] = { 0 };
	u8 eff[16];
	u8 zc[4][3];
	int i, ret;

	if (sscanf(buf, "%u", &bri) != 1 || bri > 100)
		return -EINVAL;
	mutex_lock(&nitro_lock);
	ret = nitro_get_dynamic_raw(d);
	if (ret)
		goto out;
	if (d[1] >= 1 && d[1] <= 5) {
		nitro_build_effect(eff, d[1], d[2], bri, d[5], d[6], d[7], d[8]);
		ret = nitro_set_dynamic_raw(eff);
	} else {
		for (i = 0; i < 4; i++) {
			zc[i][0] = (u8)((unsigned int)cur_zone[i][0] * bri / 100);
			zc[i][1] = (u8)((unsigned int)cur_zone[i][1] * bri / 100);
			zc[i][2] = (u8)((unsigned int)cur_zone[i][2] * bri / 100);
		}
		ret = nitro_apply_static(zc, bri);
	}
out:
	mutex_unlock(&nitro_lock);
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
	u8 v0, v1;
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_sysinfo(1, 1, &v0, &v1);
	mutex_unlock(&nitro_lock);
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
	switch (type) {
	case hwmon_fan:
		if ((channel == 0 || channel == 1) && attr == hwmon_fan_input)
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
	unsigned int v;
	int ret;

	switch (type) {
	case hwmon_fan:
		if (attr != hwmon_fan_input)
			return -EOPNOTSUPP;
		if (channel != 0 && channel != 1)
			return -EOPNOTSUPP;
		mutex_lock(&nitro_lock);
		ret = nitro_get_rpm(channel == 0, &v);
		mutex_unlock(&nitro_lock);
		if (ret)
			return ret;
		*val = v;
		return 0;
	case hwmon_temp:
		if (attr != hwmon_temp_input || channel != 0)
			return -EOPNOTSUPP;
		mutex_lock(&nitro_lock);
		ret = nitro_get_ctml(&v);
		mutex_unlock(&nitro_lock);
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

#define NITRO_NZONES 4

static const u8 nitro_zone_ids[NITRO_NZONES] = { 0x01, 0x02, 0x04, 0x08 };
static const char *const nitro_zone_names[NITRO_NZONES] = {
	"nitro:rgb:zone1", "nitro:rgb:zone2",
	"nitro:rgb:zone3", "nitro:rgb:zone4",
};

struct nitro_led {
	struct led_classdev_mc mc;
	struct mc_subled sub[3];
	u8 zone;
};

static struct nitro_led nitro_leds[NITRO_NZONES];

static int nitro_led_set(struct led_classdev *cdev, enum led_brightness brightness)
{
	struct led_classdev_mc *mc = lcdev_to_mccdev(cdev);
	struct nitro_led *nl = container_of(mc, struct nitro_led, mc);
	u8 dyn[16];
	u8 r, g, b, bri = 100;
	int i, ret;

	led_mc_calc_color_components(mc, brightness);
	r = (u8)mc->subled_info[0].intensity;
	g = (u8)mc->subled_info[1].intensity;
	b = (u8)mc->subled_info[2].intensity;

	mutex_lock(&nitro_lock);
	if (!nitro_get_dynamic_raw(dyn) && dyn[3] <= 100)
		bri = dyn[3];
	for (i = 0; i < 4; i++) {
		if (nitro_zone_ids[i] == nl->zone) {
			cur_zone[i][0] = r;
			cur_zone[i][1] = g;
			cur_zone[i][2] = b;
		}
	}
	ret = nitro_apply_static(cur_zone, bri);
	mutex_unlock(&nitro_lock);
	return ret;
}

static int nitro_leds_register(struct device *parent)
{
	int i, j, ret;

	for (i = 0; i < NITRO_NZONES; i++) {
		struct nitro_led *nl = &nitro_leds[i];

		nl->zone = nitro_zone_ids[i];
		nl->sub[0].color_index = LED_COLOR_ID_RED;
		nl->sub[0].channel = 0;
		nl->sub[1].color_index = LED_COLOR_ID_GREEN;
		nl->sub[1].channel = 1;
		nl->sub[2].color_index = LED_COLOR_ID_BLUE;
		nl->sub[2].channel = 2;
		if (have_saved) {
			nl->sub[0].intensity = saved_zone[i][0];
			nl->sub[1].intensity = saved_zone[i][1];
			nl->sub[2].intensity = saved_zone[i][2];
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
	u8 ftbl;
	int ret;

	mutex_lock(&nitro_lock);
	ret = nitro_get_ftbl(&ftbl);
	if (!ret)
		ret = nitro_ftbl_to_profile(ftbl, profile);
	mutex_unlock(&nitro_lock);
	return ret;
}

static int nitro_profile_set(struct device *dev, enum platform_profile_option profile)
{
	u8 ftbl;
	int ret;

	ret = nitro_profile_to_ftbl(profile, &ftbl);
	if (ret)
		return ret;
	mutex_lock(&nitro_lock);
	ret = nitro_set_ftbl(ftbl);
	mutex_unlock(&nitro_lock);
	if (!ret)
		pr_info("platform profile %d -> FTBL 0x%02x\n", profile, ftbl);
	return ret;
}

static int nitro_profile_probe(void *drvdata, unsigned long *choices)
{
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

static struct attribute *nitro_attrs[] = {
	&dev_attr_fan_cpu.attr,
	&dev_attr_fan_gpu.attr,
	&dev_attr_fan_mode.attr,
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
ATTRIBUTE_GROUPS(nitro);

static const struct dmi_system_id nitro_dmi[] __initconst = {
	{
		.matches = {
			DMI_MATCH(DMI_SYS_VENDOR, "Acer"),
			DMI_MATCH(DMI_PRODUCT_NAME, "Nitro AN515-57"),
		},
	},
	{}
};
MODULE_DEVICE_TABLE(dmi, nitro_dmi);

static void nitro_save_initial(void)
{
	const u8 zones[4] = { 0x01, 0x02, 0x04, 0x08 };
	int i;
	u8 d;

	if (!nitro_get_fan(NITRO_FAN_CPU, &d))
		saved_fan_cpu = d;
	if (!nitro_get_fan(NITRO_FAN_GPU, &d))
		saved_fan_gpu = d;
	if (!nitro_get_fan_behavior(
		NITRO_FANBIT_BOTH, &saved_fan_cpu_mode, &saved_fan_gpu_mode))
		have_fan_modes = true;
	for (i = 0; i < 4; i++) {
		if (nitro_get_static(
			zones[i], &saved_zone[i][0],
			&saved_zone[i][1], &saved_zone[i][2]))
			saved_zone[i][0] = saved_zone[i][1] = saved_zone[i][2] = 0;
	}
	memcpy(cur_zone, saved_zone, sizeof(cur_zone));
	if (nitro_get_dynamic_raw(saved_dyn))
		memset(saved_dyn, 0, sizeof(saved_dyn));
	if (nitro_get_ftbl(&d))
		d = NITRO_FTBL_BALANCED;
	saved_ftbl = d;
	have_saved = true;
}

static int __init nitro_init(void)
{
	u8 duty, ftbl, r, g, b, v0, v1, tkst, opms;
	u8 dyn[16] = { 0 };
	struct device *hwmon;
	struct device *pp;
	int ret;

	pr_info("v2.0.0 loading\n");

	if (!dmi_check_system(nitro_dmi)) {
		pr_err("DMI is not Acer Nitro AN515-57, refusing\n");
		return -ENODEV;
	}
	if (!wmi_has_guid(NITRO_WMI_GUID)) {
		pr_err("WMI GUID %s not present\n", NITRO_WMI_GUID);
		return -ENODEV;
	}

	nitro_pdev = platform_device_register_simple("nitro", -1, NULL, 0);
	if (IS_ERR(nitro_pdev))
		return PTR_ERR(nitro_pdev);

	ret = sysfs_create_groups(&nitro_pdev->dev.kobj, nitro_groups);
	if (ret) {
		platform_device_unregister(nitro_pdev);
		return ret;
	}

	mutex_lock(&nitro_lock);
	nitro_save_initial();
	pr_info("saved fan cpu=%u gpu=%u\n", saved_fan_cpu, saved_fan_gpu);
	if (!nitro_get_fan(NITRO_FAN_CPU, &duty))
		pr_info("EC CPU fan duty=%u\n", duty);
	if (!nitro_get_fan(NITRO_FAN_GPU, &duty))
		pr_info("EC GPU fan duty=%u\n", duty);
	if (!nitro_get_ftbl(&ftbl))
		pr_info("FTBL=0x%02x\n", ftbl);
	if (!nitro_get_static(0x01, &r, &g, &b))
		pr_info("zone1 RGB=%u %u %u\n", r, g, b);
	if (!nitro_get_sysinfo(1, 1, &v0, &v1))
		pr_info("CTML raw=0x%02x%02x\n", v1, v0);
	if (!nitro_get_dynamic_raw(dyn))
		pr_info("dyn raw %02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x %02x%02x%02x%02x\n",
			dyn[0], dyn[1], dyn[2], dyn[3], dyn[4], dyn[5], dyn[6], dyn[7],
			dyn[8], dyn[9], dyn[10], dyn[11], dyn[12], dyn[13], dyn[14], dyn[15]);
	if (!nitro_get_misc(0x01, &tkst))
		pr_info("TKST=0x%02x\n", tkst);
	if (!nitro_get_misc(0x0B, &opms))
		pr_info("OPMS=0x%02x\n", opms);
	mutex_unlock(&nitro_lock);

	hwmon = devm_hwmon_device_register_with_info(
		&nitro_pdev->dev,
		"nitro", NULL,
		&nitro_hwmon_chip, NULL);
	if (IS_ERR(hwmon)) {
		ret = PTR_ERR(hwmon);
		pr_err("hwmon register failed %d\n", ret);
		goto err_hwmon;
	}

	ret = nitro_leds_register(&nitro_pdev->dev);
	if (ret)
		goto err_hwmon;

	pp = devm_platform_profile_register(
		&nitro_pdev->dev,
		"nitro", NULL,
		&nitro_profile_ops);
	if (IS_ERR(pp)) {
		ret = PTR_ERR(pp);
		pr_err("platform_profile register failed %d\n", ret);
		goto err_hwmon;
	}

	pr_info("sysfs RW: fan_cpu/gpu (0-100, forces custom), fan_mode (both or cpu=/gpu=), ftbl (0-4), tkst (0/1 experimental), kbd_static, kbd_dynamic, kbd_bright, wmi_raw (0600 debug). RO: opms, kbd_zone1, ec_cpu_temp. hwmon + leds + platform_profile registered.\n");
	return 0;

err_hwmon:
	sysfs_remove_groups(&nitro_pdev->dev.kobj, nitro_groups);
	platform_device_unregister(nitro_pdev);
	return ret;
}

static void __exit nitro_exit(void)
{
	int i;

	if (have_saved) {
		u8 zc[4][3];
		u8 bri = saved_dyn[3] <= 100 ? saved_dyn[3] : 100;
		u8 eff[16];

		/* best-effort restore, ignore errors during unload */
		if (have_fan_modes &&
			saved_fan_cpu_mode >= NITRO_FAN_MODE_AUTO &&
			saved_fan_gpu_mode >= NITRO_FAN_MODE_AUTO)
			nitro_set_fan_behavior(
				NITRO_FANBIT_BOTH,
				saved_fan_cpu_mode, saved_fan_gpu_mode);
		nitro_set_fan(NITRO_FAN_CPU, saved_fan_cpu);
		nitro_set_fan(NITRO_FAN_GPU, saved_fan_gpu);
		for (i = 0; i < 4; i++) {
			zc[i][0] = (u8)((unsigned int)saved_zone[i][0] * bri / 100);
			zc[i][1] = (u8)((unsigned int)saved_zone[i][1] * bri / 100);
			zc[i][2] = (u8)((unsigned int)saved_zone[i][2] * bri / 100);
		}
		nitro_apply_static(zc, bri);
		if (saved_dyn[1] != 0) {
			nitro_build_effect(eff,
				saved_dyn[1], saved_dyn[2], bri,
				saved_dyn[5],
				saved_dyn[6], saved_dyn[7], saved_dyn[8]);
			nitro_set_dynamic_raw(eff);
		}
		nitro_set_ftbl(saved_ftbl);
		pr_info("restored fan %u/%u ftbl 0x%02x\n", saved_fan_cpu, saved_fan_gpu, saved_ftbl);
	}
	sysfs_remove_groups(&nitro_pdev->dev.kobj, nitro_groups);
	platform_device_unregister(nitro_pdev);
	pr_info("unloaded\n");
}

module_init(nitro_init);
module_exit(nitro_exit);
