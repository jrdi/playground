CXX = clang++
CXXFLAGS = -std=c++17 -O3 -Wall -Wextra
TARGET = alignment_benchmark
SOURCE = alignment_benchmark.cpp

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SOURCE)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SOURCE)

run: $(TARGET)
	./$(TARGET)

clean:
	rm -f $(TARGET)
