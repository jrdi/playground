# Memory Alignment vs Cache Efficiency Benchmarks

This repository contains benchmarks exploring the performance trade-offs between memory alignment and cache efficiency.

**Read the full blog post**: http://jordivillar.com/blog/memory-alignment

## TL;DR

Memory alignment matters, but not always in the way you'd expect. While misaligned access does incur penalties (2-11% on small arrays that fit in L1 cache), cache efficiency often dominates at scale.

**Key Finding**: On x86-64 (AMD Ryzen 7 8745HS), packed structs were **8-9% faster** for large arrays (50,000+ elements) despite being misaligned, because their smaller footprint (54% less memory) provides better cache utilization.

**The lesson**: Measure your specific workload. The "slower" misaligned approach can win through better cache behavior.

## Benchmarks

### 1. `padding_performance.cpp`

**Purpose**: Isolates true alignment penalties by testing small arrays (100 elements) that fit comfortably in L1 cache.

Tests:
- Sequential read/write operations
- Random access patterns
- Read-modify-write operations

Compares aligned (NormalStruct, 24 bytes) vs packed (PackedStruct, 11 bytes) performance.

**Run it:**
```bash
make run-padding
```

**Results on x86-64 (AMD Ryzen 7 8745HS):**
```
Test                   Aligned (ms)  Misaligned (ms)  Difference
-----------------------------------------------------------------
Sequential Read                2.37             2.45      +3.3%
Random Read                    3.33             3.70     +11.3%
Sequential Write               1.52             1.93     +26.7%
Read-Modify-Write              2.07             2.11      +2.0%
```

**Results on ARM64 (Apple M1 Pro):**
```
Test                   Aligned (ms)  Misaligned (ms)  Difference
-----------------------------------------------------------------
Sequential Read                9.80             9.76      -0.4%
Random Read                   20.82            20.81      -0.1%
Sequential Write               1.59             1.66      +5.0%
Read-Modify-Write              1.94             3.54     +82.7%
```

These results show the expected alignment penalties when cache effects are eliminated. Note how ARM64 handles most misalignment gracefully except for read-modify-write operations.

### 2. `cache_effect_benchmark.cpp`

**Purpose**: Demonstrates how cache efficiency can outweigh alignment penalties by testing different array sizes (100 to 200,000 elements).

**Run it:**
```bash
make run-cache
```

**Results on x86-64 (AMD Ryzen 7 8745HS):**
```
Array Size    Normal (KB)    Packed (KB)     Difference
--------------------------------------------------------
       100           2              1            +2.8%
      1000          23             10            +1.5%
     5,000         117             53            -5.4%
    10,000         234            107            -5.9%
    30,000         703            322            -6.6%
    50,000       1,171            537            -8.8%  ⭐ Critical point: packed fits in L2
   100,000       2,343          1,074            -8.7%
   200,000       4,687          2,148            -8.2%
```

**Key observation**: At ~50,000 elements, NormalStruct (1,171 KB) exceeds the L2 cache (1,024 KB) while PackedStruct (537 KB) still fits. This is where packed structs become 8-9% faster despite misalignment.

## Build All

```bash
make all
```

## Clean

```bash
make clean
```

## Test Platforms

Benchmarks were run on:

- **Apple M1 Pro (ARM64)**
  - L1 Data Cache: 64 KB
  - L2 Cache: 4 MB
  - macOS with clang++ -O3

- **AMD Ryzen 7 8745HS (x86-64)**
  - L1 Data Cache: 32 KB per core
  - L2 Cache: 1 MB per core
  - L3 Cache: 16 MB
  - Linux with clang++ -O3

## Key Insights

1. **Small arrays (fits in L1)**: Aligned structs show expected but minimal penalties (2-11% on x86-64)
2. **Large arrays (exceeds L2)**: Packed structs win through better cache utilization (8-9% faster)
3. **Modern CPUs handle misalignment well**: Alignment penalties are surprisingly small on both architectures
4. **ARM64 exception**: Read-modify-write shows dramatic 82.7% penalty on misaligned data
5. **Random access on x86-64**: Most sensitive to alignment (11.3% penalty)
6. **The crossover point**: At ~50,000 elements on x86-64, cache efficiency dominates over alignment
7. **Cache is king**: Memory footprint matters more than alignment at scale

## Requirements

- C++17 compiler (clang++ or g++)
- `-O3` optimization recommended for realistic results

## License

MIT License - feel free to use and modify for your own experiments.
