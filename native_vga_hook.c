/* Native Unicorn callback for planar VGA A000h writes.
 *
 * This mirrors Video.graphics_write().  Keeping it in C avoids crossing the
 * Python C-API boundary once for every byte of a Mode 10h blit.
 */
#include <stdint.h>
#include <string.h>
#include <unicorn/unicorn.h>

struct vga_state {
    uint8_t *planes[4];
    uint8_t *seq, *gdc, *latches, *active, *dirty, *ram;
    uint64_t fills, copies;
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

/* On the first store of REP STOS in write mode 1, skip Unicorn's remaining
 * per-byte callbacks and complete the latching fill plane-wise. */
static int fast_mode1_stos(uc_engine *uc, struct vga_state *s,
                           uint64_t address, int size) {
    uint64_t ip, cs, es, di, cx, flags;
    if (size != 1 || (s->gdc[5] & 3) != 1 ||
            uc_reg_read(uc, UC_X86_REG_EIP, &ip) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_CS, &cs) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_ES, &es) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_DI, &di) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_CX, &cx) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_EFLAGS, &flags) != UC_ERR_OK ||
            !cx || (flags & 0x400)) return 0;
    uint32_t code = ((uint32_t)cs << 4) + (uint16_t)ip;
    code &= 0xfffff;
    if (s->ram[code] != 0xf3 || (s->ram[(code + 1) & 0xfffff] != 0xaa &&
            s->ram[(code + 1) & 0xfffff] != 0xab)) return 0;
    unsigned width = s->ram[(code + 1) & 0xfffff] == 0xab ? 2 : 1;
    uint32_t length = (uint16_t)cx * width;
    uint32_t destination = ((uint32_t)(uint16_t)es << 4) + (uint16_t)di;
    if (destination != address || destination < 0xa0000 ||
            destination + length > 0xb0000) return 0;
    unsigned offset = destination - 0xa0000;
    for (unsigned p = 0; p < 4; p++)
        if (s->seq[2] & (1u << p)) memset(s->planes[p] + offset, s->latches[p], length);
    cx = 0;
    di = ((uint16_t)di + length) & 0xffff;
    ip = ((uint16_t)ip + 2) & 0xffff;
    uc_reg_write(uc, UC_X86_REG_CX, &cx);
    uc_reg_write(uc, UC_X86_REG_DI, &di);
    uc_reg_write(uc, UC_X86_REG_EIP, &ip);
    s->dirty[0] = 1;
    s->fills++;
    return 1;
}

static int fast_mode1_movs(uc_engine *uc, struct vga_state *s,
                           uint64_t address, int size) {
    uint64_t ip, cs, ds, es, si, di, cx, flags;
    if (size != 1 || (s->gdc[5] & 3) != 1 ||
            uc_reg_read(uc, UC_X86_REG_EIP, &ip) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_CS, &cs) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_DS, &ds) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_ES, &es) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_SI, &si) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_DI, &di) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_CX, &cx) != UC_ERR_OK ||
            uc_reg_read(uc, UC_X86_REG_EFLAGS, &flags) != UC_ERR_OK ||
            !cx || (flags & 0x400)) return 0;
    uint32_t code = (((uint32_t)cs << 4) + (uint16_t)ip) & 0xfffff;
    if (s->ram[code] != 0xf3 || (s->ram[(code + 1) & 0xfffff] != 0xa4 &&
            s->ram[(code + 1) & 0xfffff] != 0xa5)) return 0;
    unsigned width = s->ram[(code + 1) & 0xfffff] == 0xa5 ? 2 : 1;
    uint32_t length = (uint16_t)cx * width;
    uint32_t source = ((uint32_t)(uint16_t)ds << 4) + (uint16_t)si;
    uint32_t destination = ((uint32_t)(uint16_t)es << 4) + (uint16_t)di;
    if (source != address || source < 0xa0000 || destination < 0xa0000 ||
            source + length > 0xb0000 || destination + length > 0xb0000 ||
            (source < destination && destination < source + length)) return 0;
    unsigned src = source - 0xa0000, dst = destination - 0xa0000;
    for (unsigned p = 0; p < 4; p++) {
        if (s->seq[2] & (1u << p))
            memcpy(s->planes[p] + dst, s->planes[p] + src, length);
        s->latches[p] = s->planes[p][src + length - 1];
    }
    cx = 0;
    si = ((uint16_t)si + length) & 0xffff;
    di = ((uint16_t)di + length) & 0xffff;
    ip = ((uint16_t)ip + 2) & 0xffff;
    uc_reg_write(uc, UC_X86_REG_CX, &cx);
    uc_reg_write(uc, UC_X86_REG_SI, &si);
    uc_reg_write(uc, UC_X86_REG_DI, &di);
    uc_reg_write(uc, UC_X86_REG_EIP, &ip);
    s->dirty[0] = 1;
    s->copies++;
    return 1;
}

static void vga_write(uc_engine *uc, uc_mem_type type, uint64_t address,
                      int size, int64_t value, void *user_data) {
    (void)uc; (void)type;
    struct vga_state *s = user_data;
    if (!s->active[0]) return;
    if (fast_mode1_stos(uc, s, address, size)) return;
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

static void vga_read(uc_engine *uc, uc_mem_type type, uint64_t address,
                     int size, int64_t value, void *user_data) {
    (void)type; (void)value;
    struct vga_state *s = user_data;
    if (s->active[0]) fast_mode1_movs(uc, s, address, size);
}

int x86_vga_install(uc_engine *uc, struct vga_state *state, uc_hook *hook) {
    int error = uc_hook_add(uc, hook, UC_HOOK_MEM_WRITE, vga_write, state,
                            0xa0000, 0xaffff);
    if (error != UC_ERR_OK) return error;
    return uc_hook_add(uc, hook, UC_HOOK_MEM_READ, vga_read, state,
                       0xa0000, 0xaffff);
}

int x86_block_install(uc_engine *uc, struct block_state *state, uc_hook *hook) {
    return uc_hook_add(uc, hook, UC_HOOK_BLOCK, record_block, state, 1, 0);
}
