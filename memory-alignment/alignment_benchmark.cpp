#include <iostream>
#include <chrono>
#include <vector>
#include <random>
#include <algorithm>
#include <iomanip>
#include <cstring>
#include <numeric>
#include <cmath>

#ifdef __APPLE__
#include <sys/sysctl.h>
#endif

#ifdef __linux__
#include <fstream>
#endif

using namespace std;
using namespace std::chrono;

// Prevent compiler optimization
volatile uint64_t sink = 0;

struct BenchmarkResult {
    string name;
    double mean_ns;
    double median_ns;
    double stddev_ns;
    size_t operations;
};

class AlignedBuffer {
private:
    void* raw_ptr;
    void* aligned_ptr;
    size_t size;
    size_t offset;

public:
    AlignedBuffer(size_t buf_size, size_t alignment_offset = 0)
        : size(buf_size), offset(alignment_offset) {
        // Allocate extra space for alignment + offset
        raw_ptr = malloc(buf_size + 64 + alignment_offset);
        if (!raw_ptr) throw std::bad_alloc();

        // Align to 64 bytes, then add offset to misalign if requested
        uintptr_t addr = reinterpret_cast<uintptr_t>(raw_ptr);
        uintptr_t aligned_addr = (addr + 63) & ~63ULL;
        aligned_ptr = reinterpret_cast<void*>(aligned_addr + alignment_offset);

        // Initialize with data
        memset(aligned_ptr, 0, buf_size);
    }

    ~AlignedBuffer() {
        free(raw_ptr);
    }

    uint64_t* data() { return static_cast<uint64_t*>(aligned_ptr); }
    size_t get_size() const { return size; }
    size_t get_offset() const { return offset; }

    bool is_aligned() const {
        return (reinterpret_cast<uintptr_t>(aligned_ptr) % 8) == 0;
    }
};

double calculate_median(vector<double>& times) {
    size_t n = times.size();
    sort(times.begin(), times.end());
    if (n % 2 == 0) {
        return (times[n/2 - 1] + times[n/2]) / 2.0;
    } else {
        return times[n/2];
    }
}

double calculate_stddev(const vector<double>& times, double mean) {
    double sq_sum = 0.0;
    for (double t : times) {
        sq_sum += (t - mean) * (t - mean);
    }
    return sqrt(sq_sum / times.size());
}

BenchmarkResult run_sequential_read(AlignedBuffer& buf, size_t iterations) {
    vector<double> times;
    size_t count = buf.get_size() / sizeof(uint64_t);
    uint64_t* data = buf.data();

    // Warm-up
    for (int i = 0; i < 3; i++) {
        uint64_t sum = 0;
        for (size_t j = 0; j < count; j++) {
            sum += data[j];
        }
        sink = sum;
    }

    // Actual benchmark
    for (size_t iter = 0; iter < iterations; iter++) {
        auto start = high_resolution_clock::now();

        uint64_t sum = 0;
        for (size_t i = 0; i < count; i++) {
            sum += data[i];
        }
        sink = sum;

        auto end = high_resolution_clock::now();
        times.push_back(duration_cast<nanoseconds>(end - start).count());
    }

    double mean = accumulate(times.begin(), times.end(), 0.0) / times.size();
    double median = calculate_median(times);
    double stddev = calculate_stddev(times, mean);

    return {"Sequential Read", mean, median, stddev, count};
}

BenchmarkResult run_sequential_write(AlignedBuffer& buf, size_t iterations) {
    vector<double> times;
    size_t count = buf.get_size() / sizeof(uint64_t);
    uint64_t* data = buf.data();

    // Warm-up
    for (int i = 0; i < 3; i++) {
        for (size_t j = 0; j < count; j++) {
            data[j] = j;
        }
    }

    // Actual benchmark
    for (size_t iter = 0; iter < iterations; iter++) {
        auto start = high_resolution_clock::now();

        for (size_t i = 0; i < count; i++) {
            data[i] = i;
        }

        auto end = high_resolution_clock::now();
        times.push_back(duration_cast<nanoseconds>(end - start).count());
    }

    double mean = accumulate(times.begin(), times.end(), 0.0) / times.size();
    double median = calculate_median(times);
    double stddev = calculate_stddev(times, mean);

    return {"Sequential Write", mean, median, stddev, count};
}

BenchmarkResult run_random_read(AlignedBuffer& buf, size_t iterations) {
    vector<double> times;
    size_t count = buf.get_size() / sizeof(uint64_t);
    uint64_t* data = buf.data();

    // Generate random indices
    vector<size_t> indices(count);
    for (size_t i = 0; i < count; i++) indices[i] = i;

    random_device rd;
    mt19937 gen(42); // Fixed seed for reproducibility
    shuffle(indices.begin(), indices.end(), gen);

    // Warm-up
    for (int i = 0; i < 3; i++) {
        uint64_t sum = 0;
        for (size_t idx : indices) {
            sum += data[idx];
        }
        sink = sum;
    }

    // Actual benchmark
    for (size_t iter = 0; iter < iterations; iter++) {
        auto start = high_resolution_clock::now();

        uint64_t sum = 0;
        for (size_t idx : indices) {
            sum += data[idx];
        }
        sink = sum;

        auto end = high_resolution_clock::now();
        times.push_back(duration_cast<nanoseconds>(end - start).count());
    }

    double mean = accumulate(times.begin(), times.end(), 0.0) / times.size();
    double median = calculate_median(times);
    double stddev = calculate_stddev(times, mean);

    return {"Random Read", mean, median, stddev, count};
}

BenchmarkResult run_random_write(AlignedBuffer& buf, size_t iterations) {
    vector<double> times;
    size_t count = buf.get_size() / sizeof(uint64_t);
    uint64_t* data = buf.data();

    // Generate random indices
    vector<size_t> indices(count);
    for (size_t i = 0; i < count; i++) indices[i] = i;

    random_device rd;
    mt19937 gen(42);
    shuffle(indices.begin(), indices.end(), gen);

    // Warm-up
    for (int i = 0; i < 3; i++) {
        for (size_t idx : indices) {
            data[idx] = idx;
        }
    }

    // Actual benchmark
    for (size_t iter = 0; iter < iterations; iter++) {
        auto start = high_resolution_clock::now();

        for (size_t idx : indices) {
            data[idx] = idx;
        }

        auto end = high_resolution_clock::now();
        times.push_back(duration_cast<nanoseconds>(end - start).count());
    }

    double mean = accumulate(times.begin(), times.end(), 0.0) / times.size();
    double median = calculate_median(times);
    double stddev = calculate_stddev(times, mean);

    return {"Random Write", mean, median, stddev, count};
}

BenchmarkResult run_rmw(AlignedBuffer& buf, size_t iterations) {
    vector<double> times;
    size_t count = buf.get_size() / sizeof(uint64_t);
    uint64_t* data = buf.data();

    // Initialize
    for (size_t i = 0; i < count; i++) data[i] = i;

    // Warm-up
    for (int i = 0; i < 3; i++) {
        for (size_t j = 0; j < count; j++) {
            data[j] = data[j] * 2 + 1;
        }
    }

    // Reset
    for (size_t i = 0; i < count; i++) data[i] = i;

    // Actual benchmark
    for (size_t iter = 0; iter < iterations; iter++) {
        auto start = high_resolution_clock::now();

        for (size_t i = 0; i < count; i++) {
            data[i] = data[i] * 2 + 1;
        }

        auto end = high_resolution_clock::now();
        times.push_back(duration_cast<nanoseconds>(end - start).count());

        // Reset for next iteration
        for (size_t i = 0; i < count; i++) data[i] = i;
    }

    double mean = accumulate(times.begin(), times.end(), 0.0) / times.size();
    double median = calculate_median(times);
    double stddev = calculate_stddev(times, mean);

    return {"Read-Modify-Write", mean, median, stddev, count};
}

void print_result(const BenchmarkResult& result, size_t offset) {
    string alignment = (offset == 0) ? "Aligned (0)" :
                      "Misaligned (+" + to_string(offset) + ")";

    cout << "  " << setw(25) << left << alignment
         << setw(12) << right << fixed << setprecision(2) << result.median_ns << " ns"
         << setw(12) << right << fixed << setprecision(2) << result.mean_ns << " ns"
         << setw(12) << right << fixed << setprecision(2) << result.stddev_ns << " ns"
         << endl;
}

void run_benchmark_suite(const string& size_name, size_t buffer_size, size_t iterations) {
    cout << "\n========================================" << endl;
    cout << "BUFFER SIZE: " << size_name << " (" << buffer_size / 1024 << " KB)" << endl;
    cout << "========================================" << endl;

    vector<size_t> offsets = {0, 1, 2, 4};

    for (const auto& test_name : {"Sequential Read", "Sequential Write", "Random Read", "Random Write", "Read-Modify-Write"}) {
        cout << "\n" << test_name << ":" << endl;
        cout << "  " << setw(25) << left << "Alignment"
             << setw(12) << right << "Median"
             << setw(12) << right << "Mean"
             << setw(12) << right << "Std Dev" << endl;
        cout << "  " << string(61, '-') << endl;

        vector<BenchmarkResult> results;

        for (size_t offset : offsets) {
            AlignedBuffer buf(buffer_size, offset);

            BenchmarkResult result;
            if (string(test_name) == "Sequential Read") {
                result = run_sequential_read(buf, iterations);
            } else if (string(test_name) == "Sequential Write") {
                result = run_sequential_write(buf, iterations);
            } else if (string(test_name) == "Random Read") {
                result = run_random_read(buf, iterations);
            } else if (string(test_name) == "Random Write") {
                result = run_random_write(buf, iterations);
            } else if (string(test_name) == "Read-Modify-Write") {
                result = run_rmw(buf, iterations);
            }

            results.push_back(result);
            print_result(result, offset);
        }

        // Print performance comparison
        cout << "\n  Performance vs Aligned:" << endl;
        for (size_t i = 1; i < results.size(); i++) {
            double slowdown = ((results[i].median_ns - results[0].median_ns) / results[0].median_ns) * 100.0;
            cout << "  " << setw(25) << left << ("Misaligned (+" + to_string(offsets[i]) + ")")
                 << setw(12) << right << fixed << setprecision(2) << showpos << slowdown << "%" << noshowpos << endl;
        }
    }
}

string get_cpu_info() {
#ifdef __APPLE__
    char cpu_brand[256];
    size_t size = sizeof(cpu_brand);
    if (sysctlbyname("machdep.cpu.brand_string", &cpu_brand, &size, NULL, 0) == 0) {
        return string(cpu_brand);
    }
#elif defined(__linux__)
    ifstream cpuinfo("/proc/cpuinfo");
    string line;
    while (getline(cpuinfo, line)) {
        if (line.find("model name") != string::npos) {
            size_t colon = line.find(':');
            if (colon != string::npos) {
                string cpu = line.substr(colon + 2);
                return cpu;
            }
        }
    }
#elif defined(_WIN32)
    // Windows would need registry access or WMI
    return "Windows CPU (use Task Manager for details)";
#endif
    return "Unknown CPU";
}

string get_architecture() {
#if defined(__x86_64__) || defined(_M_X64)
    return "x86_64";
#elif defined(__aarch64__) || defined(_M_ARM64)
    return "ARM64";
#elif defined(__i386__) || defined(_M_IX86)
    return "x86";
#elif defined(__arm__) || defined(_M_ARM)
    return "ARM32";
#else
    return "Unknown";
#endif
}

int main() {
    cout << "64-bit Alignment Performance Benchmark" << endl;
    cout << "=======================================" << endl;
    cout << "Platform: " << get_cpu_info() << endl;
    cout << "Architecture: " << get_architecture() << endl;
    cout << "Compiler: " << __VERSION__ << endl;

    // Test different buffer sizes
    run_benchmark_suite("L1 Cache", 16 * 1024, 100);        // 16 KB
    run_benchmark_suite("L2 Cache", 256 * 1024, 50);        // 256 KB
    run_benchmark_suite("Beyond Cache", 16 * 1024 * 1024, 20);  // 16 MB

    cout << "\n========================================" << endl;
    cout << "Benchmark Complete!" << endl;
    cout << "========================================" << endl;

    return 0;
}
