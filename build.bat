@echo off
rem Build standalone exes with PyInstaller (run: pip install pyinstaller first)
cd /d "%~dp0"
python -m PyInstaller -F -w -n "OSD-Annotator" --distpath dist --workpath build --specpath build --exclude-module rapidocr_onnxruntime --exclude-module onnxruntime src/roi_annotator.py
python -m PyInstaller -F -w -n "OSD-Realtime" --distpath dist --workpath build --specpath build --exclude-module rapidocr_onnxruntime --exclude-module onnxruntime src/realtime_ui.py
echo.
echo Done. Outputs are in the dist folder.
pause
