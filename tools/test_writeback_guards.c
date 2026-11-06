// SPDX-License-Identifier: GPL-3.0-or-later
#include <stdio.h>

#include "../src/writeback.h"

static int failures;

static void expect(const char *what, int got, int want)
{
	if (got == want)
		return;
	printf("FAIL %s: got %d, want %d\n", what, got, want);
	failures++;
}

int main(void)
{
	expect("no fan, cpu", nitro_wb_fan_allowed(0, 0), 0);
	expect("no fan, gpu", nitro_wb_fan_allowed(0, 1), 0);
	expect("one fan, cpu", nitro_wb_fan_allowed(1, 0), 1);
	expect("one fan, gpu", nitro_wb_fan_allowed(1, 1), 0);
	expect("two fans, cpu", nitro_wb_fan_allowed(2, 0), 1);
	expect("two fans, gpu", nitro_wb_fan_allowed(2, 1), 1);

	expect("cpu auto, gpu auto", nitro_wb_modes_ok(2, 1, 1), 1);
	expect("cpu custom, gpu max", nitro_wb_modes_ok(2, 3, 2), 1);
	expect("gpu zero", nitro_wb_modes_ok(2, 1, 0), 0);
	expect("cpu zero", nitro_wb_modes_ok(2, 0, 1), 0);
	expect("cpu above custom", nitro_wb_modes_ok(2, 4, 1), 0);
	expect("one fan ignores gpu", nitro_wb_modes_ok(1, 3, 0), 1);
	expect("one fan cpu zero", nitro_wb_modes_ok(1, 0, 0), 0);
	expect("two fans gpu above custom", nitro_wb_modes_ok(2, 1, 4), 0);
	expect("one fan ignores gpu mode", nitro_wb_modes_ok(1, 1, 4), 1);

	expect("both auto", nitro_wb_fans_auto(2, 1, 1), 1);
	expect("gpu custom", nitro_wb_fans_auto(2, 1, 3), 0);
	expect("cpu max", nitro_wb_fans_auto(2, 2, 1), 0);
	expect("one fan auto", nitro_wb_fans_auto(1, 1, 0), 1);
	expect("one fan custom", nitro_wb_fans_auto(1, 3, 0), 0);
	expect("one fan ignores gpu", nitro_wb_fans_auto(1, 1, 3), 1);
	expect("both custom", nitro_wb_fans_auto(2, 3, 3), 0);
	expect("cpu not auto", nitro_wb_fans_auto(2, 0, 1), 0);
	expect("gpu not auto", nitro_wb_fans_auto(2, 1, 0), 0);

	if (failures) {
		printf("writeback guard check FAILED: %d assertion(s)\n", failures);
		return 1;
	}
	printf("writeback guard check OK\n");
	return 0;
}
