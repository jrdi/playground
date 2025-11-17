CXX = clang++
CXXFLAGS = -std=c++17 -O3 -Wall -Wextra

BUILD_DIR = build
TARGETS = $(BUILD_DIR)/padding_performance $(BUILD_DIR)/cache_effect_benchmark
SOURCES = padding_performance.cpp cache_effect_benchmark.cpp

.PHONY: all clean run-padding run-cache

all: $(BUILD_DIR) $(TARGETS)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/padding_performance: padding_performance.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $<

$(BUILD_DIR)/cache_effect_benchmark: cache_effect_benchmark.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -o $@ $<

run-padding: $(BUILD_DIR)/padding_performance
	$(BUILD_DIR)/padding_performance

run-cache: $(BUILD_DIR)/cache_effect_benchmark
	$(BUILD_DIR)/cache_effect_benchmark

clean:
	rm -rf $(BUILD_DIR)
