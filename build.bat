@echo off
rem PyInstaller 打包：标注工具 + 实时提取（需先 pip install pyinstaller）
cd /d "%~dp0"
python -m PyInstaller -F -w -n "OCR标注工具" --distpath dist --workpath build --specpath build --exclude-module rapidocr_onnxruntime --exclude-module onnxruntime src/roi_annotator.py
python -m PyInstaller -F -w -n "OCR实时提取" --distpath dist --workpath build --specpath build --exclude-module rapidocr_onnxruntime --exclude-module onnxruntime src/realtime_ui.py
echo.
echo 构建完成，产物在 dist 目录
pause
