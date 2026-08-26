/* Native Unicorn callback for planar VGA A000h writes.
 *
 * This mirrors Video.graphics_write().  Keeping it in C avoids crossing the
 * Python C-API boundary once for every byte of a Mode 10h blit.
 */
#include <stdint.h>
#include <unicorn/unicorn.h>

struct vga_state {
    uint8_t *planes[4];
    uint8_t *seq, *gdc, *latches, *active, *dirty;
};

struct block_state {
    uint64_t address;
    uint32_t size;
};

static void record_block(uc_engine *uc, uint64_t address, uint32_t size,
                         void *user_data) {
    (void)uc;
    struct block_state *state = user_data;
    state->address = address;
    state->size = size;
}

static uint8_t rotate_right(uint8_t value, unsigned amount) {
    return amount ? (uint8_t)((value >> amount) | (value << (8 - amount))) : value;
}

static uint8_t logical_op(uint8_t source, uint8_t latch, unsigned op) {
    if (op == 1) return source & latch;
    if (op == 2) return source | latch;
    if (op == 3) return source ^ latch;
    return source;
}

static void vga_write(uc_engine *uc, uc_mem_type type, uint64_t address,
                      int size, int64_t value, void *user_data) {
    (void)uc; (void)type;
    struct vga_state *s = user_data;
    if (!s->active[0]) return;
    for (int i = 0; i < size && address + (uint64_t)i < 0xb0000; i++) {
        unsigned offset = (unsigned)(address + (uint64_t)i - 0xa0000) & 0xffff;
        uint8_t data = (uint8_t)((uint64_t)value >> (8 * i));
        unsigned mode = s->gdc[5] & 3;
        unsigned rotate_function = s->gdc[3];
        data = rotate_right(data, rotate_function & 7);
        if (mode != 1)
            for (unsigned p = 0; p < 4; p++) s->latches[p] = s->planes[p][offset];
        for (unsigned p = 0; p < 4; p++) {
            if (!(s->seq[2] & (1u << p))) continue;
            uint8_t latch = s->latches[p], source, mask, result;
            if (mode == 1) result = latch;
            else {
                if (mode == 0) {
                    source = (s->gdc[0] & (1u << p)) ? 0xff : 0;
                    if (!(s->gdc[1] & (1u << p))) source = data;
                } else if (mode == 2) {
                    source = (data & (1u << p)) ? 0xff : 0;
                } else {
                    source = (s->gdc[0] & (1u << p)) ? 0xff : 0;
                }
                mask = s->gdc[8];
                if (mode == 3) mask &= data;
                result = (uint8_t)((latch & ~mask) |
                    (logical_op(source, latch, (rotate_function >> 3) & 3) & mask));
            }
            s->planes[p][offset] = result;
        }
    }
    s->dirty[0] = 1;
}

int x86_vga_install(uc_engine *uc, struct vga_state *state, uc_hook *hook) {
    return uc_hook_add(uc, hook, UC_HOOK_MEM_WRITE, vga_write, state,
                       0xa0000, 0xaffff);
}

int x86_block_install(uc_engine *uc, struct block_state *state, uc_hook *hook) {
    return uc_hook_add(uc, hook, UC_HOOK_BLOCK, record_block, state, 1, 0);
}
