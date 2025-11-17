#include <iostream>
#include <chrono>
#include <vector>
#include <iomanip>

using namespace std;
using namespace std::chrono;

struct NormalStruct {
    uint8_t  a;
    uint64_t b;
    uint16_t c;
};

struct __attribute__((packed)) PackedStruct {
    uint8_t  a;
    uint64_t b;
    uint16_t c;
};

template<typename T>
double benchmark_sequential_read(vector<T>& data, size_t iterations) {
    volatile uint64_t sum = 0;
    auto start = high_resolution_clock::now();

    for (size_t iter = 0; iter < iterations; iter++) {
        for (size_t i = 0; i < data.size(); i++) {
            sum += data[i].b;
        }
    }

    auto end = high_resolution_clock::now();

    if (sum == 0xDEADBEEF) cout << "";

    return duration_cast<nanoseconds>(end - start).count() / 1e6;
}

void test_array_size(size_t count, size_t iterations) {
    vector<NormalStruct> normal_data(count);
    vector<PackedStruct> packed_data(count);

    // Initialize
    for (size_t i = 0; i < count; i++) {
        normal_data[i].a = i & 0xFF;
        normal_data[i].b = i * 1000;
        normal_data[i].c = i & 0xFFFF;

        packed_data[i].a = i & 0xFF;
        packed_data[i].b = i * 1000;
        packed_data[i].c = i & 0xFFFF;
    }

    // Warmup
    for (int i = 0; i < 10; i++) {
        benchmark_sequential_read(normal_data, 10);
        benchmark_sequential_read(packed_data, 10);
    }

    // Benchmark
    double normal_time = benchmark_sequential_read(normal_data, iterations);
    double packed_time = benchmark_sequential_read(packed_data, iterations);

    size_t normal_kb = (count * sizeof(NormalStruct)) / 1024;
    size_t packed_kb = (count * sizeof(PackedStruct)) / 1024;
    double diff_percent = ((packed_time - normal_time) / normal_time) * 100.0;

    cout << setw(12) << count
         << setw(15) << normal_kb
         << setw(15) << packed_kb
         << setw(15) << fixed << setprecision(2) << normal_time
         << setw(15) << packed_time
         << setw(12) << setprecision(1);

    if (diff_percent > 0) {
        cout << "+" << diff_percent << "%";
    } else {
        cout << diff_percent << "%";
    }

    cout << setw(18);
    if (abs(diff_percent) < 1.0) {
        cout << "~tie";
    } else if (diff_percent < 0) {
        cout << "Packed faster";
    } else {
        cout << "Aligned faster";
    }
    cout << endl;
}

int main() {
    cout << "Cache Effect on Aligned vs Misaligned Performance" << endl;
    cout << string(105, '=') << endl;
    cout << "\nHypothesis: Packed structs perform better when they fit in cache but aligned doesn't\n" << endl;

    cout << setw(12) << "Array Size"
         << setw(15) << "Normal (KB)"
         << setw(15) << "Packed (KB)"
         << setw(15) << "Normal (ms)"
         << setw(15) << "Packed (ms)"
         << setw(12) << "Diff"
         << setw(18) << "Winner" << endl;
    cout << string(105, '-') << endl;

    // Test progressively larger arrays
    test_array_size(100, 100000);        // Fits in L1 on both
    test_array_size(1000, 100000);       // Fits in L1 on M1, L1/L2 boundary on Ryzen
    test_array_size(5000, 50000);        // L1/L2 boundary
    test_array_size(10000, 10000);       // L2 on both
    test_array_size(30000, 5000);        // Approaching L2 limit on Ryzen
    test_array_size(50000, 2000);        // CRITICAL: Packed fits L2, Normal doesn't (on Ryzen)
    test_array_size(100000, 1000);       // Both exceed L2
    test_array_size(200000, 500);        // Way beyond L2

    cout << string(105, '=') << endl;
    cout << "\nCache Sizes (approximate per core):" << endl;
    cout << "  M1 Pro:    L1=64KB, L2=4MB" << endl;
    cout << "  Ryzen 7:   L1=32KB, L2=1MB" << endl;
    cout << "\nKey Thresholds:" << endl;
    cout << "  ~1,365 elements = L1 limit for NormalStruct (Ryzen)" << endl;
    cout << "  ~43,690 elements = L2 limit for NormalStruct (Ryzen)" << endl;
    cout << "  ~95,325 elements = L2 limit for PackedStruct (Ryzen)" << endl;

    return 0;
}
