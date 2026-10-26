/* Reject short/incompatible configs without reading across the fixed prefix. */
#define _GNU_SOURCE
#include "benchset.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <sys/mman.h>
#include <unistd.h>
#endif

int main(void) {
    size_t page_size;
    unsigned char *pages;
#ifdef _WIN32
    SYSTEM_INFO info;
    GetSystemInfo(&info);
    page_size = info.dwPageSize;
    pages = VirtualAlloc(NULL, page_size * 2, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    assert(pages != NULL);
    DWORD old_protection;
    assert(VirtualProtect(pages + page_size, page_size, PAGE_NOACCESS, &old_protection));
#else
    long system_page_size = sysconf(_SC_PAGESIZE);
    assert(system_page_size > 0);
    page_size = (size_t)system_page_size;
    pages = mmap(NULL, page_size * 2, PROT_READ | PROT_WRITE,
                 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(pages != MAP_FAILED);
    assert(mprotect(pages + page_size, page_size, PROT_NONE) == 0);
#endif
    struct Prefix { uint32_t abi_version, struct_size; };
    const struct Prefix invalid[] = {
        {0, sizeof(BenchsetConfig)},
        {2, sizeof(BenchsetConfig)},
        {UINT32_MAX, sizeof(BenchsetConfig)},
        {BENCHSET_ABI_VERSION, 0},
        {BENCHSET_ABI_VERSION, sizeof(struct Prefix)},
        {BENCHSET_ABI_VERSION, sizeof(BenchsetConfig) - 1},
        {BENCHSET_ABI_VERSION, sizeof(BenchsetConfig) + 1},
    };
    unsigned char *prefix = pages + page_size - sizeof(struct Prefix);
    for (size_t index = 0; index < sizeof(invalid) / sizeof(invalid[0]); ++index) {
        memcpy(prefix, &invalid[index], sizeof(struct Prefix));
        BenchsetHandle *handle = benchset_create();
        assert(handle != NULL);
        assert(benchset_run(handle, (const BenchsetConfig *)prefix, NULL, NULL) == BENCHSET_FAILED);
        BenchsetResult result;
        assert(benchset_result(handle, &result) == 1);
        assert(result.status == BENCHSET_FAILED && result.iterations == 0 && result.sample_count == 0);
        assert(strstr(benchset_reason(handle), "incompatible C ABI/config size") != NULL);
        benchset_free(handle);
    }
#ifdef _WIN32
    assert(VirtualFree(pages, 0, MEM_RELEASE));
#else
    assert(munmap(pages, page_size * 2) == 0);
#endif
    puts("C config prefix stops at a protected page");
    return 0;
}
