@echo off
rem Windows build (MSVC). Produces tests.exe and bench.exe in this directory.
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cl /nologo /std:c++20 /O2 /EHsc /W4 /WX /Isrc tests\tests.cpp /Fe:tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /O2 /EHsc /W4 /WX /Isrc src\bench.cpp /Fe:bench.exe
if errorlevel 1 exit /b 1
echo build ok: tests.exe bench.exe
