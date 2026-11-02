# Linux/CI build (g++). On Windows use build.bat (MSVC); both compilers build
# the same strictly standard C++20 sources with warnings as errors.

CXX ?= g++
CXXFLAGS = -std=c++20 -O2 -Wall -Wextra -Werror

all: build/bench build/tests

build/bench: src/bench.cpp src/engine.hpp src/flow.hpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) src/bench.cpp -o build/bench

build/tests: tests/tests.cpp src/engine.hpp src/flow.hpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) tests/tests.cpp -o build/tests

check: build/tests
	./build/tests

run: build/bench
	./build/bench

clean:
	rm -rf build *.obj *.exe *.pdb

.PHONY: all check run clean
