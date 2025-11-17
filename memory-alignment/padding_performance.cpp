#include <iostream>
#include <chrono>
#include <random>
#include <vector>
#include <algorithm>
#include <numeric>
#include <iomanip>
#include <cstddef>

using namespace std;
using namespace std::chrono;

// NORMAL struct - compiler adds padding for alignment
struct NormalStruct {
    uint8_t  a;      // 1 byte
    uint64_t b;      // 8 bytes (aligned at offset 8)
    uint16_t c;      // 2 bytes
    // Total: 24 bytes (with padding)
};

// PACKED struct - no padding, fields stored consecutively
struct __attribute__((packed)) PackedStruct {
    uint8_t  a;      // 1 byte
    uint64_t b;      // 8 bytes (misaligned at offset 1)
    uint16_t c;      // 2 bytes
    // Total: 11 bytes (no padding)
};

// Benchmark configuration
// const size_t ARRAY_SIZE = 100000;
// const size_t ITERATIONS = 1000;
const size_t ARRAY_SIZE = 100;
const size_t ITERATIONS = 100000;
const size_t WARMUP_ITERATIONS = 100;

// For random access
vector<size_t> generate_random_indices(size_t count, size_t max_index) {
    vector<size_t> indices(count);
    random_device rd;
    mt19937 gen(42); // Fixed seed for reproducibility
    uniform_int_distribution<size_t> dist(0, max_index - 1);

    for (size_t i = 0; i < count; i++) {
        indices[i] = dist(gen);
    }
    return indices;
}

// Statistics helper
struct Stats {
    double mean;
    double median;
    double stddev;
    double min_val;
    double max_val;
};

Stats calculate_stats(vector<double>& times) {
    Stats stats;
    size_t n = times.size();

    // Sort for median
    sort(times.begin(), times.end());

    stats.min_val = times.front();
    stats.max_val = times.back();
    stats.median = times[n / 2];

    // Mean
    stats.mean = accumulate(times.begin(), times.end(), 0.0) / n;

    // Standard deviation
    double variance = 0.0;
    for (double t : times) {
        variance += (t - stats.mean) * (t - stats.mean);
    }
    stats.stddev = sqrt(variance / n);

    return stats;
}

// Test 1: Sequential read of uint64_t b field
template<typename T>
double test_sequential_read(vector<T>& data) {
    volatile uint64_t sum = 0;
    auto start = high_resolution_clock::now();

    for (size_t iter = 0; iter < ITERATIONS; iter++) {
        for (size_t i = 0; i < data.size(); i++) {
            sum += data[i].b;
        }
    }

    auto end = high_resolution_clock::now();
    auto duration = duration_cast<nanoseconds>(end - start).count();

    // Prevent optimization
    if (sum == 0xDEADBEEF) cout << "";

    return duration / 1e6; // Convert to milliseconds
}

// Test 2: Random read of uint64_t b field
template<typename T>
double test_random_read(vector<T>& data, const vector<size_t>& indices) {
    volatile uint64_t sum = 0;
    auto start = high_resolution_clock::now();

    for (size_t iter = 0; iter < ITERATIONS; iter++) {
        for (size_t idx : indices) {
            sum += data[idx].b;
        }
    }

    auto end = high_resolution_clock::now();
    auto duration = duration_cast<nanoseconds>(end - start).count();

    if (sum == 0xDEADBEEF) cout << "";

    return duration / 1e6;
}

// Test 3: Sequential write to uint64_t b field
template<typename T>
double test_sequential_write(vector<T>& data) {
    auto start = high_resolution_clock::now();

    for (size_t iter = 0; iter < ITERATIONS; iter++) {
        for (size_t i = 0; i < data.size(); i++) {
            data[i].b = i * 2;
        }
    }

    auto end = high_resolution_clock::now();
    auto duration = duration_cast<nanoseconds>(end - start).count();

    return duration / 1e6;
}

// Test 4: Read-modify-write on uint64_t b field
template<typename T>
double test_read_modify_write(vector<T>& data) {
    auto start = high_resolution_clock::now();

    for (size_t iter = 0; iter < ITERATIONS; iter++) {
        for (size_t i = 0; i < data.size(); i++) {
            data[i].b = data[i].b * 2 + 1;
        }
    }

    auto end = high_resolution_clock::now();
    auto duration = duration_cast<nanoseconds>(end - start).count();

    return duration / 1e6;
}

// Benchmark results structure
struct BenchmarkResults {
    Stats sequential_read;
    Stats random_read;
    Stats sequential_write;
    Stats read_modify_write;
    Stats multi_field_access;
};

// Run all tests multiple times and collect statistics
template<typename T>
BenchmarkResults run_benchmark_suite(vector<T>& data, const vector<size_t>& random_indices) {
    const int NUM_RUNS = 10;
    BenchmarkResults results;

    // Test 1: Sequential Read
    {
        vector<double> times;
        for (int i = 0; i < NUM_RUNS; i++) {
            times.push_back(test_sequential_read(data));
        }
        results.sequential_read = calculate_stats(times);
    }

    // Test 2: Random Read
    {
        vector<double> times;
        for (int i = 0; i < NUM_RUNS; i++) {
            times.push_back(test_random_read(data, random_indices));
        }
        results.random_read = calculate_stats(times);
    }

    // Test 3: Sequential Write
    {
        vector<double> times;
        for (int i = 0; i < NUM_RUNS; i++) {
            times.push_back(test_sequential_write(data));
        }
        results.sequential_write = calculate_stats(times);
    }

    // Test 4: Read-Modify-Write
    {
        vector<double> times;
        for (int i = 0; i < NUM_RUNS; i++) {
            times.push_back(test_read_modify_write(data));
        }
        results.read_modify_write = calculate_stats(times);
    }

    return results;
}

// Print comparison table
void print_comparison(const BenchmarkResults& aligned, const BenchmarkResults& misaligned) {
    cout << "\n\nPerformance Comparison" << endl;
    cout << string(90, '=') << endl;
    cout << left << setw(25) << "Test"
         << right << setw(15) << "Aligned (ms)"
         << setw(17) << "Misaligned (ms)"
         << setw(12) << "Difference"
         << setw(21) << "Winner" << endl;
    cout << string(90, '-') << endl;

    auto print_row = [](const string& name, const Stats& aligned_stats, const Stats& misaligned_stats) {
        double aligned_time = aligned_stats.median;
        double misaligned_time = misaligned_stats.median;
        double diff_percent = ((misaligned_time - aligned_time) / aligned_time) * 100.0;

        cout << left << setw(25) << name
             << right << setw(15) << fixed << setprecision(2) << aligned_time
             << setw(17) << misaligned_time
             << setw(11);

        if (diff_percent > 0) {
            cout << "+" << setprecision(1) << diff_percent << "%";
        } else {
            cout << setprecision(1) << diff_percent << "%";
        }

        cout << setw(21);
        if (abs(diff_percent) < 1.0) {
            cout << "~tie";
        } else if (diff_percent < 0) {
            cout << "Misaligned";
        } else {
            cout << "Aligned";
        }
        cout << endl;
    };

    print_row("Sequential Read", aligned.sequential_read, misaligned.sequential_read);
    print_row("Random Read", aligned.random_read, misaligned.random_read);
    print_row("Sequential Write", aligned.sequential_write, misaligned.sequential_write);
    print_row("Read-Modify-Write", aligned.read_modify_write, misaligned.read_modify_write);
    print_row("Multi-field Access", aligned.multi_field_access, misaligned.multi_field_access);

    cout << string(90, '=') << endl;

    // Summary
    cout << "\nKey Insights:" << endl;
    cout << "  • Positive % = Misaligned is SLOWER (penalty)" << endl;
    cout << "  • Negative % = Misaligned is FASTER (better cache utilization)" << endl;
    cout << "  • Memory savings with PackedStruct: "
         << fixed << setprecision(1)
         << (100.0 * (1.0 - (double)sizeof(PackedStruct) / sizeof(NormalStruct)))
         << "%" << endl;
}

int main() {
    cout << "Padding Performance Benchmark" << endl;
    cout << "=============================" << endl;
    cout << "\nConfiguration:" << endl;
    cout << "  Array size: " << ARRAY_SIZE << " elements" << endl;
    cout << "  Iterations per test: " << ITERATIONS << endl;
    cout << "  Test runs: 10 (for statistics)" << endl;

    // Struct info
    cout << "\n\nStruct Information:" << endl;
    cout << string(60, '-') << endl;
    cout << "NormalStruct:" << endl;
    cout << "  Size: " << sizeof(NormalStruct) << " bytes" << endl;
    cout << "  Field 'a' offset: " << offsetof(NormalStruct, a) << endl;
    cout << "  Field 'b' offset: " << offsetof(NormalStruct, b) << " (8-byte aligned)" << endl;
    cout << "  Field 'c' offset: " << offsetof(NormalStruct, c) << endl;
    cout << "  Array memory: " << (sizeof(NormalStruct) * ARRAY_SIZE) / 1024.0 << " KB" << endl;

    cout << "\nPackedStruct:" << endl;
    cout << "  Size: " << sizeof(PackedStruct) << " bytes" << endl;
    cout << "  Field 'a' offset: " << offsetof(PackedStruct, a) << endl;
    cout << "  Field 'b' offset: " << offsetof(PackedStruct, b) << " (MISALIGNED)" << endl;
    cout << "  Field 'c' offset: " << offsetof(PackedStruct, c) << endl;
    cout << "  Array memory: " << (sizeof(PackedStruct) * ARRAY_SIZE) / 1024.0 << " KB" << endl;

    double memory_savings = 100.0 * (1.0 - (double)sizeof(PackedStruct) / sizeof(NormalStruct));
    cout << "\n  Memory savings: " << fixed << setprecision(1)
         << memory_savings << "% ("
         << sizeof(NormalStruct) - sizeof(PackedStruct) << " bytes per struct)" << endl;

    // Allocate arrays
    cout << "\nAllocating arrays..." << endl;
    vector<NormalStruct> normal_data(ARRAY_SIZE);
    vector<PackedStruct> packed_data(ARRAY_SIZE);

    // Initialize with same data
    for (size_t i = 0; i < ARRAY_SIZE; i++) {
        normal_data[i].a = i & 0xFF;
        normal_data[i].b = i * 1000;
        normal_data[i].c = i & 0xFFFF;

        packed_data[i].a = i & 0xFF;
        packed_data[i].b = i * 1000;
        packed_data[i].c = i & 0xFFFF;
    }

    // Generate random indices
    auto random_indices = generate_random_indices(ARRAY_SIZE, ARRAY_SIZE);

    cout << "\nRunning benchmarks..." << endl;
    cout << "(Warmup iterations: " << WARMUP_ITERATIONS << ")" << endl;

    // Warmup
    for (size_t i = 0; i < WARMUP_ITERATIONS; i++) {
        test_sequential_read(normal_data);
        test_sequential_read(packed_data);
    }

    // Run benchmarks
    cout << "\nBenchmarking NormalStruct (Aligned)..." << flush;
    BenchmarkResults aligned_results = run_benchmark_suite(normal_data, random_indices);
    cout << " Done." << endl;

    cout << "Benchmarking PackedStruct (Misaligned)..." << flush;
    BenchmarkResults misaligned_results = run_benchmark_suite(packed_data, random_indices);
    cout << " Done." << endl;

    // Print comparison
    print_comparison(aligned_results, misaligned_results);

    cout << "\n" << string(90, '=') << endl;
    cout << "Benchmark Complete!" << endl;

    return 0;
}
